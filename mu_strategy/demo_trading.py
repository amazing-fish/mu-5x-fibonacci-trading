from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from mu_strategy.entry.scanner import EntryScanResult, scan_entry
from mu_strategy.live.okx import OKXInstrumentSpec
from mu_strategy.market_data.service import CandleBundle, refresh_candle_bundle
from mu_strategy.market_data.universe import OKXSwapTicker, top_okx_usdt_swaps
from mu_strategy.strategies.registry import baseline_strategy_group


UniverseProvider = Callable[..., list[OKXSwapTicker]]
CandleLoader = Callable[..., CandleBundle]
Scanner = Callable[..., EntryScanResult]


@dataclass(frozen=True)
class DemoTradingConfig:
    universe_limit: int = 10
    days: int = 28
    data_dir: Path = Path("data")
    refresh: bool = False
    notional_usdt: float = 10.0
    max_open_positions: int = 3
    leverage: int = 5
    dry_run: bool = True


def run_once(
    config: DemoTradingConfig | None = None,
    *,
    broker: Any | None,
    universe_provider: UniverseProvider = top_okx_usdt_swaps,
    candle_loader: CandleLoader = refresh_candle_bundle,
    scanner: Scanner = scan_entry,
) -> dict[str, Any]:
    config = config or DemoTradingConfig()
    tickers = universe_provider(limit=config.universe_limit)
    open_exposure = 0
    open_position_inst_ids: set[str] = set()
    existing_client_order_ids: set[str] = set()
    account_context: dict[str, Any] = {}

    if not config.dry_run:
        if broker is None:
            raise RuntimeError("broker is required when dry_run is false")
        positions = broker.get_positions(inst_type="SWAP")
        open_orders = broker.get_open_orders(inst_type="SWAP")
        account_context = {"positions": positions, "open_orders": open_orders}
        account_error = _account_context_error(account_context)
        if account_error is not None:
            return {
                "mode": "blocked",
                "dry_run": config.dry_run,
                "reason": "account_context_error",
                "account_context": account_context,
                "account_error": account_error,
                "universe": [asdict(ticker) for ticker in tickers],
                "open_exposure": 0,
                "scans": [],
                "orders": [],
            }
        open_exposure = _count_open_exposure(positions, open_orders)
        open_position_inst_ids = _open_position_inst_ids(positions)
        existing_client_order_ids = _client_order_ids(open_orders)

    scans: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    remaining_capacity = max(0, config.max_open_positions - open_exposure)

    for ticker in tickers:
        bundle = candle_loader(
            ticker.inst_id,
            intervals=("15m", "1h"),
            days=config.days,
            data_dir=config.data_dir,
            refresh=config.refresh,
        )
        result = scanner(
            ticker.inst_id,
            bundle.candles_by_interval.get("15m", []),
            bundle.candles_by_interval.get("1h", []),
            config=baseline_strategy_group(ticker.inst_id).config,
        )
        scans.append(_scan_payload(result, bundle))

        if result.action != "enter" or result.trigger_price is None:
            continue

        plan = _build_order_plan(result, config)
        if config.dry_run:
            _attach_order_sizing(plan, broker, result)
            orders.append(plan)
            continue

        if remaining_capacity <= 0:
            plan["status"] = "blocked"
            plan["reason"] = "max_open_exposure_reached"
            orders.append(plan)
            continue
        if plan["client_order_id"] in existing_client_order_ids:
            plan["status"] = "blocked"
            plan["reason"] = "duplicate_client_order_id"
            orders.append(plan)
            continue
        if result.symbol in open_position_inst_ids:
            plan["status"] = "blocked"
            plan["reason"] = "symbol_position_already_open"
            orders.append(plan)
            continue

        _attach_order_sizing(plan, broker, result)
        if Decimal(str(plan.get("size") or "0")) <= 0:
            plan["status"] = "blocked"
            plan["reason"] = "order_size_below_lot"
            orders.append(plan)
            continue

        leverage_response = broker.set_leverage(inst_id=result.symbol, lever=config.leverage, margin_mode="isolated")
        if _okx_response_failed(leverage_response):
            plan["status"] = "blocked"
            plan["reason"] = "leverage_setup_failed"
            plan["response"] = leverage_response
            orders.append(plan)
            continue

        response = broker.place_limit_buy(
            inst_id=result.symbol,
            size=plan["size"],
            price=plan["limit_price"],
            client_order_id=plan["client_order_id"],
            confirm_demo_order=True,
        )
        if _okx_response_failed(response):
            plan["status"] = "blocked"
            plan["reason"] = "order_placement_failed"
            plan["response"] = response
            orders.append(plan)
            continue
        plan["status"] = "submitted"
        plan["response"] = response
        orders.append(plan)
        existing_client_order_ids.add(plan["client_order_id"])
        remaining_capacity -= 1

    return {
        "mode": "dry_run" if config.dry_run else "live_demo",
        "dry_run": config.dry_run,
        "universe": [asdict(ticker) for ticker in tickers],
        "open_exposure": open_exposure,
        "account_context": account_context,
        "scans": scans,
        "orders": orders,
    }


def generate_client_order_id(symbol: str, signal_time_ms: int | None, trigger_price: float) -> str:
    source = f"{symbol}:{signal_time_ms or 0}:{trigger_price:.8f}"
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:20].upper()
    return f"OD{digest}"


def _build_order_plan(result: EntryScanResult, config: DemoTradingConfig) -> dict[str, Any]:
    assert result.trigger_price is not None
    return {
        "symbol": result.symbol,
        "status": "planned",
        "reason": result.reason,
        "client_order_id": generate_client_order_id(result.symbol, result.signal_time_ms, result.trigger_price),
        "notional_usdt": config.notional_usdt,
        "trigger_price": result.trigger_price,
        "limit_price": _float_to_price_text(result.trigger_price),
        "size": None,
        "initial_stop": result.initial_stop,
        "signal_time_ms": result.signal_time_ms,
    }


def _attach_order_sizing(plan: dict[str, Any], broker: Any | None, result: EntryScanResult) -> None:
    if broker is None or result.trigger_price is None:
        return
    instrument = broker.get_instruments(inst_type="SWAP", inst_id=result.symbol)
    spec = _instrument_spec(instrument)
    if spec is None:
        plan["reason"] = "instrument_spec_unavailable"
        return
    plan["limit_price"] = spec.price_to_string(result.trigger_price)
    plan["size"] = spec.size_for_notional(plan["notional_usdt"], price=plan["limit_price"])


def _instrument_spec(response: dict[str, Any]) -> OKXInstrumentSpec | None:
    data = response.get("data")
    if not isinstance(data, list) or not data:
        return None
    return OKXInstrumentSpec.from_row(data[0])


def _scan_payload(result: EntryScanResult, bundle: CandleBundle) -> dict[str, Any]:
    payload = asdict(result)
    payload["data_files"] = {interval: str(path) for interval, path in bundle.files_by_interval.items()}
    return payload


def _count_open_exposure(positions: dict[str, Any], open_orders: dict[str, Any]) -> int:
    count = 0
    for row in positions.get("data") or []:
        if _decimal(row.get("pos")) != 0:
            count += 1
    count += len(open_orders.get("data") or [])
    return count


def _open_position_inst_ids(positions: dict[str, Any]) -> set[str]:
    return {
        str(row.get("instId"))
        for row in positions.get("data") or []
        if row.get("instId") and _decimal(row.get("pos")) != 0
    }


def _client_order_ids(open_orders: dict[str, Any]) -> set[str]:
    return {str(row.get("clOrdId")) for row in open_orders.get("data") or [] if row.get("clOrdId")}


def _account_context_error(account_context: dict[str, Any]) -> dict[str, str] | None:
    for component, response in account_context.items():
        code = str(response.get("code", "")) if isinstance(response, dict) else ""
        if code not in {"", "0"}:
            return {
                "component": component,
                "code": code,
                "msg": str(response.get("msg", "")) if isinstance(response, dict) else "",
            }
    return None


def _okx_response_failed(response: dict[str, Any]) -> bool:
    if str(response.get("code", "")) not in {"", "0"}:
        return True

    data = response.get("data")
    if not isinstance(data, list):
        return False
    for row in data:
        if isinstance(row, dict) and "sCode" in row and str(row.get("sCode", "")) not in {"", "0"}:
            return True
    return False


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


def _float_to_price_text(value: float) -> str:
    return format(Decimal(str(value)).normalize(), "f")
