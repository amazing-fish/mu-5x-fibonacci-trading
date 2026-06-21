from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from mu_strategy.entry.scanner import EntryScanResult, scan_entry
from mu_strategy.live.okx import OKXInstrumentSpec
from mu_strategy.market_data.service import CandleBundle, refresh_candle_bundle
from mu_strategy.market_data.symbols import resolve_okx_swap_symbol
from mu_strategy.market_data.universe import OKXSwapTicker, top_okx_usdt_swaps
from mu_strategy.market_data.utils import interval_to_ms
from mu_strategy.strategies.registry import baseline_strategy_group


UniverseProvider = Callable[..., list[OKXSwapTicker]]
CandleLoader = Callable[..., CandleBundle]
Scanner = Callable[..., EntryScanResult]
BOT_CLIENT_ORDER_ID_PATTERN = re.compile(r"^OD[A-F0-9]{20}$")
PENDING_ORDER_STATES = {"", "live", "partially_filled"}
DEFAULT_WATCHLIST_SYMBOLS = ("MU-USDT-SWAP",)


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
    max_candle_staleness_bars: int = 3
    watchlist_symbols: tuple[str, ...] = DEFAULT_WATCHLIST_SYMBOLS


def run_once(
    config: DemoTradingConfig | None = None,
    *,
    broker: Any | None,
    universe_provider: UniverseProvider = top_okx_usdt_swaps,
    candle_loader: CandleLoader = refresh_candle_bundle,
    scanner: Scanner = scan_entry,
) -> dict[str, Any]:
    config = config or DemoTradingConfig()
    tickers: list[OKXSwapTicker] = []
    universe_error: dict[str, Any] | None = None
    open_exposure = 0
    open_position_inst_ids: set[str] = set()
    open_order_inst_ids: set[str] = set()
    open_order_rows_by_inst_id: dict[str, list[dict[str, Any]]] = {}
    existing_client_order_ids: set[str] = set()
    account_context: dict[str, Any] = {}
    account_error: dict[str, str] | None = None

    if config.dry_run:
        tickers = _merge_watchlist_tickers(
            universe_provider(limit=config.universe_limit),
            config.watchlist_symbols,
        )
    else:
        if broker is None:
            raise RuntimeError("broker is required when dry_run is false")
        positions = broker.get_positions(inst_type="SWAP")
        open_orders = broker.get_open_orders(inst_type="SWAP")
        account_context = {"positions": positions, "open_orders": open_orders}
        account_error = _account_context_error(account_context)
        if not _okx_response_failed(open_orders):
            open_order_inst_ids = _open_order_inst_ids(open_orders)
            open_order_rows_by_inst_id = _open_order_rows_by_inst_id(open_orders)
            existing_client_order_ids = _client_order_ids(open_orders)
        if account_error is None:
            open_exposure = _count_open_exposure(positions, open_orders)
            open_position_inst_ids = _open_position_inst_ids(positions)
            try:
                tickers = _merge_watchlist_tickers(
                    universe_provider(limit=config.universe_limit),
                    config.watchlist_symbols,
                )
            except Exception as exc:
                universe_error = _universe_load_error(exc)
    entry_eligible_inst_ids = {ticker.inst_id for ticker in tickers}

    scans: list[dict[str, Any]] = []
    scan_results: list[EntryScanResult] = []
    orders: list[dict[str, Any]] = []
    expired_orders: list[dict[str, Any]] = []
    data_errors: list[dict[str, Any]] = []
    remaining_capacity = max(0, config.max_open_positions - open_exposure)

    for ticker in _tickers_to_scan(tickers, open_order_rows_by_inst_id):
        data_error = None
        bundle = None
        try:
            bundle = candle_loader(
                ticker.inst_id,
                intervals=("15m", "1h"),
                days=config.days,
                data_dir=config.data_dir,
                refresh=config.refresh,
            )
        except Exception as exc:
            data_error = _market_data_load_error(ticker.inst_id, exc)
            data_errors.append(data_error)
            result = _data_error_scan_result(ticker.inst_id, data_error)
            scans.append(_data_error_scan_payload(result, data_error, source=ticker.source))

        if bundle is not None and not config.dry_run:
            data_error = _market_data_freshness_error(
                symbol=ticker.inst_id,
                bundle=bundle,
                config=config,
                now_ms=int(time.time() * 1000),
            )
            if data_error is not None:
                data_errors.append(data_error)
                result = _stale_scan_result(ticker.inst_id, bundle, data_error)
                scans.append(_stale_scan_payload(result, bundle, data_error, source=ticker.source))

        if bundle is not None and data_error is None:
            strategy_config = baseline_strategy_group(ticker.inst_id).config
            result = scanner(
                ticker.inst_id,
                bundle.candles_by_interval.get("15m", []),
                bundle.candles_by_interval.get("1h", []),
                config=strategy_config,
            )
            scan_results.append(result)
            scans.append(
                _scan_payload(
                    result,
                    bundle,
                    source=ticker.source,
                    second_pullback_wait_bars=strategy_config.second_pullback_wait_bars,
                )
            )

        if not config.dry_run:
            stale_orders = _expire_stale_limit_orders(
                broker=broker,
                symbol=ticker.inst_id,
                open_order_rows=open_order_rows_by_inst_id.get(ticker.inst_id, []),
                result=result,
            )
            if stale_orders:
                expired_orders.extend(stale_orders)
                successful_expirations = [item for item in stale_orders if item["status"] == "expired"]
                if successful_expirations:
                    expired_client_order_ids = {
                        item["client_order_id"] for item in successful_expirations if item.get("client_order_id")
                    }
                    expired_order_ids = {item["order_id"] for item in successful_expirations if item.get("order_id")}
                    existing_client_order_ids.difference_update(expired_client_order_ids)
                    remaining_rows = [
                        row
                        for row in open_order_rows_by_inst_id.get(ticker.inst_id, [])
                        if str(row.get("clOrdId") or "") not in expired_client_order_ids
                        and str(row.get("ordId") or "") not in expired_order_ids
                    ]
                    if remaining_rows:
                        open_order_rows_by_inst_id[ticker.inst_id] = remaining_rows
                    else:
                        open_order_rows_by_inst_id.pop(ticker.inst_id, None)
                        open_order_inst_ids.discard(ticker.inst_id)
                    open_exposure = max(0, open_exposure - len(successful_expirations))
                    remaining_capacity += len(successful_expirations)
            if data_error is not None:
                continue

    for result in scan_results:
        if result.symbol not in entry_eligible_inst_ids:
            continue
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
        if result.symbol in open_order_inst_ids:
            plan["status"] = "blocked"
            plan["reason"] = "symbol_order_already_open"
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

    mode = "dry_run" if config.dry_run else "live_demo"
    if account_error is not None:
        mode = "blocked"
    payload = {
        "mode": mode,
        "dry_run": config.dry_run,
        "universe": [asdict(ticker) for ticker in tickers],
        "open_exposure": open_exposure,
        "account_context": account_context,
        "scans": scans,
        "orders": orders,
        "expired_orders": expired_orders,
        "universe_error": universe_error,
        "data_errors": data_errors,
    }
    if account_error is not None:
        payload["reason"] = "account_context_error"
        payload["account_error"] = account_error
    return payload


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
        "leverage": config.leverage,
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


def _merge_watchlist_tickers(
    tickers: list[OKXSwapTicker],
    watchlist_symbols: tuple[str, ...],
) -> list[OKXSwapTicker]:
    merged = list(tickers)
    seen = {ticker.inst_id for ticker in merged}
    for symbol in watchlist_symbols:
        inst_id = resolve_okx_swap_symbol(symbol).inst_id
        if inst_id in seen:
            continue
        merged.append(OKXSwapTicker(inst_id=inst_id, last=0.0, volume_ccy_24h=0.0, source="watchlist"))
        seen.add(inst_id)
    return merged


def _scan_payload(
    result: EntryScanResult,
    bundle: CandleBundle,
    *,
    source: str = "top",
    second_pullback_wait_bars: int | None = None,
) -> dict[str, Any]:
    payload = asdict(result)
    payload["source"] = source
    if second_pullback_wait_bars is not None:
        payload["second_pullback_wait_bars"] = second_pullback_wait_bars
    payload["data_files"] = {interval: str(path) for interval, path in bundle.files_by_interval.items()}
    return payload


def _stale_scan_result(symbol: str, bundle: CandleBundle, data_error: dict[str, Any]) -> EntryScanResult:
    return _data_error_scan_result(
        symbol,
        data_error,
        last_close=_latest_close(bundle.candles_by_interval.get("15m", [])),
    )


def _data_error_scan_result(
    symbol: str,
    data_error: dict[str, Any],
    *,
    last_close: float | None = None,
) -> EntryScanResult:
    return EntryScanResult(
        symbol=symbol,
        action="skip",
        reason=data_error["reason"],
        last_close=last_close,
        regime_1h="yellow",
        rsi14=None,
        macd_hist=None,
        macd_hist_prev=None,
    )


def _stale_scan_payload(
    result: EntryScanResult,
    bundle: CandleBundle,
    data_error: dict[str, Any],
    *,
    source: str = "top",
) -> dict[str, Any]:
    payload = _scan_payload(result, bundle, source=source)
    payload["data_error"] = data_error
    return payload


def _data_error_scan_payload(
    result: EntryScanResult,
    data_error: dict[str, Any],
    *,
    source: str = "top",
) -> dict[str, Any]:
    payload = asdict(result)
    payload["source"] = source
    payload["data_files"] = {}
    payload["data_error"] = data_error
    return payload


def _market_data_load_error(symbol: str, exc: Exception) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "reason": "market_data_load_failed",
        "error_type": type(exc).__name__,
        "message": str(exc),
    }


def _universe_load_error(exc: Exception) -> dict[str, Any]:
    return {
        "reason": "universe_load_failed",
        "error_type": type(exc).__name__,
        "message": str(exc),
    }


def _market_data_freshness_error(
    *,
    symbol: str,
    bundle: CandleBundle,
    config: DemoTradingConfig,
    now_ms: int,
) -> dict[str, Any] | None:
    status_error = _market_data_status_error(symbol=symbol, bundle=bundle)
    if status_error is not None:
        return status_error
    max_staleness_bars = max(1, config.max_candle_staleness_bars)
    for interval, candles in bundle.candles_by_interval.items():
        if not candles:
            return {
                "symbol": symbol,
                "reason": "market_data_missing",
                "interval": interval,
                "latest_open_time_ms": None,
                "age_ms": None,
                "max_age_ms": interval_to_ms(interval) * max_staleness_bars,
            }
        latest_open_time_ms = max(candle.open_time_ms for candle in candles)
        max_age_ms = interval_to_ms(interval) * max_staleness_bars
        age_ms = now_ms - latest_open_time_ms
        if age_ms > max_age_ms:
            return {
                "symbol": symbol,
                "reason": "market_data_stale",
                "interval": interval,
                "latest_open_time_ms": latest_open_time_ms,
                "age_ms": age_ms,
                "max_age_ms": max_age_ms,
            }
    return None


def _market_data_status_error(*, symbol: str, bundle: CandleBundle) -> dict[str, Any] | None:
    statuses = getattr(bundle, "statuses_by_interval", None) or {}
    for interval, status in statuses.items():
        is_valid = bool(getattr(status, "is_valid", True))
        is_stale = bool(getattr(status, "is_stale", False))
        if is_valid and not is_stale:
            continue
        return {
            "symbol": symbol,
            "reason": "market_data_invalid" if not is_valid else "market_data_cache_stale",
            "interval": interval,
            "status_reason": getattr(status, "reason", None),
            "error_type": getattr(status, "error_type", None),
            "message": getattr(status, "message", None),
            "latest_open_time_ms": getattr(status, "last_timestamp_ms", None),
            "source_file": str(getattr(status, "source_file", "")),
        }
    return None


def _latest_close(candles: list) -> float | None:
    if not candles:
        return None
    latest = max(candles, key=lambda candle: candle.open_time_ms)
    return latest.close


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


def _open_order_inst_ids(open_orders: dict[str, Any]) -> set[str]:
    return {str(row.get("instId")) for row in open_orders.get("data") or [] if row.get("instId")}


def _open_order_rows_by_inst_id(open_orders: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    rows_by_inst_id: dict[str, list[dict[str, Any]]] = {}
    for row in open_orders.get("data") or []:
        if not isinstance(row, dict) or not row.get("instId"):
            continue
        rows_by_inst_id.setdefault(str(row["instId"]), []).append(row)
    return rows_by_inst_id


def _client_order_ids(open_orders: dict[str, Any]) -> set[str]:
    return {str(row.get("clOrdId")) for row in open_orders.get("data") or [] if row.get("clOrdId")}


def _tickers_to_scan(
    tickers: list[OKXSwapTicker],
    open_order_rows_by_inst_id: dict[str, list[dict[str, Any]]],
) -> list[OKXSwapTicker]:
    scan_tickers = list(tickers)
    seen = {ticker.inst_id for ticker in scan_tickers}
    for inst_id, rows in open_order_rows_by_inst_id.items():
        if inst_id in seen or not any(_is_bot_fib_limit_order(row) for row in rows):
            continue
        scan_tickers.append(OKXSwapTicker(inst_id=inst_id, last=0.0, volume_ccy_24h=0.0, source="open_order"))
        seen.add(inst_id)
    return scan_tickers


def _expire_stale_limit_orders(
    *,
    broker: Any,
    symbol: str,
    open_order_rows: list[dict[str, Any]],
    result: EntryScanResult,
) -> list[dict[str, Any]]:
    expired_orders = []
    for row in open_order_rows:
        if not _is_bot_fib_limit_order(row):
            continue
        client_order_id = str(row.get("clOrdId") or "")
        if _matches_active_signal_order(client_order_id, result):
            continue
        order_id = _text_or_none(row.get("ordId"))
        response = broker.cancel_order(
            inst_id=symbol,
            order_id=order_id,
            client_order_id=client_order_id,
            confirm_demo_order=True,
        )
        expired_orders.append(
            {
                "symbol": symbol,
                "status": "expire_failed" if _okx_response_failed(response) else "expired",
                "reason": _stale_order_reason(result),
                "order_id": order_id,
                "client_order_id": client_order_id,
                "response": response,
            }
        )
    return expired_orders


def _is_bot_fib_limit_order(row: dict[str, Any]) -> bool:
    client_order_id = str(row.get("clOrdId") or "")
    if not BOT_CLIENT_ORDER_ID_PATTERN.fullmatch(client_order_id):
        return False
    state = str(row.get("state") or "").lower()
    if state not in PENDING_ORDER_STATES:
        return False
    order_type = str(row.get("ordType") or "limit").lower()
    side = str(row.get("side") or "buy").lower()
    return order_type == "limit" and side == "buy"


def _matches_active_signal_order(client_order_id: str, result: EntryScanResult) -> bool:
    if result.action != "enter" or result.trigger_price is None:
        return False
    return client_order_id == generate_client_order_id(result.symbol, result.signal_time_ms, result.trigger_price)


def _stale_order_reason(result: EntryScanResult) -> str:
    if result.action == "enter":
        return "superseded_by_current_signal"
    return result.reason


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


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

