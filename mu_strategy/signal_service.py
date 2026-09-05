from __future__ import annotations

import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from mu_strategy.demo_trading import DemoTradingConfig, run_once
from mu_strategy.market_data.service import refresh_trusted_candle_bundle
from mu_strategy.market_data.symbols import resolve_okx_swap_symbol
from mu_strategy.market_data.trusted_data.contracts import Clock, RefreshAttemptStatus, SnapshotUsability, SystemClock
from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle
from mu_strategy.market_data.trusted_data.policy import trading_strict_policy
from mu_strategy.market_data.trusted_data.store import TrustedDataStore
from mu_strategy.observations import JsonlObservationRepository, ObservationCycleInvalidError, Stage0ObservationCycle
from mu_strategy.service_health import (
    EVENT_LIMIT, MAX_STATE_BYTES, CycleHealth, HealthEvent, HealthStateError, HealthStore,
    Phase, RefreshHealth, ScanHealth, ServiceState, StepStatus, decode_health_json, health_view,
)


DEADLINE_GRACE_MS = 30_000


class InterruptedCycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServiceConfig:
    data_dir: Path = Path("data/live")
    symbols: tuple[str, ...] = ("MU-USDT-SWAP",)
    interval_seconds: int = 300
    refresh_timeout_seconds: int = 240
    scan_timeout_seconds: int = 60
    refresh_days: int = 180
    scan_days: int = 28

    def __post_init__(self) -> None:
        for value in (self.interval_seconds, self.refresh_timeout_seconds, self.scan_timeout_seconds, self.refresh_days, self.scan_days):
            if type(value) is not int or value <= 0:
                raise ValueError("service intervals, timeouts, and days must be positive integers")
        if self.scan_days > self.refresh_days:
            raise ValueError("scan days must not exceed refreshed history")
        if not self.symbols:
            raise ValueError("at least one service symbol is required")
        object.__setattr__(self, "symbols", tuple(dict.fromkeys(resolve_okx_swap_symbol(value).inst_id for value in self.symbols)))
        object.__setattr__(self, "data_dir", self.data_dir.resolve())


class ProcessRunner:
    def run(self, argv: list[str], *, timeout: int) -> subprocess.CompletedProcess:
        # subprocess.run kills and waits for its owned child on timeout or interruption.
        # Both children are single commands; neither starts a loop or another child.
        return subprocess.run(
            argv, cwd=Path(__file__).resolve().parents[1], timeout=timeout,
            capture_output=True, text=True, encoding="utf-8", check=False,
        )


class SignalService:
    def __init__(
        self, config: ServiceConfig, *, store: HealthStore | None = None, processes: ProcessRunner | None = None,
        clock: Clock | None = None, monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep, id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ):
        self.config = config
        self.store = store or HealthStore(config.data_dir)
        if self.store.data_dir != config.data_dir:
            raise ValueError("service store and configured data directory must match")
        self.processes = processes or ProcessRunner()
        self.clock = clock or SystemClock()
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.id_factory = id_factory
        self.state: ServiceState | None = None

    def run(self, *, cycles: int | None = None, on_cycle: Callable[[dict], None] = lambda value: None) -> None:
        if cycles is not None and cycles <= 0:
            raise ValueError("cycles must be positive")
        with self.store.exclusive():
            previous = self.store.read()
            if previous is not None and previous.running and previous.phase is not Phase.IDLE:
                raise InterruptedCycleError("interrupted child phase; confirm all prior workers stopped before recovery")
            now = self.clock.now_ms()
            same_symbols = previous is not None and previous.symbols == self.config.symbols
            self.state = ServiceState(
                str(self.config.data_dir), self.config.symbols, self.id_factory(), True, Phase.IDLE,
                now, now, now + DEADLINE_GRACE_MS,
                previous.last_cycle if same_symbols else None, previous.consecutive_failures if same_symbols else 0,
                previous.events if previous else (), previous.event_sequence if previous else 0,
            )
            self._event("restarted" if previous else "started", ())
            self.store.write(self.state)
            next_tick = self.monotonic()
            completed = 0
            try:
                while cycles is None or completed < cycles:
                    delay = next_tick - self.monotonic()
                    if delay > 0:
                        self.sleeper(delay)
                    self._cycle()
                    completed += 1
                    next_tick += self.config.interval_seconds
                    current_tick = self.monotonic()
                    if next_tick <= current_tick:
                        next_tick += (int((current_tick - next_tick) // self.config.interval_seconds) + 1) * self.config.interval_seconds
                    now = self.clock.now_ms()
                    self.state = replace(self.state, phase=Phase.IDLE, updated_at_ms=now,
                                         deadline_ms=now + int(max(0, next_tick - current_tick) * 1000) + DEADLINE_GRACE_MS)
                    self.store.write(self.state)
                    on_cycle(health_view(self.state, running=True, now_ms=now))
            finally:
                now = self.clock.now_ms()
                self.state = replace(self.state, running=False, phase=Phase.IDLE, updated_at_ms=now, deadline_ms=now)
                self._event("stopped", ())
                self.store.write(self.state)

    def _phase(self, phase: Phase, timeout: int) -> None:
        now = self.clock.now_ms()
        self.state = replace(self.state, phase=phase, updated_at_ms=now, deadline_ms=now + timeout * 1000 + DEADLINE_GRACE_MS)
        # Publish the active phase before spawning. A hard crash cannot leave an
        # unrecorded writer that a new supervisor silently runs beside.
        self.store.write(self.state)

    def _cycle(self) -> None:
        started = self.clock.now_ms()
        previous = self.state.last_cycle
        self._phase(Phase.REFRESH, self.config.refresh_timeout_seconds)
        refresh_args = [sys.executable, "-B", "-m", "mu_strategy.commands.refresh_market_data", "--data-dir", str(self.config.data_dir),
                        "--days", str(self.config.refresh_days), "--limit", "0", "--html-output", str(self.store.root / "data-health.html")]
        for symbol in self.config.symbols:
            refresh_args.extend(("--symbol", symbol))
        refresh = self._refresh(refresh_args)
        # A failed refresh does not restate the trusted gate. The cache-only child
        # may find a still-allowed previous generation; both facts remain visible.
        self._phase(Phase.SCAN, self.config.scan_timeout_seconds)
        scan_args = [sys.executable, "-B", "-m", "mu_strategy.commands.signal_service", "scan-once", "--data-dir", str(self.config.data_dir),
                     "--scan-days", str(self.config.scan_days), "--refresh-days", str(self.config.refresh_days)]
        for symbol in self.config.symbols:
            scan_args.extend(("--symbol", symbol))
        scan = self._scan(scan_args)
        cycle = CycleHealth(1 if previous is None else previous.number + 1, started, self.clock.now_ms(), refresh, scan)
        problems = cycle.problems()
        prior_problems = previous.problems() if previous else ()
        self.state = replace(self.state, last_cycle=cycle, updated_at_ms=cycle.completed_at_ms,
                             deadline_ms=max(self.state.deadline_ms, cycle.completed_at_ms),
                             consecutive_failures=self.state.consecutive_failures + 1 if problems else 0)
        if problems != prior_problems:
            self._event("fault" if problems else "recovered", problems)

    def _refresh(self, argv: list[str]) -> RefreshHealth:
        try:
            result = self.processes.run(argv, timeout=self.config.refresh_timeout_seconds)
        except subprocess.TimeoutExpired:
            return RefreshHealth(StepStatus.TIMED_OUT, error_code="refresh_timeout")
        except Exception:
            return RefreshHealth(StepStatus.FAILED, error_code="refresh_process_failed")
        try:
            payload = _result_json(result.stdout)
            attempt = RefreshAttemptStatus(payload["attempt_status"])
            usability = SnapshotUsability(payload["snapshot_usability"])
            if type(payload["usable"]) is not bool or payload["usable"] != (usability is SnapshotUsability.USABLE):
                raise ValueError("contradictory refresh usability")
            status = StepStatus.FAILED
            if attempt is RefreshAttemptStatus.SUCCESS and usability is SnapshotUsability.USABLE:
                status = StepStatus.SUCCEEDED
            elif attempt is RefreshAttemptStatus.DEGRADED and usability is SnapshotUsability.USABLE:
                status = StepStatus.DEGRADED
            expected_exit = {StepStatus.SUCCEEDED: 0, StepStatus.DEGRADED: 2, StepStatus.FAILED: 1}[status]
            if result.returncode != expected_exit:
                raise ValueError("refresh exit contradicts payload")
            health = RefreshHealth(status, payload["run_id"], attempt, usability, result.returncode,
                                   None if status is StepStatus.SUCCEEDED else "publication_not_fully_healthy")
            return RefreshHealth.from_dict(health.to_dict())
        except (KeyError, TypeError, ValueError):
            error = "refresh_process_failed" if result.returncode != 0 else "refresh_result_invalid"
            return RefreshHealth(StepStatus.FAILED, exit_code=result.returncode, error_code=error)

    def _scan(self, argv: list[str]) -> ScanHealth:
        try:
            result = self.processes.run(argv, timeout=self.config.scan_timeout_seconds)
        except subprocess.TimeoutExpired:
            return ScanHealth(StepStatus.TIMED_OUT, error_code="scan_timeout")
        except Exception:
            return ScanHealth(StepStatus.FAILED, error_code="scan_process_failed")
        try:
            payload = _result_json(result.stdout)
            if set(payload) != {"schema_version", "scan"} or type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
                raise ValueError("unsupported scan worker payload")
            scan = ScanHealth.from_dict(payload["scan"])
            expected_exit = 0 if scan.status is StepStatus.SUCCEEDED and scan.persistence is StepStatus.SUCCEEDED else 1
            if result.returncode != expected_exit or scan.status not in {StepStatus.SUCCEEDED, StepStatus.FAILED}:
                raise ValueError("scan exit contradicts payload")
            if scan.cycle is not None and sorted(item.symbol for item in scan.cycle.observations) != sorted(self.config.symbols):
                raise ValueError("scan did not cover configured watchlist exactly once")
            return scan
        except (KeyError, TypeError, ValueError):
            return ScanHealth(StepStatus.FAILED, error_code="scan_result_invalid")

    def _event(self, kind: str, problems: tuple[str, ...]) -> None:
        sequence = self.state.event_sequence + 1
        event = HealthEvent(sequence, self.clock.now_ms(), kind, problems)
        self.state = replace(self.state, events=(*self.state.events, event)[-EVENT_LIMIT:], event_sequence=sequence)


def recover_interrupted(store: HealthStore, *, clock: Clock | None = None) -> ServiceState:
    """Caller must have explicitly confirmed that all old worker processes stopped."""
    with store.exclusive():
        state = store.read()
        if state is None or not state.running or state.phase is Phase.IDLE:
            raise InterruptedCycleError("no interrupted child phase to acknowledge")
        now = (clock or SystemClock()).now_ms()
        sequence = state.event_sequence + 1
        event = HealthEvent(sequence, now, "interruption_acknowledged", ("runtime.interrupted",))
        state = replace(state, running=False, phase=Phase.IDLE, updated_at_ms=now, deadline_ms=now,
                        event_sequence=sequence, events=(*state.events, event)[-EVENT_LIMIT:])
        store.write(state)
        return state


class _ObservedRepository:
    def __init__(self, repository):
        self.repository = repository
        self.cycle: Stage0ObservationCycle | None = None
        self.persistence = StepStatus.NOT_RUN

    def append_cycle(self, cycle: Stage0ObservationCycle) -> None:
        self.cycle = cycle
        try:
            self.repository.append_cycle(cycle)
        except Exception:
            self.persistence = StepStatus.FAILED
            raise
        self.persistence = StepStatus.SUCCEEDED


def _watchlist_scan(config: DemoTradingConfig, **kwargs) -> dict:
    # Pin one context for the entire explicit watchlist. A broken manifest must
    # become a typed data failure for every requested symbol, not an empty universe.
    context = None
    load_error = None
    try:
        context = LoadTrustedBundle(TrustedDataStore(data_dir=config.data_dir)).open_context()
    except Exception as exc:
        load_error = exc

    def load(symbol, **query):
        if load_error is not None:
            raise load_error
        return refresh_trusted_candle_bundle(
            symbol, **query, context=context, policy=trading_strict_policy(),
            max_staleness_bars=config.max_candle_staleness_bars,
        )

    return run_once(config, **kwargs, candle_loader=load)


def scan_once(config: ServiceConfig, *, repository=None, runner=_watchlist_scan) -> ScanHealth:
    store = HealthStore(config.data_dir)
    observed = _ObservedRepository(repository if repository is not None else JsonlObservationRepository(store.root / "observations.jsonl"))
    try:
        runner(DemoTradingConfig(data_dir=config.data_dir, universe_limit=0, days=config.scan_days, dry_run=True,
                                 watchlist_symbols=config.symbols), broker=None, observation_repository=observed)
    except ObservationCycleInvalidError:
        if observed.cycle is not None and observed.persistence is StepStatus.FAILED:
            return ScanHealth(StepStatus.SUCCEEDED, observed.cycle, StepStatus.FAILED, "observation_write_failed")
        return ScanHealth(StepStatus.FAILED, error_code="scan_failed")
    except Exception:
        return ScanHealth(StepStatus.FAILED, error_code="scan_failed")
    if observed.cycle is None:
        return ScanHealth(StepStatus.FAILED, error_code="scan_result_missing")
    return ScanHealth(StepStatus.SUCCEEDED, observed.cycle, observed.persistence)


def _result_json(value: str) -> dict:
    if len(value.encode("utf-8")) > MAX_STATE_BYTES:
        raise ValueError("worker output exceeds size limit")
    result = decode_health_json(value)
    if type(result) is not dict:
        raise ValueError("worker output must be an object")
    return result
