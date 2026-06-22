from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from mu_strategy.market_data.cache import cached_historical, read_csv, validate_close_to_next_open_gaps
from mu_strategy.market_data.symbols import ResolvedSymbol, resolve_okx_swap_symbol
from mu_strategy.market_data.trusted import (
    DataStatus,
    aggregate_candles,
    refresh_trusted_symbol_statuses,
    trusted_cache_path,
    validate_built_native_candles,
)
from mu_strategy.models import Candle


TRUSTED_REQUIRED_INTERVALS = ("5m", "15m", "1h")


@dataclass(frozen=True)
class CandleBundle:
    symbol: ResolvedSymbol
    candles_by_interval: dict[str, list[Candle]]
    files_by_interval: dict[str, Path]
    days: int
    statuses_by_interval: dict[str, DataStatus] = field(default_factory=dict)


def refresh_candle_bundle(
    symbol: str,
    *,
    intervals: tuple[str, ...] = ("15m", "1h"),
    days: int = 28,
    data_dir: Path = Path("data"),
    refresh: bool = False,
    source: str = "okx",
) -> CandleBundle:
    if source == "okx":
        resolved = resolve_okx_swap_symbol(symbol)
        fetch_symbol = resolved.inst_id
    elif source == "binance":
        resolved = ResolvedSymbol(requested=symbol, inst_id=symbol, source=source)
        fetch_symbol = symbol
    else:
        raise ValueError(f"unsupported data source: {source}")
    candles_by_interval: dict[str, list[Candle]] = {}
    files_by_interval: dict[str, Path] = {}
    for interval in intervals:
        candles, path = cached_historical(
            fetch_symbol,
            interval,
            days=days,
            data_dir=data_dir,
            refresh=refresh,
            source=source,
        )
        candles_by_interval[interval] = candles
        files_by_interval[interval] = path
    return CandleBundle(
        symbol=resolved,
        candles_by_interval=candles_by_interval,
        files_by_interval=files_by_interval,
        days=days,
    )


def refresh_trusted_candle_bundle(
    symbol: str,
    *,
    intervals: tuple[str, ...] = ("15m", "1h"),
    days: int = 28,
    data_dir: Path = Path("data/live"),
    refresh: bool = False,
    fetcher: Callable[..., list[Candle]] | None = None,
) -> CandleBundle:
    resolved = resolve_okx_swap_symbol(symbol)
    candles_by_interval: dict[str, list[Candle]] = {}
    files_by_interval: dict[str, Path] = {}
    requested_intervals = tuple(dict.fromkeys(intervals))
    validation_intervals = _validation_intervals(requested_intervals)
    if refresh:
        statuses_by_interval = refresh_trusted_symbol_statuses(
            resolved.inst_id,
            intervals=validation_intervals,
            days=days,
            data_dir=data_dir,
            fetcher=fetcher,
        )
    else:
        statuses_by_interval, cached_candles = _load_trusted_cache_statuses(
            resolved.inst_id,
            intervals=validation_intervals,
            data_dir=data_dir,
        )

    for interval in requested_intervals:
        status = statuses_by_interval[interval]
        files_by_interval[interval] = status.source_file
        if not status.is_valid:
            candles_by_interval[interval] = []
        elif refresh:
            candles_by_interval[interval] = read_csv(status.source_file)
        else:
            candles_by_interval[interval] = cached_candles[interval]
    return CandleBundle(
        symbol=resolved,
        candles_by_interval=candles_by_interval,
        files_by_interval=files_by_interval,
        days=days,
        statuses_by_interval=statuses_by_interval,
    )


def trusted_status_error(
    statuses: dict[str, DataStatus],
    *,
    required_intervals: tuple[str, ...] = TRUSTED_REQUIRED_INTERVALS,
) -> str | None:
    for interval in required_intervals:
        status = statuses.get(interval)
        if status is None:
            return f"trusted data status missing for {interval}"
        if not status.is_valid:
            return f"trusted data invalid for {interval}: {status.reason}"
        if status.is_stale:
            return f"trusted data stale for {interval}: {status.reason}"
    return None


def _validation_intervals(intervals: tuple[str, ...]) -> tuple[str, ...]:
    if any(interval in {"15m", "1h"} for interval in intervals):
        return tuple(dict.fromkeys(("5m", *intervals)))
    return intervals


def _load_trusted_cache_statuses(
    symbol: str,
    *,
    intervals: tuple[str, ...],
    data_dir: Path,
) -> tuple[dict[str, DataStatus], dict[str, list[Candle]]]:
    statuses: dict[str, DataStatus] = {}
    candles_by_interval: dict[str, list[Candle]] = {}
    manifest_statuses = _load_manifest_interval_statuses(symbol, intervals=intervals, data_dir=data_dir)
    for interval in intervals:
        path = trusted_cache_path(symbol, interval, data_dir=data_dir)
        try:
            candles = read_csv(path) if path.exists() else []
            validate_close_to_next_open_gaps(candles)
            candles_by_interval[interval] = candles
            cache_status = _cache_status(symbol, interval, candles, path)
            statuses[interval] = (manifest_statuses.get(interval) or cache_status) if cache_status.is_valid else cache_status
        except Exception as exc:
            candles_by_interval[interval] = []
            statuses[interval] = DataStatus(
                symbol=symbol,
                interval=interval,
                rows=0,
                first_timestamp_ms=None,
                last_timestamp_ms=None,
                updated_at_ms=0,
                source_file=path,
                is_valid=False,
                reason="cache_read_failed",
                error_type=type(exc).__name__,
                message=str(exc),
            )
    _attach_cached_built_native_validation(statuses, candles_by_interval)
    return statuses, candles_by_interval


def _load_manifest_interval_statuses(
    symbol: str,
    *,
    intervals: tuple[str, ...],
    data_dir: Path,
) -> dict[str, DataStatus]:
    path = Path(data_dir) / "manifest.json"
    if not path.exists():
        return {}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    interval_payloads = (
        (manifest.get("symbols") or {})
        .get(symbol, {})
        .get("intervals", {})
    )
    statuses: dict[str, DataStatus] = {}
    for interval in intervals:
        payload = interval_payloads.get(interval)
        if not isinstance(payload, dict):
            continue
        statuses[interval] = DataStatus(
            symbol=str(payload.get("symbol") or symbol),
            interval=str(payload.get("interval") or interval),
            rows=int(payload.get("rows") or 0),
            first_timestamp_ms=payload.get("first_timestamp_ms"),
            last_timestamp_ms=payload.get("last_timestamp_ms"),
            updated_at_ms=int(payload.get("updated_at_ms") or 0),
            source_file=Path(payload.get("source_file") or trusted_cache_path(symbol, interval, data_dir=data_dir)),
            is_valid=bool(payload.get("is_valid", True)),
            is_stale=bool(payload.get("is_stale")),
            reason=str(payload.get("reason") or "ok"),
            error_type=payload.get("error_type"),
            message=payload.get("message"),
            warnings=tuple(payload.get("warnings") or ()),
        )
    return statuses


def _cache_status(symbol: str, interval: str, candles: list[Candle], path: Path) -> DataStatus:
    rows = len(candles)
    reason = "ok"
    if not path.exists():
        reason = "cache_missing"
    elif not candles:
        reason = "empty"
    return DataStatus(
        symbol=symbol,
        interval=interval,
        rows=rows,
        first_timestamp_ms=candles[0].open_time_ms if candles else None,
        last_timestamp_ms=candles[-1].open_time_ms if candles else None,
        updated_at_ms=0,
        source_file=path,
        is_valid=rows > 0,
        reason=reason,
    )


def _attach_cached_built_native_validation(
    statuses: dict[str, DataStatus],
    candles_by_interval: dict[str, list[Candle]],
) -> None:
    five_minute_status = statuses.get("5m")
    if five_minute_status is None or not five_minute_status.is_valid:
        return
    five_minute = candles_by_interval.get("5m") or []
    for interval in ("15m", "1h"):
        native_status = statuses.get(interval)
        if native_status is None or not native_status.is_valid:
            continue
        validation = validate_built_native_candles(
            aggregate_candles(five_minute, interval=interval),
            candles_by_interval.get(interval) or [],
            interval=interval,
        )
        statuses[interval] = replace(
            native_status,
            is_valid=native_status.is_valid and validation.ok,
            reason=native_status.reason if validation.ok else validation.reason,
            validation=validation,
        )
