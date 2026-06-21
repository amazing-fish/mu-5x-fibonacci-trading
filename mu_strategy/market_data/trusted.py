from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from mu_strategy.market_data.cache import (
    read_csv,
    validate_close_to_next_open_gaps,
    write_csv,
)
from mu_strategy.market_data.providers.okx import fetch_okx_historical, fetch_okx_incremental
from mu_strategy.market_data.symbols import resolve_okx_swap_symbol
from mu_strategy.market_data.universe import OKXSwapTicker, fetch_okx_swap_tickers, select_top_okx_usdt_swaps
from mu_strategy.market_data.utils import dedupe_candles, interval_to_ms
from mu_strategy.models import Candle


DEFAULT_INTERVALS = ("5m", "15m", "1h")
DEFAULT_STOCK_TOKEN_CONFIG = Path("config/okx_stock_tokens.json")
DEFAULT_LIVE_DATA_DIR = Path("data/live")


@dataclass(frozen=True)
class CandleValidationResult:
    ok: bool
    reason: str = "ok"
    missing_in_built: list[int] = field(default_factory=list)
    missing_in_native: list[int] = field(default_factory=list)
    misaligned_timestamps: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DataStatus:
    symbol: str
    interval: str
    rows: int
    first_timestamp_ms: int | None
    last_timestamp_ms: int | None
    updated_at_ms: int
    source_file: Path
    is_valid: bool = True
    is_stale: bool = False
    reason: str = "ok"
    error_type: str | None = None
    message: str | None = None
    warnings: tuple[str, ...] = ()
    validation: CandleValidationResult | None = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["source_file"] = str(self.source_file)
        if self.validation is not None:
            payload["validation"] = self.validation.to_dict()
        return payload


OKXHistoryFetcher = Callable[[str, str], list[Candle]]


def trusted_cache_path(symbol: str, interval: str, *, data_dir: Path = DEFAULT_LIVE_DATA_DIR) -> Path:
    return Path(data_dir) / "okx" / symbol / f"{interval}.csv"


def load_stock_token_inst_ids(config_path: Path = DEFAULT_STOCK_TOKEN_CONFIG) -> set[str]:
    values = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if not isinstance(values, list):
        raise ValueError("stock token config must be a JSON array")
    return {resolve_okx_swap_symbol(str(value)).inst_id for value in values}


def select_top_okx_crypto_swaps(
    rows: list[dict],
    *,
    stock_token_inst_ids: set[str],
    limit: int,
) -> list[OKXSwapTicker]:
    filtered_rows = [row for row in rows if str(row.get("instId") or "") not in stock_token_inst_ids]
    return select_top_okx_usdt_swaps(filtered_rows, limit=limit)


def select_top_okx_stock_tokens(
    rows: list[dict],
    *,
    stock_token_inst_ids: set[str],
    limit: int,
) -> list[OKXSwapTicker]:
    selected = []
    for ticker in select_top_okx_usdt_swaps(rows, limit=len(rows)):
        if ticker.inst_id in stock_token_inst_ids:
            selected.append(
                OKXSwapTicker(
                    inst_id=ticker.inst_id,
                    last=ticker.last,
                    volume_ccy_24h=ticker.volume_ccy_24h,
                    source="stock_token",
                )
            )
        if len(selected) >= limit:
            break
    return selected


def refresh_trusted_interval(
    symbol: str,
    interval: str,
    *,
    days: int,
    data_dir: Path = DEFAULT_LIVE_DATA_DIR,
    now_ms: int | None = None,
    fetcher: Callable[..., list[Candle]] | None = None,
) -> DataStatus:
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    path = trusted_cache_path(symbol, interval, data_dir=data_dir)
    existing: list[Candle] = []
    cache_loaded = False
    fetcher = fetcher or fetch_okx_historical
    try:
        existing = read_csv(path) if path.exists() else []
        cache_loaded = True
        if existing:
            since_time_ms = existing[-2].open_time_ms if len(existing) >= 2 else existing[0].open_time_ms
            fetched = fetch_okx_incremental(symbol, interval, since_time_ms=since_time_ms)
            candles = _merge_trusted_candles(existing, fetched, days=days)
        else:
            candles = fetcher(symbol, interval, days=days)
            candles = _merge_trusted_candles([], candles, days=days)
        validate_close_to_next_open_gaps(candles)
        write_csv(candles, path)
        return _status_from_candles(
            symbol=symbol,
            interval=interval,
            candles=candles,
            path=path,
            updated_at_ms=now_ms,
        )
    except Exception as exc:
        candles = _merge_trusted_candles([], existing, days=days)
        reason = "incremental_refresh_failed" if existing else "refresh_failed"
        if path.exists() and not cache_loaded:
            reason = "cache_read_failed"
            candles = []
        return _status_from_candles(
            symbol=symbol,
            interval=interval,
            candles=candles,
            path=path,
            updated_at_ms=now_ms,
            is_valid=False,
            is_stale=bool(candles),
            reason=reason,
            error_type=type(exc).__name__,
            message=str(exc),
        )


def refresh_trusted_symbol_statuses(
    symbol: str,
    *,
    intervals: tuple[str, ...] = DEFAULT_INTERVALS,
    days: int,
    data_dir: Path = DEFAULT_LIVE_DATA_DIR,
    now_ms: int | None = None,
    fetcher: Callable[..., list[Candle]] | None = None,
) -> dict[str, DataStatus]:
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    interval_statuses = {}
    for interval in intervals:
        interval_statuses[interval] = refresh_trusted_interval(
            symbol,
            interval,
            days=days,
            data_dir=data_dir,
            now_ms=now_ms,
            fetcher=fetcher,
        )
    _attach_built_native_validation(interval_statuses, data_dir=data_dir)
    return interval_statuses


def refresh_market_data_once(
    *,
    data_dir: Path = DEFAULT_LIVE_DATA_DIR,
    stock_token_inst_ids: set[str] | None = None,
    stock_token_config: Path = DEFAULT_STOCK_TOKEN_CONFIG,
    ticker_rows: list[dict] | None = None,
    limit: int = 10,
    days: int = 180,
    intervals: tuple[str, ...] = DEFAULT_INTERVALS,
    fetcher: Callable[..., list[Candle]] | None = None,
    now_ms: int | None = None,
) -> dict:
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    rows = ticker_rows if ticker_rows is not None else fetch_okx_swap_tickers()
    stock_ids = stock_token_inst_ids if stock_token_inst_ids is not None else load_stock_token_inst_ids(stock_token_config)
    crypto_top = select_top_okx_crypto_swaps(rows, stock_token_inst_ids=stock_ids, limit=limit)
    stock_top = select_top_okx_stock_tokens(rows, stock_token_inst_ids=stock_ids, limit=limit)
    symbols = _dedupe_tickers([*crypto_top, *stock_top])

    manifest: dict = {
        "status": "ok",
        "updated_at_ms": now_ms,
        "data_dir": str(data_dir),
        "intervals": list(intervals),
        "universes": {
            "crypto_top": [_ticker_dict(ticker) for ticker in crypto_top],
            "stock_token_top": [_ticker_dict(ticker) for ticker in stock_top],
        },
        "symbols": {},
        "warnings": [],
    }
    if len(stock_top) < limit:
        manifest["warnings"].append(f"stock_token_top_count_below_limit:{len(stock_top)}/{limit}")

    for ticker in symbols:
        interval_statuses = refresh_trusted_symbol_statuses(
            ticker.inst_id,
            intervals=intervals,
            days=days,
            data_dir=data_dir,
            now_ms=now_ms,
            fetcher=fetcher,
        )
        manifest["symbols"][ticker.inst_id] = {
            "source": ticker.source,
            "last": ticker.last,
            "volume_ccy_24h": ticker.volume_ccy_24h,
            "intervals": {interval: status.to_dict() for interval, status in interval_statuses.items()},
        }

    manifest["status"] = _manifest_status(manifest)
    _write_manifest(manifest, data_dir=data_dir)
    _append_run_log(manifest, data_dir=data_dir)
    return manifest


def aggregate_candles(candles: list[Candle], *, interval: str, base_interval: str = "5m") -> list[Candle]:
    target_ms = interval_to_ms(interval)
    base_ms = interval_to_ms(base_interval)
    expected = target_ms // base_ms
    if expected <= 0 or target_ms % base_ms != 0:
        raise ValueError(f"{base_interval} cannot build {interval}")
    groups: dict[int, list[Candle]] = {}
    for candle in dedupe_candles(candles):
        bucket = candle.open_time_ms - (candle.open_time_ms % target_ms)
        groups.setdefault(bucket, []).append(candle)
    output = []
    for timestamp, rows in sorted(groups.items()):
        rows = dedupe_candles(rows)
        if len(rows) != expected:
            continue
        output.append(
            Candle(
                timestamp,
                rows[0].open,
                max(row.high for row in rows),
                min(row.low for row in rows),
                rows[-1].close,
                sum(row.volume for row in rows),
            )
        )
    return output


def validate_built_native_candles(
    built: list[Candle],
    native: list[Candle],
    *,
    interval: str,
    min_samples: int = 1,
) -> CandleValidationResult:
    if not built:
        return CandleValidationResult(False, "built_empty")
    if not native:
        return CandleValidationResult(False, "native_empty")
    if len(built) < min_samples:
        return CandleValidationResult(False, "built_sample_count_below_minimum")
    if len(native) < min_samples:
        return CandleValidationResult(False, "native_sample_count_below_minimum")

    interval_ms = interval_to_ms(interval)
    timestamps = sorted({bar.open_time_ms for bar in [*built, *native]})
    misaligned = [timestamp for timestamp in timestamps if timestamp % interval_ms != 0]
    if misaligned:
        return CandleValidationResult(False, "timestamp_misaligned", misaligned_timestamps=misaligned)

    built_times = {bar.open_time_ms for bar in built}
    native_times = {bar.open_time_ms for bar in native}
    missing_in_built = sorted(native_times - built_times)
    if missing_in_built:
        return CandleValidationResult(False, "missing_in_built", missing_in_built=missing_in_built)
    missing_in_native = sorted(built_times - native_times)
    if missing_in_native:
        return CandleValidationResult(False, "missing_in_native", missing_in_native=missing_in_native)
    return CandleValidationResult(True, "ok")


def _attach_built_native_validation(interval_statuses: dict[str, DataStatus], *, data_dir: Path) -> None:
    five_minute = interval_statuses.get("5m")
    if five_minute is None or not five_minute.is_valid or not five_minute.source_file.exists():
        return
    five_minute_candles = read_csv(five_minute.source_file)
    for interval in ("15m", "1h"):
        native_status = interval_statuses.get(interval)
        if native_status is None or not native_status.is_valid or not native_status.source_file.exists():
            continue
        built = aggregate_candles(five_minute_candles, interval=interval)
        native = read_csv(native_status.source_file)
        validation = validate_built_native_candles(built, native, interval=interval)
        if not validation.ok:
            interval_statuses[interval] = DataStatus(
                symbol=native_status.symbol,
                interval=native_status.interval,
                rows=native_status.rows,
                first_timestamp_ms=native_status.first_timestamp_ms,
                last_timestamp_ms=native_status.last_timestamp_ms,
                updated_at_ms=native_status.updated_at_ms,
                source_file=native_status.source_file,
                is_valid=False,
                is_stale=native_status.is_stale,
                reason=validation.reason,
                error_type=native_status.error_type,
                message=native_status.message,
                warnings=native_status.warnings,
                validation=validation,
            )
        else:
            interval_statuses[interval] = DataStatus(
                symbol=native_status.symbol,
                interval=native_status.interval,
                rows=native_status.rows,
                first_timestamp_ms=native_status.first_timestamp_ms,
                last_timestamp_ms=native_status.last_timestamp_ms,
                updated_at_ms=native_status.updated_at_ms,
                source_file=native_status.source_file,
                is_valid=native_status.is_valid,
                is_stale=native_status.is_stale,
                reason=native_status.reason,
                error_type=native_status.error_type,
                message=native_status.message,
                warnings=native_status.warnings,
                validation=validation,
            )


def _merge_trusted_candles(existing: list[Candle], fetched: list[Candle], *, days: int) -> list[Candle]:
    candles = dedupe_candles([*existing, *fetched])
    if not candles:
        return []
    end_time_ms = max(candle.open_time_ms for candle in candles)
    start_time_ms = end_time_ms - days * 86_400_000
    return [candle for candle in candles if start_time_ms <= candle.open_time_ms <= end_time_ms]


def _status_from_candles(
    *,
    symbol: str,
    interval: str,
    candles: list[Candle],
    path: Path,
    updated_at_ms: int,
    is_valid: bool = True,
    is_stale: bool = False,
    reason: str = "ok",
    error_type: str | None = None,
    message: str | None = None,
) -> DataStatus:
    rows = len(candles)
    return DataStatus(
        symbol=symbol,
        interval=interval,
        rows=rows,
        first_timestamp_ms=candles[0].open_time_ms if candles else None,
        last_timestamp_ms=candles[-1].open_time_ms if candles else None,
        updated_at_ms=updated_at_ms,
        source_file=path,
        is_valid=is_valid and rows > 0,
        is_stale=is_stale,
        reason=reason if rows or reason != "ok" else "empty",
        error_type=error_type,
        message=message,
    )


def _write_manifest(manifest: dict, *, data_dir: Path) -> Path:
    path = Path(data_dir) / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _append_run_log(manifest: dict, *, data_dir: Path) -> Path:
    path = Path(data_dir) / "refresh_runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_run_log_payload(manifest), ensure_ascii=False, sort_keys=True))
        handle.write("\n")
    return path


def _run_log_payload(manifest: dict) -> dict:
    symbol_count = len(manifest.get("symbols") or {})
    invalid_count = 0
    for symbol in (manifest.get("symbols") or {}).values():
        for status in (symbol.get("intervals") or {}).values():
            if not status.get("is_valid"):
                invalid_count += 1
    return {
        "status": manifest.get("status"),
        "updated_at_ms": manifest.get("updated_at_ms"),
        "symbol_count": symbol_count,
        "invalid_count": invalid_count,
        "warnings": manifest.get("warnings") or [],
    }


def _manifest_status(manifest: dict) -> str:
    has_stale = False
    for symbol in (manifest.get("symbols") or {}).values():
        for status in (symbol.get("intervals") or {}).values():
            if not status.get("is_valid"):
                return "invalid"
            if status.get("is_stale"):
                has_stale = True
    return "stale" if has_stale else "ok"


def _dedupe_tickers(tickers: Iterable[OKXSwapTicker]) -> list[OKXSwapTicker]:
    by_symbol: dict[str, OKXSwapTicker] = {}
    for ticker in tickers:
        by_symbol.setdefault(ticker.inst_id, ticker)
    return list(by_symbol.values())


def _ticker_dict(ticker: OKXSwapTicker) -> dict:
    return {
        "inst_id": ticker.inst_id,
        "last": ticker.last,
        "volume_ccy_24h": ticker.volume_ccy_24h,
        "source": ticker.source,
    }
