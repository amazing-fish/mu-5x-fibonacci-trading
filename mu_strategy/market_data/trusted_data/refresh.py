from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
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
    RefreshSegmentDiagnostics,
    RefreshRun,
    RefreshAttemptStatus,
    SnapshotUsability,
    SystemClock,
    UniverseSnapshot,
)
from mu_strategy.market_data.trusted_data.evaluate import DatasetEvaluationSeed, classify_publication_health, evaluate_candle_bundle, exception_failure
from mu_strategy.market_data.trusted_data.policy import FreshnessPolicy, IntervalDependencyPlanner
from mu_strategy.market_data.trusted_data.store import TrustedDataStore, candles_content_sha256, validate_storage_segment
from mu_strategy.market_data.trusted_data.windowing import assess_requested_coverage
from mu_strategy.market_data.universe import OKXSwapTicker, fetch_okx_swap_tickers, select_top_okx_usdt_swaps
from mu_strategy.market_data.utils import dedupe_candles
from mu_strategy.models import Candle


DEFAULT_INTERVALS = ("5m", "15m", "1h")
DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_REQUEST_MAX_CONCURRENCY = 1
DEFAULT_STOCK_TOKEN_CONFIG = Path("config/okx_stock_tokens.json")
DEFAULT_LIVE_DATA_DIR = Path("data/live")
OKXHistoryFetcher = Callable[..., list[Candle]]
OKXIncrementalFetcher = Callable[..., list[Candle]]


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
    max_concurrency: int = DEFAULT_REQUEST_MAX_CONCURRENCY
    symbols: tuple[str, ...] = ()
    stock_token_config: Path = DEFAULT_STOCK_TOKEN_CONFIG
    stock_token_inst_ids: set[str] | None = None
    now_ms: int | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ValueError("limit must be non-negative")
        if not isinstance(self.max_concurrency, int) or isinstance(self.max_concurrency, bool) or self.max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        object.__setattr__(self, "symbols", _normalize_explicit_symbols(self.symbols))


@dataclass(frozen=True)
class DatasetRefreshCandidate:
    key: DatasetKey
    path: Path
    source_file: Path
    candles: list[Candle]
    diagnostics: RefreshSegmentDiagnostics
    had_existing: bool = False
    fetch_reason: HealthReason | None = None
    error_type: str | None = None
    message: str | None = None


class ReusablePriorDatasetReadError(Exception):
    pass


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
        for interval in plan.effective_intervals:
            validate_storage_segment(interval, field="interval")
        run_id = validate_storage_segment(request.run_id or uuid.uuid4().hex, field="run_id")
        previous_manifest = self.store.read_manifest()
        self.store.prepare_generation(run_id)
        try:
            universe = self._universe(request)
        except Exception as exc:
            failure = exception_failure(exc)
            run = RefreshRun(
                run_id=run_id,
                attempt_status=RefreshAttemptStatus.FAILED,
                snapshot_usability=SnapshotUsability.INVALID,
                started_at_ms=started_at_ms,
                completed_at_ms=_now_ms(request.now_ms, self.clock),
                requested_intervals=plan.requested_intervals,
                effective_intervals=plan.effective_intervals,
                universe_snapshot=UniverseSnapshot(),
                provider_failures=(
                    {
                        "symbol": "*",
                        "interval": "*",
                        "reason": HealthReason.REFRESH_FAILED.value,
                        **failure,
                    },
                ),
                cycle_error=failure,
            )
            run = self._persist_run(run)
            return run

        datasets: dict[tuple[str, str], DatasetHealth] = {}
        candles_by_key: dict[tuple[str, str], list[Candle]] = {}
        refresh_segments_by_key: dict[tuple[str, str], RefreshSegmentDiagnostics] = {}
        symbols = tuple(
            validate_storage_segment(str(ticker["inst_id"]), field="symbol")
            for ticker in _dedupe_tickers([*universe.crypto_top, *universe.stock_token_top])
        )
        candidates_by_key = self._fetch_dataset_candidates(
            symbols=symbols,
            intervals=plan.effective_intervals,
            days=request.days,
            run_id=run_id,
            previous_manifest=previous_manifest,
            max_concurrency=request.max_concurrency,
        )
        for symbol in symbols:
            candidates = {
                (symbol, interval): candidates_by_key[(symbol, interval)]
                for interval in plan.effective_intervals
            }
            for key, candidate in candidates.items():
                refresh_segments_by_key[key] = candidate.diagnostics
            symbol_datasets, symbol_candles = self._materialize_symbol_bundle(
                symbol=symbol,
                intervals=plan.effective_intervals,
                candidates=candidates,
                days=request.days,
                now_ms=_now_ms(request.now_ms, self.clock),
            )
            for key, health in symbol_datasets.items():
                if key in refresh_segments_by_key:
                    refresh_segments_by_key[key] = refresh_segments_by_key[key].with_health(health)
            datasets.update(symbol_datasets)
            candles_by_key.update(symbol_candles)

        warnings: list[str] = []
        if not request.symbols:
            if request.limit > 0 and not datasets:
                warnings.append("empty_universe")
            if len(universe.stock_token_top) < request.limit:
                warnings.append(f"stock_token_top_count_below_limit:{len(universe.stock_token_top)}/{request.limit}")
        provider_failures = _provider_failures(datasets)
        health_summary = classify_publication_health(datasets, provider_failures=provider_failures)
        run = RefreshRun(
            run_id=run_id,
            attempt_status=health_summary.attempt_status,
            snapshot_usability=health_summary.snapshot_usability,
            started_at_ms=started_at_ms,
            completed_at_ms=_now_ms(request.now_ms, self.clock),
            requested_intervals=plan.requested_intervals,
            effective_intervals=plan.effective_intervals,
            universe_snapshot=universe,
            datasets=datasets,
            provider_failures=provider_failures,
            warnings=tuple(warnings),
            refresh_segments=tuple(refresh_segments_by_key.values()),
        )
        run = self._persist_run(run)
        return run

    def _fetch_dataset_candidates(
        self,
        *,
        symbols: tuple[str, ...],
        intervals: tuple[str, ...],
        days: int,
        run_id: str,
        previous_manifest,
        max_concurrency: int,
    ) -> dict[tuple[str, str], DatasetRefreshCandidate]:
        tasks = tuple((symbol, interval) for symbol in symbols for interval in intervals)

        def fetch(symbol: str, interval: str) -> DatasetRefreshCandidate:
            return self._fetch_dataset_candidate(
                symbol=symbol,
                interval=interval,
                days=days,
                run_id=run_id,
                previous_manifest=previous_manifest,
            )

        if max_concurrency == 1:
            return {key: fetch(*key) for key in tasks}

        with ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="trusted-refresh") as executor:
            futures = [executor.submit(fetch, *key) for key in tasks]
            return {key: future.result() for key, future in zip(tasks, futures)}

    def _universe(self, request: RefreshTrustedMarketDataRequest) -> UniverseSnapshot:
        if request.symbols:
            return UniverseSnapshot(
                crypto_top=tuple(_explicit_ticker_dict(symbol) for symbol in request.symbols),
            )
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
        run_id: str,
        previous_manifest,
    ) -> DatasetRefreshCandidate:
        started_at_ms = self.clock.now_ms()
        path = self.store.generation_cache_path(run_id, symbol, interval)
        source_file = self.store.generation_source_file(symbol, interval)
        existing: list[Candle] = []
        try:
            existing = self._load_reusable_prior_candles(previous_manifest, symbol, interval, days=days) or []
            if existing:
                since_time_ms = existing[-2].open_time_ms if len(existing) >= 2 else existing[0].open_time_ms
                fetched = self.provider.fetch_incremental(symbol, interval, since_time_ms=since_time_ms)
                fetch_mode = "incremental_reuse"
            else:
                fetched = self.provider.fetch_history(symbol, interval, days=days)
                fetch_mode = "full_history"
            candles = dedupe_candles([*existing, *fetched])
            return DatasetRefreshCandidate(
                key=DatasetKey(symbol, interval),
                path=path,
                source_file=source_file,
                candles=candles,
                diagnostics=_segment_diagnostics(
                    symbol=symbol,
                    interval=interval,
                    fetch_mode=fetch_mode,
                    started_at_ms=started_at_ms,
                    completed_at_ms=self.clock.now_ms(),
                    existing_rows=len(existing),
                    fetched_rows=len(fetched),
                    output_rows=len(candles),
                    had_existing=bool(existing),
                    reused_prior_generation=bool(existing),
                ),
                had_existing=bool(existing),
            )
        except ReusablePriorDatasetReadError as exc:
            prior_failure = exception_failure(exc.__cause__ if exc.__cause__ is not None else exc)
            try:
                fetched = self.provider.fetch_history(symbol, interval, days=days)
                candles = dedupe_candles(fetched)
                return DatasetRefreshCandidate(
                    key=DatasetKey(symbol, interval),
                    path=path,
                    source_file=source_file,
                    candles=candles,
                    diagnostics=_segment_diagnostics(
                        symbol=symbol,
                        interval=interval,
                        fetch_mode="prior_read_failed_full_history",
                        started_at_ms=started_at_ms,
                        completed_at_ms=self.clock.now_ms(),
                        existing_rows=0,
                        fetched_rows=len(fetched),
                        output_rows=len(candles),
                        had_existing=False,
                        reused_prior_generation=False,
                        fetch_reason=HealthReason.CACHE_READ_FAILED,
                        error_type=prior_failure["error_type"],
                        message=prior_failure["message"],
                    ),
                )
            except Exception:
                failure = prior_failure
            return DatasetRefreshCandidate(
                key=DatasetKey(symbol, interval),
                path=path,
                source_file=source_file,
                candles=[],
                diagnostics=_segment_diagnostics(
                    symbol=symbol,
                    interval=interval,
                    fetch_mode="cache_read_failed",
                    started_at_ms=started_at_ms,
                    completed_at_ms=self.clock.now_ms(),
                    existing_rows=0,
                    fetched_rows=0,
                    output_rows=0,
                    had_existing=False,
                    reused_prior_generation=False,
                    fetch_reason=HealthReason.CACHE_READ_FAILED,
                    error_type=failure["error_type"],
                    message=failure["message"],
                ),
                fetch_reason=HealthReason.CACHE_READ_FAILED,
                error_type=failure["error_type"],
                message=failure["message"],
            )
        except Exception as exc:
            reason = HealthReason.INCREMENTAL_REFRESH_FAILED if existing else HealthReason.REFRESH_FAILED
            failure = exception_failure(exc)
            candles = dedupe_candles(existing)
            return DatasetRefreshCandidate(
                key=DatasetKey(symbol, interval),
                path=path,
                source_file=source_file,
                candles=candles,
                diagnostics=_segment_diagnostics(
                    symbol=symbol,
                    interval=interval,
                    fetch_mode="incremental_failed_reused_cache" if existing else "refresh_failed",
                    started_at_ms=started_at_ms,
                    completed_at_ms=self.clock.now_ms(),
                    existing_rows=len(existing),
                    fetched_rows=0,
                    output_rows=len(candles),
                    had_existing=bool(existing),
                    reused_prior_generation=bool(existing),
                    fetch_reason=reason,
                    error_type=failure["error_type"],
                    message=failure["message"],
                ),
                had_existing=bool(existing),
                fetch_reason=reason,
                error_type=failure["error_type"],
                message=failure["message"],
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
        seeds_by_interval = {
            interval: DatasetEvaluationSeed(
                key=candidates[(symbol, interval)].key,
                source_file=candidates[(symbol, interval)].source_file,
                candles=candidates[(symbol, interval)].candles,
                prefailed_reason=(
                    candidates[(symbol, interval)].fetch_reason
                    if candidates[(symbol, interval)].fetch_reason is not None and not candidates[(symbol, interval)].had_existing
                    else None
                ),
                empty_prefailed_reason=candidates[(symbol, interval)].fetch_reason,
                exception_reason=(
                    HealthReason.INCREMENTAL_REFRESH_FAILED
                    if candidates[(symbol, interval)].had_existing
                    else HealthReason.REFRESH_FAILED
                ),
                error_type=candidates[(symbol, interval)].error_type,
                message=candidates[(symbol, interval)].message,
                warnings=_fetch_warnings(candidates[(symbol, interval)]),
            )
            for interval in intervals
        }

        def write_valid_dataset(interval: str, seed: DatasetEvaluationSeed, candles: list[Candle]) -> str:
            candidate = candidates[(symbol, interval)]
            if candidate.path.exists():
                raise FileExistsError(f"generation dataset already exists: {candidate.path}")
            self.store.write_csv(candles, candidate.path)
            return candles_content_sha256(candles)

        result = evaluate_candle_bundle(
            symbol=symbol,
            intervals=intervals,
            seeds_by_interval=seeds_by_interval,
            days=days,
            now_ms=now_ms,
            freshness_policy=self.freshness_policy,
            on_validated_candles=write_valid_dataset,
            retain_invalid_candles_for_reasons=(HealthReason.TIMESTAMP_GAP,),
            allow_timestamp_gap_built_native_inputs=True,
            raise_os_errors=True,
        )
        return result.health_by_key, result.candles_by_key

    def _persist_run(self, run: RefreshRun) -> RefreshRun:
        publication_warnings = self.store.commit_generation_publication(
            run.run_id,
            run.to_manifest(),
            run.run_log_payload(),
        )
        if not publication_warnings:
            return run
        return replace(run, warnings=(*run.warnings, *publication_warnings))

    def _previous_dataset_path(self, manifest_result, symbol: str, interval: str) -> Path | None:
        if not manifest_result.ok or manifest_result.snapshot is None or manifest_result.generation_root is None:
            return None
        health = manifest_result.snapshot.datasets.get((symbol, interval))
        if health is None:
            return None
        try:
            return self.store.resolve_source_file(
                health.source_file,
                generation_root=manifest_result.generation_root,
                generation_id=manifest_result.generation_id,
            )
        except Exception:
            return None

    def _load_reusable_prior_candles(self, manifest_result, symbol: str, interval: str, *, days: int) -> list[Candle] | None:
        if not manifest_result.ok or manifest_result.snapshot is None:
            return None
        health = manifest_result.snapshot.datasets.get((symbol, interval))
        if health is None or not _is_reusable_prior_health(health):
            return None
        previous_path = self._previous_dataset_path(manifest_result, symbol, interval)
        if previous_path is None or not previous_path.exists():
            return None
        try:
            cached = self.store.read_csv(previous_path)
        except Exception as exc:
            raise ReusablePriorDatasetReadError from exc
        if candles_content_sha256(cached) != health.content_sha256:
            return None
        coverage = assess_requested_coverage(
            cached,
            interval=interval,
            requested_days=days,
            window_end_time_ms=cached[-1].open_time_ms if cached else None,
        )
        if not coverage.covered and not _is_reusable_partial_history(health, requested_days=days):
            return None
        return cached


def _is_reusable_prior_health(health: DatasetHealth) -> bool:
    return (
        health.availability == AvailabilityState.AVAILABLE
        and health.integrity == IntegrityState.VALID
        and health.freshness in {FreshnessState.FRESH, FreshnessState.STALE}
        and bool(health.content_sha256)
    )


def _is_reusable_partial_history(health: DatasetHealth, *, requested_days: int) -> bool:
    return (
        health.coverage_state == "partial_available_history"
        and health.requested_days is not None
        and health.requested_days >= requested_days
    )


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


def _fetch_warnings(candidate: DatasetRefreshCandidate) -> tuple[str, ...]:
    if candidate.fetch_reason is None:
        return ()
    return (candidate.fetch_reason.value,)


def _provider_failures(datasets: dict[tuple[str, str], DatasetHealth]) -> tuple[dict[str, str], ...]:
    failures: list[dict[str, str]] = []
    for (symbol, interval), health in datasets.items():
        reason = health.primary_reason
        if HealthReason.INCREMENTAL_REFRESH_FAILED.value in health.warnings:
            reason = HealthReason.INCREMENTAL_REFRESH_FAILED
        if reason not in {
            HealthReason.REFRESH_FAILED,
            HealthReason.INCREMENTAL_REFRESH_FAILED,
        }:
            continue
        failures.append(
            {
                "symbol": symbol,
                "interval": interval,
                "reason": reason.value,
                **_failure_fields_from_health(health),
            }
        )
    return tuple(failures)


def _failure_fields_from_health(health: DatasetHealth) -> dict[str, str]:
    error_type = (health.error_type or "").strip() or "Error"
    message = (health.message or "").strip() or error_type
    return {"error_type": error_type, "message": message}


def _segment_diagnostics(
    *,
    symbol: str,
    interval: str,
    fetch_mode: str,
    started_at_ms: int,
    completed_at_ms: int,
    existing_rows: int,
    fetched_rows: int,
    output_rows: int,
    had_existing: bool,
    reused_prior_generation: bool,
    fetch_reason: HealthReason | None = None,
    error_type: str | None = None,
    message: str | None = None,
) -> RefreshSegmentDiagnostics:
    return RefreshSegmentDiagnostics(
        symbol=symbol,
        interval=interval,
        fetch_mode=fetch_mode,
        started_at_ms=started_at_ms,
        completed_at_ms=completed_at_ms,
        elapsed_ms=max(0, completed_at_ms - started_at_ms),
        existing_rows=existing_rows,
        fetched_rows=fetched_rows,
        output_rows=output_rows,
        had_existing=had_existing,
        reused_prior_generation=reused_prior_generation,
        fetch_reason=fetch_reason,
        error_type=error_type,
        message=message,
    )


def _now_ms(value: int | None, clock: Clock) -> int:
    return int(value if value is not None else clock.now_ms())


def _normalize_explicit_symbols(values) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        iterable = (values,)
    else:
        iterable = tuple(values)
    symbols: list[str] = []
    seen: set[str] = set()
    for value in iterable:
        resolved = resolve_okx_swap_symbol(str(value))
        inst_id = validate_storage_segment(resolved.inst_id, field="symbol")
        if inst_id in seen:
            continue
        seen.add(inst_id)
        symbols.append(inst_id)
    return tuple(symbols)


def _explicit_ticker_dict(symbol: str) -> dict:
    return {
        "inst_id": symbol,
        "last": 0.0,
        "volume_ccy_24h": 0.0,
        "source": "explicit",
    }


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
