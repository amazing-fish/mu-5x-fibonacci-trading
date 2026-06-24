from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from mu_strategy.market_data.trusted_data.contracts import DatasetHealth, TrustedConsumerRefreshError, ValidationReport
from mu_strategy.market_data.trusted_data.refresh import (
    DEFAULT_INTERVALS,
    DEFAULT_LIVE_DATA_DIR,
    DEFAULT_STOCK_TOKEN_CONFIG,
    OKXHistoryFetcher,
    OKXIncrementalFetcher,
    RefreshTrustedMarketDataRequest,
    load_stock_token_inst_ids,
    refresh_with_okx_provider,
    select_top_okx_crypto_swaps,
    select_top_okx_stock_tokens,
)
from mu_strategy.market_data.trusted_data.store import TrustedDataStore
from mu_strategy.market_data.trusted_data.validation import (
    aggregate_candles,
    validate_built_native_candles as _validate_built_native_candles,
)
from mu_strategy.models import Candle


TRUSTED_REQUIRED_INTERVALS = DEFAULT_INTERVALS
PER_SYMBOL_REFRESH_ERROR = (
    "trusted per-symbol refresh APIs are deprecated; run "
    "python -m mu_strategy.commands.refresh_market_data for canonical refresh, "
    "then use LoadTrustedBundle or refresh_trusted_candle_bundle for cache-only load"
)


@dataclass(frozen=True)
class CandleValidationResult:
    ok: bool
    reason: str = "ok"
    missing_in_built: list[int] = field(default_factory=list)
    missing_in_native: list[int] = field(default_factory=list)
    misaligned_timestamps: list[int] = field(default_factory=list)
    timestamp_gaps: list[dict[str, int]] = field(default_factory=list)
    value_mismatches: list[dict[str, int | float | str]] = field(default_factory=list)

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


def trusted_cache_path(symbol: str, interval: str, *, data_dir: Path = DEFAULT_LIVE_DATA_DIR) -> Path:
    return TrustedDataStore(data_dir=Path(data_dir)).cache_path(symbol, interval)


def refresh_trusted_interval(
    symbol: str,
    interval: str,
    *,
    days: int,
    data_dir: Path = DEFAULT_LIVE_DATA_DIR,
    now_ms: int | None = None,
    fetcher: OKXHistoryFetcher | None = None,
    incremental_fetcher: OKXIncrementalFetcher | None = None,
) -> DataStatus:
    raise TrustedConsumerRefreshError(PER_SYMBOL_REFRESH_ERROR)


def refresh_trusted_symbol_statuses(
    symbol: str,
    *,
    intervals: tuple[str, ...] = DEFAULT_INTERVALS,
    days: int,
    data_dir: Path = DEFAULT_LIVE_DATA_DIR,
    now_ms: int | None = None,
    fetcher: OKXHistoryFetcher | None = None,
    incremental_fetcher: OKXIncrementalFetcher | None = None,
) -> dict[str, DataStatus]:
    raise TrustedConsumerRefreshError(PER_SYMBOL_REFRESH_ERROR)


def refresh_market_data_once(
    *,
    data_dir: Path = DEFAULT_LIVE_DATA_DIR,
    stock_token_inst_ids: set[str] | None = None,
    stock_token_config: Path = DEFAULT_STOCK_TOKEN_CONFIG,
    ticker_rows: list[dict] | None = None,
    limit: int = 10,
    days: int = 180,
    intervals: tuple[str, ...] = DEFAULT_INTERVALS,
    fetcher: OKXHistoryFetcher | None = None,
    incremental_fetcher: OKXIncrementalFetcher | None = None,
    now_ms: int | None = None,
) -> dict:
    run = refresh_with_okx_provider(
        TrustedDataStore(data_dir=Path(data_dir)),
        RefreshTrustedMarketDataRequest(
            requested_intervals=intervals,
            days=days,
            limit=limit,
            stock_token_config=stock_token_config,
            stock_token_inst_ids=stock_token_inst_ids,
            now_ms=now_ms,
        ),
        ticker_rows=ticker_rows,
        history_fetcher=fetcher,
        incremental_fetcher=incremental_fetcher,
        history_days_fallback=days,
    )
    return run.to_manifest()


def validate_built_native_candles(
    built: list[Candle],
    native: list[Candle],
    *,
    interval: str,
    min_samples: int = 1,
    value_rel_tol: float = 1e-8,
    value_abs_tol: float = 1e-8,
    max_value_mismatches: int = 20,
) -> CandleValidationResult:
    report = _validate_built_native_candles(
        built,
        native,
        interval=interval,
        min_samples=min_samples,
        value_rel_tol=value_rel_tol,
        value_abs_tol=value_abs_tol,
        max_value_mismatches=max_value_mismatches,
    )
    return _validation_result_from_report(report)


def _data_status_from_health(health: DatasetHealth) -> DataStatus:
    payload = health.to_dict()
    return DataStatus(
        symbol=health.key.symbol,
        interval=health.key.interval,
        rows=health.rows,
        first_timestamp_ms=health.first_timestamp_ms,
        last_timestamp_ms=health.last_timestamp_ms,
        updated_at_ms=health.updated_at_ms,
        source_file=health.source_file,
        is_valid=bool(payload["is_valid"]),
        is_stale=bool(payload["is_stale"]),
        reason=str(payload["reason"]),
        error_type=health.error_type,
        message=health.message,
        warnings=health.warnings,
        validation=_validation_result_from_report(health.validation) if health.validation else None,
    )


def _validation_result_from_report(report: ValidationReport) -> CandleValidationResult:
    return CandleValidationResult(
        ok=report.ok,
        reason=report.reason.value,
        missing_in_built=list(report.missing_in_built),
        missing_in_native=list(report.missing_in_native),
        misaligned_timestamps=list(report.misaligned_timestamps),
        timestamp_gaps=[dict(value) for value in report.timestamp_gaps],
        value_mismatches=[dict(value) for value in report.value_mismatches],
    )


def _dedupe_tickers(tickers: Iterable) -> list:
    by_symbol = {}
    for ticker in tickers:
        by_symbol.setdefault(ticker.inst_id, ticker)
    return list(by_symbol.values())
