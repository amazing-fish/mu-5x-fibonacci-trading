from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from mu_strategy.market_data.providers.okx import fetch_okx_historical, fetch_okx_incremental
from mu_strategy.market_data.symbols import resolve_okx_swap_symbol
from mu_strategy.market_data.trusted_data.contracts import (
    AvailabilityState,
    Clock,
    DatasetHealth,
    DatasetKey,
    FreshnessState,
    HealthReason,
    IntegrityState,
    RefreshRun,
    RefreshRunOutcome,
    SystemClock,
    UniverseSnapshot,
    ValidationReport,
)
from mu_strategy.market_data.trusted_data.policy import FreshnessPolicy, IntervalDependencyPlanner
from mu_strategy.market_data.trusted_data.store import TrustedDataStore, candles_content_sha256
from mu_strategy.market_data.trusted_data.validation import (
    aggregate_candles,
    normalize_and_validate_candles,
    validate_built_native_candles,
)
from mu_strategy.market_data.trusted_data.windowing import prune_candle_bundle, resolve_shared_window
from mu_strategy.market_data.universe import OKXSwapTicker, fetch_okx_swap_tickers, select_top_okx_usdt_swaps
from mu_strategy.market_data.utils import dedupe_candles
from mu_strategy.models import Candle


DEFAULT_INTERVALS = ("5m", "15m", "1h")
DEFAULT_STOCK_TOKEN_CONFIG = Path("config/okx_stock_tokens.json")
DEFAULT_LIVE_DATA_DIR = Path("data/live")
OKXHistoryFetcher = Callable[..., list[Candle]]
OKXIncrementalFetcher = Callable[..., list[Candle]]


class RefreshScope(Enum):
    CANONICAL_UNIVERSE = "canonical_universe"
    EXPLICIT_SYMBOLS = "explicit_symbols"


class MarketDataProvider(Protocol):
    def fetch_tickers(self) -> list[dict]:
        ...

    def fetch_history(self, symbol: str, interval: str, *, days: int) -> list[Candle]:
        ...

    def fetch_incremental(self, symbol: str, interval: str, *, since_time_ms: int) -> list[Candle]:
        ...


class OKXMarketDataProvider:
    def __init__(
        self,
        *,
        ticker_rows: list[dict] | None = None,
        history_fetcher: OKXHistoryFetcher | None = None,
        incremental_fetcher: OKXIncrementalFetcher | None = None,
        history_days_fallback: int | None = None,
    ):
        self.ticker_rows = ticker_rows
        self.history_fetcher = history_fetcher
        self.incremental_fetcher = incremental_fetcher
        self.history_days_fallback = history_days_fallback

    def fetch_tickers(self) -> list[dict]:
        if self.ticker_rows is not None:
            return list(self.ticker_rows)
        return fetch_okx_swap_tickers()

    def fetch_history(self, symbol: str, interval: str, *, days: int) -> list[Candle]:
        if self.history_fetcher is not None:
            return self.history_fetcher(symbol, interval, days=days)
        return fetch_okx_historical(symbol, interval, days=days)

    def fetch_incremental(self, symbol: str, interval: str, *, since_time_ms: int) -> list[Candle]:
        if self.incremental_fetcher is not None:
            return self.incremental_fetcher(symbol, interval, since_time_ms=since_time_ms)
        if self.history_fetcher is not None and self.history_days_fallback is not None:
            return self.history_fetcher(symbol, interval, days=self.history_days_fallback)
        return fetch_okx_incremental(symbol, interval, since_time_ms=since_time_ms)


def refresh_with_okx_provider(
    store: TrustedDataStore,
    request: RefreshTrustedMarketDataRequest,
    *,
    ticker_rows: list[dict] | None = None,
    history_fetcher: OKXHistoryFetcher | None = None,
    incremental_fetcher: OKXIncrementalFetcher | None = None,
    history_days_fallback: int | None = None,
    clock: Clock | None = None,
) -> RefreshRun:
    provider = OKXMarketDataProvider(
        ticker_rows=ticker_rows,
        history_fetcher=history_fetcher,
        incremental_fetcher=incremental_fetcher,
        history_days_fallback=history_days_fallback,
    )
    return RefreshTrustedMarketData(store, provider, clock=clock).execute(request)


@dataclass(frozen=True)
class RefreshTrustedMarketDataRequest:
    requested_intervals: tuple[str, ...] = DEFAULT_INTERVALS
    days: int = 180
    limit: int = 10
    stock_token_config: Path = DEFAULT_STOCK_TOKEN_CONFIG
    stock_token_inst_ids: set[str] | None = None
    explicit_symbols: tuple[str, ...] = ()
    now_ms: int | None = None
    run_id: str | None = None
    scope: RefreshScope = RefreshScope.CANONICAL_UNIVERSE

    def __post_init__(self) -> None:
        scope = self.scope
        if not isinstance(scope, RefreshScope):
            scope = RefreshScope(str(scope))
            object.__setattr__(self, "scope", scope)
        if self.limit < 0:
            raise ValueError("limit must be non-negative")
        if scope != RefreshScope.CANONICAL_UNIVERSE:
            raise ValueError("non-canonical trusted refresh scopes cannot publish canonical manifest")
        if self.explicit_symbols:
            raise ValueError("canonical trusted market-data refresh cannot use explicit_symbols")


@dataclass(frozen=True)
class DatasetRefreshCandidate:
    key: DatasetKey
    path: Path
    candles: list[Candle]
    had_existing: bool = False
    fetch_reason: HealthReason | None = None
    error_type: str | None = None
    message: str | None = None


class RefreshTrustedMarketData:
    def __init__(
        self,
        store: TrustedDataStore,
        provider: MarketDataProvider | None = None,
        *,
        planner: IntervalDependencyPlanner | None = None,
        freshness_policy: FreshnessPolicy | None = None,
        clock: Clock | None = None,
    ):
        self.store = store
        self.provider = provider or OKXMarketDataProvider()
        self.planner = planner or IntervalDependencyPlanner()
        self.freshness_policy = freshness_policy or FreshnessPolicy(max_staleness_bars=3)
        self.clock = clock or SystemClock()

    def execute(self, request: RefreshTrustedMarketDataRequest) -> RefreshRun:
        started_at_ms = _now_ms(request.now_ms, self.clock)
        plan = self.planner.plan(request.requested_intervals)
        run_id = request.run_id or uuid.uuid4().hex
        try:
            universe = self._universe(request)
        except Exception as exc:
            run = RefreshRun(
                run_id=run_id,
                outcome=RefreshRunOutcome.FAILED,
                started_at_ms=started_at_ms,
                completed_at_ms=_now_ms(request.now_ms, self.clock),
                requested_intervals=plan.requested_intervals,
                effective_intervals=plan.effective_intervals,
                universe_snapshot=UniverseSnapshot(),
                cycle_error={"error_type": type(exc).__name__, "message": str(exc)},
            )
            self._persist_run(run)
            return run

        datasets: dict[tuple[str, str], DatasetHealth] = {}
        candles_by_key: dict[tuple[str, str], list[Candle]] = {}
        for ticker in _dedupe_tickers([*universe.crypto_top, *universe.stock_token_top]):
            symbol = str(ticker["inst_id"])
            candidates: dict[tuple[str, str], DatasetRefreshCandidate] = {}
            for interval in plan.effective_intervals:
                candidate = self._fetch_dataset_candidate(
                    symbol=symbol,
                    interval=interval,
                    days=request.days,
                )
                candidates[(symbol, interval)] = candidate
            symbol_datasets, symbol_candles = self._materialize_symbol_bundle(
                symbol=symbol,
                intervals=plan.effective_intervals,
                candidates=candidates,
                days=request.days,
                now_ms=_now_ms(request.now_ms, self.clock),
            )
            datasets.update(symbol_datasets)
            candles_by_key.update(symbol_candles)

        warnings: list[str] = []
        if request.limit > 0 and not datasets:
            warnings.append("empty_universe")
        if len(universe.stock_token_top) < request.limit:
            warnings.append(f"stock_token_top_count_below_limit:{len(universe.stock_token_top)}/{request.limit}")
        outcome = _refresh_outcome(datasets)
        if request.limit > 0 and not datasets:
            outcome = RefreshRunOutcome.FAILED
        run = RefreshRun(
            run_id=run_id,
            outcome=outcome,
            started_at_ms=started_at_ms,
            completed_at_ms=_now_ms(request.now_ms, self.clock),
            requested_intervals=plan.requested_intervals,
            effective_intervals=plan.effective_intervals,
            universe_snapshot=universe,
            datasets=datasets,
            warnings=tuple(warnings),
        )
        self._persist_run(run)
        return run

    def _universe(self, request: RefreshTrustedMarketDataRequest) -> UniverseSnapshot:
        if request.limit == 0:
            return UniverseSnapshot()
        rows = self.provider.fetch_tickers()
        stock_ids = request.stock_token_inst_ids
        if stock_ids is None:
            stock_ids = load_stock_token_inst_ids(request.stock_token_config)
        crypto_top = select_top_okx_crypto_swaps(rows, stock_token_inst_ids=stock_ids, limit=request.limit)
        stock_top = select_top_okx_stock_tokens(rows, stock_token_inst_ids=stock_ids, limit=request.limit)
        return UniverseSnapshot(
            crypto_top=tuple(_ticker_dict(ticker) for ticker in crypto_top),
            stock_token_top=tuple(_ticker_dict(ticker) for ticker in stock_top),
        )

    def _fetch_dataset_candidate(
        self,
        *,
        symbol: str,
        interval: str,
        days: int,
    ) -> DatasetRefreshCandidate:
        path = self.store.cache_path(symbol, interval)
        existing: list[Candle] = []
        cache_loaded = False
        try:
            if path.exists():
                existing = self.store.read_csv(path)
            cache_loaded = True
            if existing:
                since_time_ms = existing[-2].open_time_ms if len(existing) >= 2 else existing[0].open_time_ms
                fetched = self.provider.fetch_incremental(symbol, interval, since_time_ms=since_time_ms)
            else:
                fetched = self.provider.fetch_history(symbol, interval, days=days)
            return DatasetRefreshCandidate(
                key=DatasetKey(symbol, interval),
                path=path,
                candles=dedupe_candles([*existing, *fetched]),
                had_existing=bool(existing),
            )
        except Exception as exc:
            reason = HealthReason.INCREMENTAL_REFRESH_FAILED if existing else HealthReason.REFRESH_FAILED
            if path.exists() and not cache_loaded:
                reason = HealthReason.CACHE_READ_FAILED
                existing = []
            return DatasetRefreshCandidate(
                key=DatasetKey(symbol, interval),
                path=path,
                candles=dedupe_candles(existing),
                had_existing=bool(existing),
                fetch_reason=reason,
                error_type=type(exc).__name__,
                message=str(exc),
            )

    def _materialize_symbol_bundle(
        self,
        *,
        symbol: str,
        intervals: tuple[str, ...],
        candidates: dict[tuple[str, str], DatasetRefreshCandidate],
        days: int,
        now_ms: int,
    ) -> tuple[dict[tuple[str, str], DatasetHealth], dict[tuple[str, str], list[Candle]]]:
        raw_candles_by_interval = {
            interval: candidates[(symbol, interval)].candles
            for interval in intervals
        }
        window_plan = resolve_shared_window(raw_candles_by_interval, days=days)
        pruned_candles_by_interval = prune_candle_bundle(raw_candles_by_interval, plan=window_plan)
        datasets: dict[tuple[str, str], DatasetHealth] = {}
        candles_by_key: dict[tuple[str, str], list[Candle]] = {}

        for interval in intervals:
            candidate = candidates[(symbol, interval)]
            key = candidate.key.tuple()
            candles = pruned_candles_by_interval.get(interval) or []
            if candidate.fetch_reason is not None:
                datasets[key] = _health(
                    symbol,
                    interval,
                    candidate.path,
                    candles,
                    now_ms=now_ms,
                    availability=AvailabilityState.AVAILABLE if candles else AvailabilityState.MISSING,
                    integrity=IntegrityState.INVALID,
                    freshness=FreshnessState.STALE,
                    reason=candidate.fetch_reason,
                    error_type=candidate.error_type,
                    message=candidate.message,
                )
                candles_by_key[key] = []
                continue

            try:
                candles, validation = normalize_and_validate_candles(candles, interval=interval)
                if not validation.ok:
                    datasets[key] = _health(
                        symbol,
                        interval,
                        candidate.path,
                        candles,
                        now_ms=now_ms,
                        availability=AvailabilityState.AVAILABLE if candles else AvailabilityState.MISSING,
                        integrity=IntegrityState.INVALID,
                        freshness=FreshnessState.STALE,
                        reason=validation.reason,
                        validation=validation,
                    )
                    candles_by_key[key] = candles if validation.reason == HealthReason.TIMESTAMP_GAP else []
                    continue
                self.store.write_csv(candles, candidate.path)
                freshness = self.freshness_policy.assess(
                    now_ms=now_ms,
                    interval=interval,
                    last_confirmed_open_time_ms=candles[-1].open_time_ms if candles else None,
                )
                datasets[key] = _health(
                    symbol,
                    interval,
                    candidate.path,
                    candles,
                    now_ms=now_ms,
                    availability=AvailabilityState.AVAILABLE,
                    integrity=IntegrityState.VALID,
                    freshness=freshness.state,
                    reason=freshness.reason,
                    validation=validation,
                    content_sha256=candles_content_sha256(candles),
                )
                candles_by_key[key] = candles
            except Exception as exc:
                reason = HealthReason.INCREMENTAL_REFRESH_FAILED if candidate.had_existing else HealthReason.REFRESH_FAILED
                datasets[key] = _health(
                    symbol,
                    interval,
                    candidate.path,
                    candles,
                    now_ms=now_ms,
                    availability=AvailabilityState.AVAILABLE if candles else AvailabilityState.MISSING,
                    integrity=IntegrityState.INVALID,
                    freshness=FreshnessState.STALE,
                    reason=reason,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
                candles_by_key[key] = []

        self._attach_built_native_validation(symbol, datasets, candles_by_key)
        return datasets, candles_by_key

    def _attach_built_native_validation(
        self,
        symbol: str,
        datasets: dict[tuple[str, str], DatasetHealth],
        candles_by_key: dict[tuple[str, str], list[Candle]],
    ) -> None:
        base_health = datasets.get((symbol, "5m"))
        if base_health is None or not _has_built_native_validation_inputs(base_health):
            return
        five = candles_by_key.get((symbol, "5m")) or []
        for interval in ("15m", "1h"):
            key = (symbol, interval)
            native_health = datasets.get(key)
            if native_health is None or not _has_built_native_validation_inputs(native_health):
                continue
            report = validate_built_native_candles(
                aggregate_candles(five, interval=interval),
                candles_by_key.get(key) or [],
                interval=interval,
            )
            if report.ok and native_health.integrity != IntegrityState.VALID:
                continue
            datasets[key] = replace(
                native_health,
                integrity=IntegrityState.VALID if report.ok else IntegrityState.INVALID,
                freshness=native_health.freshness if report.ok else FreshnessState.STALE,
                reasons=(native_health.primary_reason if report.ok else report.reason,),
                validation=report,
            )

    def _persist_run(self, run: RefreshRun) -> None:
        self.store.append_run_log(run.run_log_payload())
        self.store.write_manifest(run.to_manifest())


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
    _validate_selection_limit(limit)
    if limit == 0:
        return []
    filtered_rows = [row for row in rows if str(row.get("instId") or "") not in stock_token_inst_ids]
    return select_top_okx_usdt_swaps(filtered_rows, limit=limit)


def select_top_okx_stock_tokens(
    rows: list[dict],
    *,
    stock_token_inst_ids: set[str],
    limit: int,
) -> list[OKXSwapTicker]:
    _validate_selection_limit(limit)
    if limit == 0:
        return []
    return [
        OKXSwapTicker(
            inst_id=ticker.inst_id,
            last=ticker.last,
            volume_ccy_24h=ticker.volume_ccy_24h,
            source="stock_token",
        )
        for ticker in select_top_okx_usdt_swaps(rows, limit=len(rows))
        if ticker.inst_id in stock_token_inst_ids
    ][:limit]


def _validate_selection_limit(limit: int) -> None:
    if limit < 0:
        raise ValueError("limit must be non-negative")


def _health(
    symbol: str,
    interval: str,
    path: Path,
    candles: list[Candle],
    *,
    now_ms: int,
    availability: AvailabilityState,
    integrity: IntegrityState,
    freshness: FreshnessState,
    reason: HealthReason,
    validation: ValidationReport | None = None,
    error_type: str | None = None,
    message: str | None = None,
    content_sha256: str | None = None,
) -> DatasetHealth:
    return DatasetHealth(
        key=DatasetKey(symbol, interval),
        availability=availability,
        integrity=integrity,
        freshness=freshness,
        reasons=(reason,),
        rows=len(candles),
        first_timestamp_ms=candles[0].open_time_ms if candles else None,
        last_timestamp_ms=candles[-1].open_time_ms if candles else None,
        updated_at_ms=now_ms,
        source_file=path,
        validation=validation,
        error_type=error_type,
        message=message,
        content_sha256=content_sha256,
    )


def _refresh_outcome(datasets: dict[tuple[str, str], DatasetHealth]) -> RefreshRunOutcome:
    if not datasets:
        return RefreshRunOutcome.FAILED
    valid_count = sum(
        1
        for health in datasets.values()
        if health.availability == AvailabilityState.AVAILABLE and health.integrity == IntegrityState.VALID
    )
    if valid_count == len(datasets):
        return RefreshRunOutcome.SUCCESS
    if valid_count == 0:
        return RefreshRunOutcome.FAILED if len(datasets) == 1 else RefreshRunOutcome.PARTIAL
    return RefreshRunOutcome.PARTIAL


def _now_ms(value: int | None, clock: Clock) -> int:
    return int(value if value is not None else clock.now_ms())


def _ticker_dict(ticker: OKXSwapTicker) -> dict:
    return {
        "inst_id": ticker.inst_id,
        "last": ticker.last,
        "volume_ccy_24h": ticker.volume_ccy_24h,
        "source": ticker.source,
    }


def _dedupe_tickers(tickers: list[dict]) -> list[dict]:
    by_symbol: dict[str, dict] = {}
    for ticker in tickers:
        by_symbol.setdefault(str(ticker["inst_id"]), ticker)
    return list(by_symbol.values())


def _has_validation_inputs(health: DatasetHealth) -> bool:
    return (
        health.availability == AvailabilityState.AVAILABLE
        and health.integrity == IntegrityState.VALID
        and health.rows > 0
    )


def _has_built_native_validation_inputs(health: DatasetHealth) -> bool:
    if _has_validation_inputs(health):
        return True
    return (
        health.availability == AvailabilityState.AVAILABLE
        and health.rows > 0
        and health.validation is not None
        and health.validation.reason == HealthReason.TIMESTAMP_GAP
    )
