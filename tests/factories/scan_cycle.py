from pathlib import Path

from mu_strategy.market_data.service import CandleBundle
from mu_strategy.market_data.symbols import ResolvedSymbol
from mu_strategy.market_data.trusted_data.contracts import (
    AvailabilityState,
    DatasetHealth,
    DatasetKey,
    FreshnessState,
    HealthReason,
    IntegrityState,
    RefreshAttemptStatus,
    SnapshotUsability,
    TrustDecision,
    TrustedLoadContext,
    TrustedManifestSnapshot,
    UniverseSnapshot,
)
from mu_strategy.entry.scanner import EntryScanResult
from mu_strategy.models import Candle


def trusted_scan_bundle(*, symbol="BTC-USDT-SWAP", allowed=True, reason=HealthReason.OK):
    intervals = ("5m", "15m", "1h")
    hashes = {"5m": "a" * 64, "15m": "b" * 64, "1h": "c" * 64}
    health = {
        (symbol, interval): DatasetHealth(
            key=DatasetKey(symbol, interval),
            availability=AvailabilityState.AVAILABLE,
            integrity=IntegrityState.VALID,
            freshness=FreshnessState.FRESH,
            reasons=(HealthReason.OK,),
            rows=40,
            first_timestamp_ms=0,
            last_timestamp_ms=39,
            source_file=Path(f"{interval}.csv"),
            content_sha256=hashes[interval],
        )
        for interval in intervals
    }
    manifest = TrustedManifestSnapshot(
        schema_version=3,
        run_id="trusted-run",
        attempt_status=RefreshAttemptStatus.SUCCESS,
        snapshot_usability=SnapshotUsability.USABLE,
        started_at_ms=0,
        completed_at_ms=900,
        requested_intervals=("15m", "1h"),
        effective_intervals=intervals,
        universe_snapshot=UniverseSnapshot(),
        datasets=health,
    )
    context = TrustedLoadContext(
        manifest=manifest,
        observed_at_ms=900,
        generation_root=Path("generation"),
        generation_id="trusted-run",
    )
    candles = [Candle(index, 100.0, 101.0, 99.0, 100.0, 10.0) for index in range(40)]
    return CandleBundle(
        symbol=ResolvedSymbol(requested=symbol, inst_id=symbol, source="okx"),
        candles_by_interval={"15m": candles, "1h": candles},
        files_by_interval={"15m": Path("15m.csv"), "1h": Path("1h.csv")},
        days=1,
        run_id="trusted-run",
        trust_decision=TrustDecision(allowed, reason),
        load_context=context,
        observed_at_ms=900,
    )


def scan_result(decision_code, *, symbol="BTC-USDT-SWAP", action="wait", reason="typed scan result"):
    return EntryScanResult(
        symbol=symbol,
        action=action,
        reason=reason,
        last_close=100.0,
        regime_1h="green",
        rsi14=55.0,
        macd_hist=0.2,
        macd_hist_prev=0.1,
        fib_level=99.0,
        fib_distance_pct=0.01,
        trigger_price=99.0,
        initial_stop=97.0,
        signal_time_ms=42,
        decision_code=decision_code,
    )


