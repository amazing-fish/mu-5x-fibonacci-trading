from __future__ import annotations

from pathlib import Path

from mu_strategy.market_data.trusted_data.compat import (
    CandleValidationResult,
    DataStatus,
    validation_result_from_report,
)
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


def trusted_cache_path(symbol: str, interval: str, *, data_dir: Path = DEFAULT_LIVE_DATA_DIR) -> Path:
    return TrustedDataStore(data_dir=Path(data_dir)).cache_path(symbol, interval)


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
    return validation_result_from_report(report)
