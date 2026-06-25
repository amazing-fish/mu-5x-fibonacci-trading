from __future__ import annotations

from pathlib import Path

from mu_strategy.market_data.cache import cached_historical
from mu_strategy.market_data.symbols import ResolvedSymbol, resolve_okx_swap_symbol
from mu_strategy.market_data.trusted_data.compat import (
    CandleBundle,
    candle_bundle_from_trusted_bundle,
)
from mu_strategy.market_data.trusted_data.contracts import (
    Clock,
    TrustedConsumerRefreshError,
    TrustedLoadContext,
)
from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
from mu_strategy.market_data.trusted_data.policy import FreshnessPolicy, TrustPolicy, research_strict_policy
from mu_strategy.market_data.trusted_data.refresh import (
    DEFAULT_LIVE_DATA_DIR,
)
from mu_strategy.market_data.trusted_data.store import TrustedDataStore


TRUSTED_CONSUMER_REFRESH_ERROR = (
    "trusted bundle loading is cache-only; run "
    "python -m mu_strategy.commands.refresh_market_data "
    "before loading trusted data"
)


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
    data_dir: Path = DEFAULT_LIVE_DATA_DIR,
    refresh: bool = False,
    policy: TrustPolicy | None = None,
    freshness_policy: FreshnessPolicy | None = None,
    max_staleness_bars: int = 3,
    clock: Clock | None = None,
    context: TrustedLoadContext | None = None,
) -> CandleBundle:
    if refresh:
        raise TrustedConsumerRefreshError(TRUSTED_CONSUMER_REFRESH_ERROR)
    resolved = resolve_okx_swap_symbol(symbol)
    store = TrustedDataStore(data_dir=Path(data_dir))
    requested_intervals = tuple(dict.fromkeys(intervals))
    resolved_freshness_policy = freshness_policy or FreshnessPolicy(max_staleness_bars=max_staleness_bars)
    bundle = LoadTrustedBundle(store, clock=clock, freshness_policy=resolved_freshness_policy).execute(
        LoadTrustedBundleQuery(
            resolved.inst_id,
            intervals=requested_intervals,
            days=days,
        ),
        policy or research_strict_policy(),
        context=context,
    )
    return candle_bundle_from_trusted_bundle(resolved, bundle)
