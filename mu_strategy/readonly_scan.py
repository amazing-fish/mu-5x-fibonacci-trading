from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from mu_strategy.entry.scanner import EntryScanResult, scan_entry
from mu_strategy.market_data.symbols import resolve_okx_swap_symbol
from mu_strategy.market_data.trusted_data.compat import CandleBundle, ensure_trusted_candle_bundle, trust_error_payload
from mu_strategy.market_data.trusted_data.contracts import (
    Clock, FreshnessState, HealthReason, SystemClock, TrustedLoadContext, TrustedConsumerRefreshError,
)
from mu_strategy.market_data.trusted_data.load import (
    LoadTrustedBundle, load_trusted_candle_bundle, TRUSTED_CONSUMER_REFRESH_ERROR,
)
from mu_strategy.market_data.trusted_data.policy import FreshnessPolicy, trading_strict_policy
from mu_strategy.market_data.trusted_data.store import TrustedDataStore
from mu_strategy.market_data.universe import OKXSwapTicker
from mu_strategy.observations import ObservationFailureCode, Stage0ObservationCycle
from mu_strategy.scan_cycle import ScanCycle, ScanDataFailure
from mu_strategy.strategies.registry import baseline_strategy_group
from mu_strategy.strategy import StrategyConfig


CandleLoader = Callable[..., CandleBundle]
Scanner = Callable[..., EntryScanResult]


@dataclass(frozen=True)
class ScannedSymbol:
    ticker: OKXSwapTicker
    bundle: CandleBundle | None
    load_failure: ScanDataFailure | None
    strategy_config: StrategyConfig
    result: EntryScanResult | None
    data_error: dict[str, Any] | None


class ScanBatch:
    """Pin trusted inputs and evaluate them once, without application side effects.

    Callers own universe selection, cycle timing and persistence. `load` also
    serves confirmed Demo and position-only reads at their existing loader gate;
    only `scan` applies the canonical ScanCycle scanner/result boundary.
    """

    def __init__(self, *, data_dir: Path, days: int = 28, max_candle_staleness_bars: int = 3,
                 candle_loader: CandleLoader | None = None, refresh: bool = False):
        self.data_dir = data_dir
        self.days = days
        self.max_candle_staleness_bars = max_candle_staleness_bars
        self.refresh = refresh
        self.default_trusted_loader = candle_loader is None
        if self.default_trusted_loader and refresh:
            raise TrustedConsumerRefreshError(TRUSTED_CONSUMER_REFRESH_ERROR)
        self.candle_loader = candle_loader or load_trusted_candle_bundle
        self.context: TrustedLoadContext | None = None
        self.context_error: Exception | None = None
        if self.default_trusted_loader:
            try:
                self.context = LoadTrustedBundle(TrustedDataStore(data_dir=data_dir)).open_context()
            except Exception as exc:
                self.context_error = exc

    def scan(self, tickers: list[OKXSwapTicker], *, cycle: ScanCycle,
             scanner: Scanner = scan_entry) -> Iterator[ScannedSymbol]:
        for ticker in tickers:
            if self.context_error is not None:
                # Explicit watchlists retain one load failure per requested symbol.
                bundle, failure = None, _load_failure(ticker.inst_id, self.context_error)
            else:
                bundle, failure = self.load(ticker.inst_id)
            group = baseline_strategy_group(ticker.inst_id)
            outcome = cycle.scan_symbol(
                symbol=ticker.inst_id, source=ticker.source, bundle=bundle,
                requested_intervals=("15m", "1h"), strategy_name=group.name,
                strategy_config=group.config, scanner=scanner, data_failure=failure,
            )
            yield ScannedSymbol(ticker, bundle, failure, group.config, outcome.scan_result, outcome.data_error)

    def load(self, symbol: str) -> tuple[CandleBundle | None, ScanDataFailure | None]:
        requested_intervals = ("15m", "1h")
        try:
            loader_kwargs = {
                "intervals": requested_intervals,
                "days": self.days,
                "data_dir": self.data_dir,
                "refresh": self.refresh,
            }
            if self.default_trusted_loader:
                loader_kwargs["policy"] = trading_strict_policy()
                loader_kwargs["max_staleness_bars"] = self.max_candle_staleness_bars
                if self.context is not None:
                    loader_kwargs["context"] = self.context
            bundle = self.candle_loader(symbol, **loader_kwargs)
            if not _is_plain_legacy_bundle(bundle):
                bundle = ensure_trusted_candle_bundle(bundle, requested_intervals=requested_intervals)
        except Exception as exc:
            return None, _load_failure(symbol, exc)

        return bundle, _market_data_freshness_error(
            symbol=symbol,
            bundle=bundle,
            requested_intervals=requested_intervals,
            max_staleness_bars=self.max_candle_staleness_bars,
        )


def scan_watchlist(*, data_dir: Path, symbols: tuple[str, ...], days: int = 28,
                   candle_loader: CandleLoader | None = None, scanner: Scanner = scan_entry,
                   observation_clock: Clock | None = None,
                   observation_id_factory: Callable[[], str] | None = None) -> Stage0ObservationCycle:
    # Service timing is context first, then cycle. Demo constructs its cycle first.
    batch = ScanBatch(data_dir=data_dir, days=days, candle_loader=candle_loader)
    cycle = ScanCycle(clock=observation_clock, id_factory=observation_id_factory)
    for _ in batch.scan(merge_watchlist_tickers([], symbols), cycle=cycle, scanner=scanner):
        pass
    return cycle.observations()


def _load_failure(symbol: str, exc: Exception) -> ScanDataFailure:
    return ScanDataFailure(ObservationFailureCode.TRUSTED_DATA_LOAD_FAILED,
                           HealthReason.CACHE_READ_FAILED, _market_data_load_error(symbol, exc))


def merge_watchlist_tickers(
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


def _market_data_load_error(symbol: str, exc: Exception) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "reason": "market_data_load_failed",
        "error_type": type(exc).__name__,
        "message": str(exc),
    }


def _market_data_freshness_error(
    *,
    symbol: str,
    bundle: CandleBundle,
    requested_intervals: tuple[str, ...],
    max_staleness_bars: int,
) -> ScanDataFailure | None:
    if _is_plain_legacy_bundle(bundle):
        legacy_error = _plain_legacy_market_data_staleness_error(
            symbol=symbol,
            bundle=bundle,
            requested_intervals=requested_intervals,
            max_staleness_bars=max_staleness_bars,
        )
        if legacy_error is not None:
            return legacy_error
    else:
        trust_error = trust_error_payload(symbol, bundle, requested_intervals=requested_intervals)
        if trust_error is not None:
            return ScanDataFailure(
                ObservationFailureCode.TRUSTED_DATA_BLOCKED,
                bundle.trust_decision.reason,
                trust_error,
            )
    for interval in requested_intervals:
        candles = bundle.candles_by_interval.get(interval) or []
        if not candles:
            return ScanDataFailure(
                ObservationFailureCode.TRUSTED_DATA_BLOCKED,
                HealthReason.CACHE_MISSING,
                {
                    "symbol": symbol,
                    "reason": "market_data_missing",
                    "interval": interval,
                    "latest_open_time_ms": None,
                    "source_file": str(bundle.files_by_interval.get(interval, "")),
                },
            )
    return None


def _is_plain_legacy_bundle(bundle: Any) -> bool:
    statuses = getattr(bundle, "statuses_by_interval", None)
    return (
        getattr(bundle, "trust_decision", None) is None
        and not statuses
        and isinstance(getattr(bundle, "candles_by_interval", None), dict)
    )


def _plain_legacy_market_data_staleness_error(
    *,
    symbol: str,
    bundle: Any,
    requested_intervals: tuple[str, ...],
    max_staleness_bars: int,
) -> ScanDataFailure | None:
    policy = FreshnessPolicy(max_staleness_bars=max_staleness_bars)
    now_ms = SystemClock().now_ms()
    for interval in requested_intervals:
        candles = bundle.candles_by_interval.get(interval) or []
        if not candles:
            continue
        latest = max(candles, key=lambda candle: candle.open_time_ms)
        freshness = policy.assess(
            now_ms=now_ms,
            interval=interval,
            last_confirmed_open_time_ms=latest.open_time_ms,
        )
        if freshness.state == FreshnessState.FRESH:
            continue
        return ScanDataFailure(
            ObservationFailureCode.TRUSTED_DATA_BLOCKED,
            freshness.reason,
            {
                "symbol": symbol,
                "reason": "market_data_stale",
                "interval": interval,
                "status_reason": freshness.reason.value,
                "latest_open_time_ms": latest.open_time_ms,
                "source_file": str(getattr(bundle, "files_by_interval", {}).get(interval, "")),
            },
        )
    return None
