from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from mu_strategy.market_data.cache import cached_historical
from mu_strategy.market_data.symbols import ResolvedSymbol, resolve_okx_swap_symbol
from mu_strategy.market_data.trusted import (
    DataStatus,
    OKXHistoryFetcher,
    OKXIncrementalFetcher,
    refresh_trusted_symbol_statuses,
)
from mu_strategy.market_data.trusted_data.contracts import DatasetHealth, TrustDecision, UniverseSnapshot
from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle, LoadTrustedBundleQuery
from mu_strategy.market_data.trusted_data.policy import TrustPolicy, research_strict_policy
from mu_strategy.market_data.trusted_data.refresh import (
    DEFAULT_LIVE_DATA_DIR,
    DEFAULT_INTERVALS,
    RefreshTrustedMarketData,
    RefreshTrustedMarketDataRequest,
)
from mu_strategy.market_data.trusted_data.store import TrustedDataStore
from mu_strategy.models import Candle


TRUSTED_REQUIRED_INTERVALS = DEFAULT_INTERVALS


@dataclass(frozen=True)
class CandleBundle:
    symbol: ResolvedSymbol
    candles_by_interval: dict[str, list[Candle]]
    files_by_interval: dict[str, Path]
    days: int
    statuses_by_interval: dict[str, DataStatus] = field(default_factory=dict)
    run_id: str | None = None
    trust_decision: TrustDecision | None = None
    universe_snapshot: UniverseSnapshot | None = None


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
    fetcher: OKXHistoryFetcher | None = None,
    incremental_fetcher: OKXIncrementalFetcher | None = None,
    policy: TrustPolicy | None = None,
) -> CandleBundle:
    resolved = resolve_okx_swap_symbol(symbol)
    store = TrustedDataStore(data_dir=Path(data_dir))
    requested_intervals = tuple(dict.fromkeys(intervals))
    if refresh:
        provider = _ServiceProvider(days=days, fetcher=fetcher, incremental_fetcher=incremental_fetcher)
        RefreshTrustedMarketData(store, provider).execute(
            RefreshTrustedMarketDataRequest(
                requested_intervals=requested_intervals,
                days=days,
                limit=0,
                explicit_symbols=(resolved.inst_id,),
                stock_token_inst_ids=set(),
            )
        )
    bundle = LoadTrustedBundle(store).execute(
        LoadTrustedBundleQuery(
            resolved.inst_id,
            intervals=requested_intervals,
            days=days,
        ),
        policy or research_strict_policy(),
    )
    return _compat_bundle(resolved, bundle)


def trusted_bundle_error(
    bundle: CandleBundle,
    *,
    required_intervals: tuple[str, ...] = TRUSTED_REQUIRED_INTERVALS,
) -> str | None:
    decision = getattr(bundle, "trust_decision", None)
    if decision is not None and not decision.allowed:
        return f"trusted data blocked: {decision.reason.value}"
    return trusted_status_error(bundle.statuses_by_interval, required_intervals=required_intervals)


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


class _ServiceProvider:
    def __init__(
        self,
        *,
        days: int,
        fetcher: OKXHistoryFetcher | None = None,
        incremental_fetcher: OKXIncrementalFetcher | None = None,
    ):
        from mu_strategy.market_data.trusted import _CompatProvider

        self._provider = _CompatProvider(days=days, fetcher=fetcher, incremental_fetcher=incremental_fetcher)

    def fetch_tickers(self):
        return self._provider.fetch_tickers()

    def fetch_history(self, symbol, interval, *, days):
        return self._provider.fetch_history(symbol, interval, days=days)

    def fetch_incremental(self, symbol, interval, *, since_time_ms):
        return self._provider.fetch_incremental(symbol, interval, since_time_ms=since_time_ms)


def _compat_bundle(resolved: ResolvedSymbol, bundle) -> CandleBundle:
    statuses = {
        interval: _data_status_from_health(health)
        for interval, health in bundle.health_by_interval.items()
    }
    return CandleBundle(
        symbol=resolved,
        candles_by_interval=bundle.candles_by_interval,
        files_by_interval=bundle.files_by_interval,
        days=bundle.days,
        statuses_by_interval=statuses,
        run_id=bundle.run_id,
        trust_decision=bundle.trust_decision,
        universe_snapshot=bundle.universe_snapshot,
    )


def _data_status_from_health(health: DatasetHealth) -> DataStatus:
    payload = health.to_dict()
    validation = payload.get("validation")
    from mu_strategy.market_data.trusted import CandleValidationResult

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
        validation=CandleValidationResult(
            ok=bool(validation.get("ok")),
            reason=str(validation.get("reason")),
            missing_in_built=list(validation.get("missing_in_built") or []),
            missing_in_native=list(validation.get("missing_in_native") or []),
            misaligned_timestamps=list(validation.get("misaligned_timestamps") or []),
            value_mismatches=list(validation.get("value_mismatches") or []),
        )
        if isinstance(validation, dict)
        else None,
    )
