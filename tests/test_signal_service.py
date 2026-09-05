import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from mu_strategy.commands.signal_service import main
from mu_strategy.demo_trading import run_once
from mu_strategy.market_data.trusted_data.contracts import HealthReason
from mu_strategy.models import EntryDecisionCode
from mu_strategy.observations import JsonlObservationRepository, ObservationOutcome
from mu_strategy.scan_cycle import ScanCycle
from mu_strategy.service_health import (
    EVENT_LIMIT, CycleHealth, HealthEvent, HealthStateError, HealthStore, Phase,
    RefreshHealth, ScanHealth, ServiceBusyError, ServiceState, StepStatus, health_view,
)
from mu_strategy.signal_service import (
    InterruptedCycleError, ProcessRunner, ServiceConfig, SignalService, recover_interrupted, scan_once,
)
from mu_strategy.strategies.registry import baseline_strategy_group
from tests.factories.scan_cycle import scan_result, trusted_scan_bundle
from tests.factories.trusted_publication import write_generation_manifest_and_caches


SYMBOL = "MU-USDT-SWAP"


class FakeTime:
    def __init__(self):
        self.value = 1000

    def now_ms(self):
        return self.value

    def monotonic(self):
        return self.value / 1000

    def sleep(self, seconds):
        self.value += int(seconds * 1000)


def observation_cycle(kind="wait", symbol=SYMBOL):
    cycle = ScanCycle(clock=FakeTime(), id_factory=iter(("cycle", "observation")).__next__)
    group = baseline_strategy_group(symbol)
    bundle = trusted_scan_bundle(symbol=symbol, allowed=kind != "blocked",
                                 reason=HealthReason.STALE_BY_CLOCK if kind == "blocked" else HealthReason.OK)
    code = EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY if kind == "ready" else EntryDecisionCode.WAITING_SECOND_PULLBACK
    scanner = Mock(side_effect=RuntimeError("scanner unavailable")) if kind == "failed" else Mock(return_value=scan_result(code, symbol=symbol))
    cycle.scan_symbol(symbol=symbol, source="watchlist", bundle=bundle, requested_intervals=("15m", "1h"),
                      strategy_name=group.name, strategy_config=group.config, scanner=scanner, data_failure=None)
    return cycle.observations()


def refresh_result(*, attempt="success", usable=True, exit_code=0):
    return subprocess.CompletedProcess([], exit_code, json.dumps({
        "run_id": "trusted-run", "attempt_status": attempt,
        "snapshot_usability": "usable" if usable else "unusable", "usable": usable,
    }), "provider diagnostic that must not leak")


def scan_result_process(kind="wait", *, persistence=StepStatus.SUCCEEDED, exit_code=None, symbol=SYMBOL):
    scan = ScanHealth(StepStatus.SUCCEEDED, observation_cycle(kind, symbol), persistence,
                      "observation_write_failed" if persistence is StepStatus.FAILED else None)
    return subprocess.CompletedProcess([], (0 if persistence is StepStatus.SUCCEEDED else 1) if exit_code is None else exit_code,
                                       json.dumps({"schema_version": 1, "scan": scan.to_dict()}), "")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = ServiceConfig(data_dir=Path(self.temp.name) / "live", interval_seconds=10)
        self.store = HealthStore(self.config.data_dir)
        self.time = FakeTime()

    def service(self, results, *, on_process=None):
        def run(argv, *, timeout):
            if on_process:
                on_process(argv, timeout)
            result = next(iterator)
            if isinstance(result, BaseException):
                raise result
            return result
        iterator = iter(results)
        return SignalService(self.config, store=self.store, processes=Mock(run=Mock(side_effect=run)), clock=self.time,
                             monotonic=self.time.monotonic, sleeper=self.time.sleep, id_factory=lambda: "service-run")

    def test_refresh_precedes_cache_only_scan_with_active_phase_persisted_before_spawn(self):
        seen = []
        def inspect(argv, timeout):
            seen.append((argv, timeout, self.store.read().phase))
            self.assertTrue(self.store.is_running())
        views = []
        service = self.service([refresh_result(), scan_result_process()], on_process=inspect)
        service.run(cycles=1, on_cycle=views.append)
        self.assertEqual([Phase.REFRESH, Phase.SCAN], [item[2] for item in seen])
        self.assertEqual("mu_strategy.commands.refresh_market_data", seen[0][0][3])
        self.assertEqual("mu_strategy.commands.signal_service", seen[1][0][3])
        self.assertIn("scan-once", seen[1][0])
        for argv, timeout, _ in seen:
            self.assertEqual(sys.executable, argv[0])
            self.assertIn(str(self.config.data_dir), argv)
            self.assertIn(SYMBOL, argv)
            self.assertNotIn("--loop", argv)
            self.assertNotIn("--demo", argv)
        self.assertEqual([240, 60], [item[1] for item in seen])
        self.assertTrue(views[0]["healthy"])
        self.assertEqual("allowed", views[0]["data_at_last_scan"]["status"])
        self.assertFalse(self.store.is_running())
        state = self.store.read()
        self.assertFalse(state.running)
        self.assertEqual(["started", "stopped"], [event.kind for event in state.events])
        self.assertEqual("stopped", health_view(state, running=False, now_ms=self.time.now_ms())["runtime"])

    def test_outcome_and_persistence_health_are_independent(self):
        for kind, persistence, expected in [
            ("wait", StepStatus.SUCCEEDED, ()), ("ready", StepStatus.SUCCEEDED, ()),
            ("blocked", StepStatus.SUCCEEDED, ("data.blocked",)),
            ("failed", StepStatus.SUCCEEDED, ("scan.failed",)),
            ("ready", StepStatus.FAILED, ("persistence.failed",)),
        ]:
            with self.subTest(kind=kind, persistence=persistence):
                views = []
                self.service([refresh_result(), scan_result_process(kind, persistence=persistence)]).run(cycles=1, on_cycle=views.append)
                self.assertEqual(list(expected), views[0]["problems"])
                self.assertEqual(not expected, views[0]["healthy"])

    def test_failures_retry_and_recovery_event_is_durable_and_not_repeated(self):
        views = []
        results = [refresh_result(), scan_result_process("blocked")] * 2 + [refresh_result(), scan_result_process()] * 2
        self.service(results).run(cycles=4, on_cycle=views.append)
        self.assertEqual([1, 2, 0, 0], [view["consecutive_failures"] for view in views])
        self.assertEqual(["started", "fault", "recovered", "stopped"], [event.kind for event in self.store.read().events])
        self.assertEqual(4, self.store.read().last_cycle.number)
        self.assertEqual(31000, self.time.now_ms())

    def test_refresh_failure_still_scans_previous_cache_but_service_remains_unhealthy(self):
        for result, expected in [
            (refresh_result(attempt="degraded", exit_code=2), StepStatus.DEGRADED),
            (subprocess.CompletedProcess([], 1, '{"status":"error"}', "secret provider text"), StepStatus.FAILED),
            (subprocess.TimeoutExpired("refresh", 240), StepStatus.TIMED_OUT),
            (OSError("cannot spawn"), StepStatus.FAILED),
        ]:
            with self.subTest(expected=expected):
                views = []
                self.service([result, scan_result_process()]).run(cycles=1, on_cycle=views.append)
                self.assertFalse(views[0]["healthy"])
                self.assertEqual("allowed", views[0]["data_at_last_scan"]["status"])
                self.assertIs(expected, self.store.read().last_cycle.refresh.status)
                self.assertNotIn("secret provider text", self.store.path.read_text())

    def test_malformed_or_contradictory_refresh_success_fails_closed(self):
        for result in [refresh_result(exit_code=1), refresh_result(attempt="degraded"), refresh_result(usable=False),
                       subprocess.CompletedProcess([], 0, "not-json", ""),
                       subprocess.CompletedProcess([], 0, '{"attempt_status":"success","snapshot_usability":"usable","usable":1}', "")]:
            with self.subTest(stdout=result.stdout):
                actual = self.service([result])._refresh([])
                self.assertIs(StepStatus.FAILED, actual.status)

    def test_scan_timeout_exception_wrong_watchlist_and_payload_are_not_success(self):
        for result, expected in [
            (subprocess.TimeoutExpired("scan", 60), StepStatus.TIMED_OUT), (OSError(), StepStatus.FAILED),
            (scan_result_process(symbol="BTC-USDT-SWAP"), StepStatus.FAILED),
            (scan_result_process(exit_code=1), StepStatus.FAILED),
            (subprocess.CompletedProcess([], 0, '{}', ''), StepStatus.FAILED),
        ]:
            with self.subTest(expected=expected):
                scan = self.service([result])._scan([])
                self.assertIs(expected, scan.status)
                self.assertIs(StepStatus.NOT_RUN, scan.persistence)
                self.assertIsNone(scan.cycle)

    def test_clean_restart_retains_counter_and_recovers_after_first_new_scan(self):
        self.service([refresh_result(), scan_result_process("blocked")]).run(cycles=1)
        self.time.sleep(1)
        startup = []
        def inspect(argv, timeout):
            startup.append(health_view(self.store.read(), running=True, now_ms=self.time.now_ms()))
        self.service([refresh_result(), scan_result_process()], on_process=inspect).run(cycles=1)
        self.assertFalse(startup[0]["healthy"])
        self.assertIn("no_completed_cycle_in_current_run", startup[0]["problems"])
        self.assertEqual(2, self.store.read().last_cycle.number)
        self.assertEqual(["started", "fault", "stopped", "restarted", "recovered", "stopped"], [e.kind for e in self.store.read().events])

    def test_single_instance_excludes_second_runner_without_overwriting_state(self):
        self.service([refresh_result(), scan_result_process()]).run(cycles=1)
        before = self.store.path.read_bytes()
        with self.store.exclusive():
            with self.assertRaises(ServiceBusyError):
                self.service([]).run(cycles=1)
            self.assertTrue(self.store.is_running())
            self.assertEqual(before, self.store.path.read_bytes())
        self.assertFalse(self.store.is_running())

    def test_interrupted_active_phase_requires_explicit_recovery_before_restart(self):
        self.service([refresh_result(), scan_result_process()]).run(cycles=1)
        for phase in (Phase.REFRESH, Phase.SCAN):
            with self.subTest(phase=phase):
                self.store.write(replace(self.store.read(), running=True, phase=phase))
                before = self.store.path.read_bytes()
                self.assertEqual("interrupted", health_view(self.store.read(), running=False, now_ms=1000)["runtime"])
                with self.assertRaises(InterruptedCycleError):
                    self.service([]).run(cycles=1)
                self.assertEqual(before, self.store.path.read_bytes())
                recover_interrupted(self.store, clock=self.time)
                self.assertEqual("interruption_acknowledged", self.store.read().events[-1].kind)
                self.service([refresh_result(), scan_result_process()]).run(cycles=1)

    def test_interruption_during_idle_can_restart_and_busy_service_cannot_be_recovered(self):
        self.service([refresh_result(), scan_result_process()]).run(cycles=1)
        self.store.write(replace(self.store.read(), running=True, phase=Phase.IDLE))
        with self.store.exclusive():
            with self.assertRaises(ServiceBusyError):
                recover_interrupted(self.store)
        self.service([refresh_result(), scan_result_process()]).run(cycles=1)
        with self.assertRaises(InterruptedCycleError):
            recover_interrupted(self.store)

    def test_keyboard_interrupt_stops_and_releases_service_lock(self):
        with self.assertRaises(KeyboardInterrupt):
            self.service([KeyboardInterrupt()]).run(cycles=1)
        self.assertFalse(self.store.is_running())
        self.assertFalse(self.store.read().running)

    def test_slow_cycle_skips_missed_ticks_without_overlapping_workers(self):
        def inspect(argv, timeout):
            self.time.sleep(12)
        self.service([refresh_result(), scan_result_process()] * 2, on_process=inspect).run(cycles=2)
        self.assertEqual(55000, self.time.now_ms())

    def test_config_rejects_invalid_bounds_and_normalizes_symbols(self):
        for kwargs in ({"interval_seconds": 0}, {"scan_timeout_seconds": -1}, {"refresh_days": True},
                       {"symbols": ()}, {"scan_days": 181}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ServiceConfig(**kwargs)
        self.assertEqual((SYMBOL,), ServiceConfig(symbols=("MU", SYMBOL)).symbols)

    def test_health_round_trip_and_invalid_state_matrix(self):
        self.service([refresh_result(), scan_result_process()]).run(cycles=1)
        original = self.store.read()
        self.assertEqual(original, ServiceState.from_dict(original.to_dict()))
        cases = [dict(schema_version=True), dict(schema_version=99), dict(running="true"), dict(phase="other"),
                 dict(symbols=[]), dict(symbols=[SYMBOL, SYMBOL]), dict(event_sequence=100), dict(deadline_ms=0),
                 dict(consecutive_failures=-1), dict(started_at_ms=2000), dict(extra=True)]
        for update in cases:
            with self.subTest(update=update), self.assertRaises((HealthStateError, ValueError)):
                ServiceState.from_dict(original.to_dict() | update)
        wrong_cycle = replace(original.last_cycle.scan, cycle=observation_cycle(symbol="BTC-USDT-SWAP"))
        with self.assertRaises(HealthStateError):
            self.store.write(replace(original, last_cycle=replace(original.last_cycle, scan=wrong_cycle)))
        self.assertEqual(original, self.store.read())

    def test_atomic_write_failure_preserves_old_or_new_complete_snapshot(self):
        self.service([refresh_result(), scan_result_process()]).run(cycles=1)
        old = self.store.read()
        new = replace(old, run_id="replacement")
        with patch("mu_strategy.service_health.os.replace", side_effect=OSError("before commit")):
            with self.assertRaises(OSError):
                self.store.write(new)
        self.assertEqual(old, self.store.read())
        def sync(directory):
            if directory == self.store.root:
                raise OSError("after commit")
        with patch("mu_strategy.service_health.fsync_directory", side_effect=sync):
            with self.assertRaises(OSError):
                self.store.write(new)
        self.assertEqual(new, self.store.read())
        self.assertEqual([], list(self.store.root.glob("health-*.tmp")))

    def test_corrupt_unknown_and_foreign_state_are_not_replaced_on_start(self):
        self.store.root.mkdir()
        for raw in (b"{", b"{}", b'{"schema_version":99}'):
            with self.subTest(raw=raw):
                self.store.path.write_bytes(raw)
                with self.assertRaises(HealthStateError):
                    self.service([]).run(cycles=1)
                self.assertEqual(raw, self.store.path.read_bytes())
        self.store.path.unlink()
        self.service([refresh_result(), scan_result_process()]).run(cycles=1)
        foreign = self.store.read().to_dict() | {"data_dir": str(Path(self.temp.name) / "foreign")}
        self.store.path.write_text(json.dumps(foreign))
        with self.assertRaises(HealthStateError):
            self.store.read()

    def test_cursor_retention_is_bounded_and_detects_missed_history(self):
        self.service([refresh_result(), scan_result_process()]).run(cycles=1)
        events = tuple(HealthEvent(i, 1000, "fault", ("data.blocked",)) for i in range(2, EVENT_LIMIT + 2))
        state = replace(self.store.read(), events=events, event_sequence=EVENT_LIMIT + 1)
        self.store.write(state)
        self.assertEqual(EVENT_LIMIT, len(self.store.read().events_since(1)))
        self.assertEqual((), self.store.read().events_since(EVENT_LIMIT + 1))
        for cursor in (0, EVENT_LIMIT + 2, -1, True):
            with self.subTest(cursor=cursor), self.assertRaises(HealthStateError):
                self.store.read().events_since(cursor)

    def test_process_liveness_alone_cannot_make_health_green(self):
        views = []
        self.service([refresh_result(), scan_result_process()]).run(cycles=1, on_cycle=views.append)
        state = replace(self.store.read(), running=True, deadline_ms=2000)
        for now, running, expected in ((2001, True, "unresponsive"), (999, True, "unresponsive"), (1000, False, "interrupted")):
            view = health_view(state, running=running, now_ms=now)
            self.assertEqual(expected, view["runtime"])
            self.assertFalse(view["healthy"])
        self.assertFalse(health_view(replace(state, running=False), running=True, now_ms=1000)["healthy"])

    def test_status_missing_or_corrupt_is_read_only_and_nonzero(self):
        output = io.StringIO()
        self.assertEqual(2, main(["status", "--data-dir", str(self.config.data_dir)], stdout=output))
        self.assertEqual("not_started", json.loads(output.getvalue())["runtime"])
        self.assertFalse(self.store.root.exists())
        self.store.root.mkdir()
        self.store.path.write_text("bad state")
        output = io.StringIO()
        self.assertEqual(2, main(["status", "--data-dir", str(self.config.data_dir)], stdout=output))
        self.assertFalse(json.loads(output.getvalue())["healthy"])
        self.assertEqual("bad state", self.store.path.read_text())
        self.assertFalse(self.store.lock_path.exists())

    def test_once_cli_returns_cycle_health_and_writes_diagnostic_log(self):
        for kind, exit_code in (("wait", 0), ("blocked", 2)):
            with self.subTest(kind=kind):
                output = io.StringIO()
                service = self.service([refresh_result(), scan_result_process(kind)])
                result = main(["run", "--once", "--data-dir", str(self.config.data_dir)], stdout=output,
                              service_factory=lambda config: service)
                self.assertEqual(exit_code, result)
                self.assertEqual(exit_code == 0, json.loads(output.getvalue())["healthy"])
                self.assertIn('"healthy"', (self.store.root / "service.log").read_text())

    def test_recover_cli_requires_explicit_acknowledgment_and_rejects_trading_flags(self):
        for argv in (["recover"], ["run", "--demo"], ["run", "--confirm-live-risk"]):
            with self.subTest(argv=argv), patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as error:
                main(argv, stdout=io.StringIO())
            self.assertEqual(2, error.exception.code)

    def test_status_after_event_returns_only_new_events_and_rejects_future_cursor(self):
        self.service([refresh_result(), scan_result_process()]).run(cycles=1)
        output = io.StringIO()
        args = ["status", "--data-dir", str(self.config.data_dir), "--after-event"]
        self.assertEqual(2, main([*args, "1"], stdout=output))
        self.assertEqual(["stopped"], [event["kind"] for event in json.loads(output.getvalue())["events"]])
        output = io.StringIO()
        self.assertEqual(2, main([*args, "99"], stdout=output))
        self.assertIn("error_code", json.loads(output.getvalue()))

    def test_actual_scan_path_is_cache_only_and_retains_persistence_failure_evidence(self):
        def runner(config, **kwargs):
            self.assertTrue(config.dry_run)
            self.assertIsNone(kwargs["broker"])
            return run_once(config, **kwargs, candle_loader=lambda *args, **kw: trusted_scan_bundle(symbol=SYMBOL),
                            scanner=lambda *args, **kw: scan_result(EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY, symbol=SYMBOL))
        for broken in (False, True):
            with self.subTest(broken=broken):
                repository = Mock()
                if broken:
                    repository.append_cycle.side_effect = OSError("disk full")
                with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=1000):
                    scan = scan_once(self.config, repository=repository, runner=runner)
                self.assertIs(StepStatus.SUCCEEDED, scan.status)
                self.assertIs(StepStatus.FAILED if broken else StepStatus.SUCCEEDED, scan.persistence)
                self.assertIs(ObservationOutcome.READY_FOR_REVIEW, scan.cycle.observations[0].outcome)
                repository.append_cycle.assert_called_once_with(scan.cycle)

    def test_process_runner_uses_single_command_without_shell_and_propagates_timeout(self):
        with patch("mu_strategy.signal_service.subprocess.run", side_effect=subprocess.TimeoutExpired("worker", 3)) as child:
            with self.assertRaises(subprocess.TimeoutExpired):
                ProcessRunner().run([sys.executable, "-B", "-m", "worker"], timeout=3)
        self.assertEqual(3, child.call_args.kwargs["timeout"])
        self.assertNotIn("shell", child.call_args.kwargs)
        self.assertEqual(Path(__file__).resolve().parents[1], child.call_args.kwargs["cwd"])

    def test_real_cache_worker_reports_missing_invalid_and_stale_data_without_network(self):
        config = replace(self.config, symbols=(SYMBOL, "BTC-USDT-SWAP"), scan_days=1)
        for state in ("missing", "invalid", "stale"):
            with self.subTest(state=state):
                if state == "invalid":
                    config.data_dir.mkdir()
                    (config.data_dir / "current.json").write_text("broken pointer")
                if state == "stale":
                    (config.data_dir / "current.json").unlink()
                    write_generation_manifest_and_caches(config.data_dir, symbol=SYMBOL, days=1)
                with patch("socket.create_connection", side_effect=AssertionError("no network")) as network, patch(
                    "mu_strategy.live.okx.OKXCredentials.from_env", side_effect=AssertionError("no credentials")
                ) as credentials:
                    result = scan_once(config)
                self.assertIs(StepStatus.SUCCEEDED, result.status)
                self.assertIs(StepStatus.SUCCEEDED, result.persistence)
                self.assertEqual(2, len(result.cycle.observations))
                self.assertTrue(all(item.outcome is ObservationOutcome.DATA_GATE_BLOCKED for item in result.cycle.observations))
                network.assert_not_called()
                credentials.assert_not_called()
                if state == "stale":
                    self.assertIn(HealthReason.STALE_BY_CLOCK, {item.trust_reason for item in result.cycle.observations})
        stored = JsonlObservationRepository(self.store.root / "observations.jsonl").read_cycles()
        self.assertEqual(3, len(stored))

    def test_fresh_real_cache_worker_pins_one_context_and_preserves_current_pointer(self):
        from mu_strategy.market_data.trusted_data.load import LoadTrustedBundle
        write_generation_manifest_and_caches(self.config.data_dir, symbol=SYMBOL, days=2)
        pointer = (self.config.data_dir / "current.json").read_bytes()
        original = LoadTrustedBundle.open_context
        with patch("mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=172800000), patch.object(
            LoadTrustedBundle, "open_context", autospec=True, side_effect=original
        ) as opened, patch("socket.create_connection", side_effect=AssertionError("no network")) as network:
            scan = scan_once(replace(self.config, scan_days=2))
        self.assertIs(StepStatus.SUCCEEDED, scan.status)
        self.assertTrue(scan.cycle.observations[0].trust_allowed)
        self.assertNotIn(scan.cycle.observations[0].outcome, {ObservationOutcome.DATA_GATE_BLOCKED, ObservationOutcome.SCAN_FAILED})
        self.assertEqual(1, opened.call_count)
        network.assert_not_called()
        self.assertEqual(pointer, (self.config.data_dir / "current.json").read_bytes())
        self.assertEqual((scan.cycle,), JsonlObservationRepository(self.store.root / "observations.jsonl").read_cycles())

    def test_real_worker_process_emits_valid_blocked_observations_with_missing_cache(self):
        result = ProcessRunner().run([sys.executable, "-B", "-m", "mu_strategy.commands.signal_service", "scan-once",
                                      "--data-dir", str(self.config.data_dir)], timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        scan = ScanHealth.from_dict(json.loads(result.stdout)["scan"])
        self.assertIs(ObservationOutcome.DATA_GATE_BLOCKED, scan.cycle.observations[0].outcome)
        self.assertIs(StepStatus.SUCCEEDED, scan.persistence)

    def test_os_lock_releases_after_owning_process_dies(self):
        script = "from pathlib import Path; import sys; from mu_strategy.service_health import HealthStore\nwith HealthStore(Path(sys.argv[1])).exclusive():\n print('locked', flush=True)\n sys.stdin.readline()\n"
        process = subprocess.Popen([sys.executable, "-B", "-c", script, str(self.config.data_dir)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual("locked", process.stdout.readline().strip())
            self.assertTrue(self.store.is_running())
            with self.assertRaises(ServiceBusyError):
                self.service([]).run(cycles=1)
            process.terminate()
            process.communicate(timeout=10)
            self.assertFalse(self.store.is_running())
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)

    def test_publication_drift_and_contradictory_success_are_not_healthy(self):
        self.service([refresh_result(), scan_result_process()]).run(cycles=1)
        cycle = self.store.read().last_cycle
        changed = replace(cycle, refresh=replace(cycle.refresh, run_id="other-generation"))
        self.assertIn("data.publication_changed", changed.problems())
        with self.assertRaises(HealthStateError):
            RefreshHealth.from_dict(cycle.refresh.to_dict() | {"error_code": "failed"})
        with self.assertRaises(HealthStateError):
            ScanHealth.from_dict(cycle.scan.to_dict() | {"error_code": "failed"})

    def test_duplicate_json_fields_and_empty_cycles_fail_closed(self):
        self.service([refresh_result(), scan_result_process()]).run(cycles=1)
        raw = self.store.path.read_text()
        self.store.path.write_text(raw.replace('"schema_version":1', '"schema_version":99,"schema_version":1', 1))
        with self.assertRaises(HealthStateError):
            self.store.read()
        empty = ScanCycle(clock=self.time).observations()
        payload = {"schema_version": 1, "scan": ScanHealth(StepStatus.SUCCEEDED, empty, StepStatus.SUCCEEDED).to_dict()}
        actual = self.service([subprocess.CompletedProcess([], 0, json.dumps(payload), "")])._scan([])
        self.assertIs(StepStatus.FAILED, actual.status)

    def test_concurrent_status_probes_cannot_impersonate_a_dead_supervisor(self):
        import mu_strategy.service_health as health
        self.service([refresh_result(), scan_result_process()]).run(cycles=1)
        self.store.write(replace(self.store.read(), running=True, deadline_ms=2000))
        acquired, release = threading.Event(), threading.Event()
        original_lock = health._lock
        def lock(stream, **kwargs):
            original_lock(stream, **kwargs)
            if kwargs.get("shared") and threading.current_thread().name.startswith("first-probe"):
                acquired.set()
                if not release.wait(10):
                    raise AssertionError("test probe was not released")
        def query():
            output = io.StringIO()
            code = main(["status", "--data-dir", str(self.config.data_dir)], stdout=output)
            return code, json.loads(output.getvalue())
        with patch.object(health, "_lock", side_effect=lock), patch(
            "mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms", return_value=1000
        ), ThreadPoolExecutor(max_workers=1, thread_name_prefix="first-probe") as pool:
            first = pool.submit(query)
            try:
                self.assertTrue(acquired.wait(10))
                code, result = query()
                self.assertEqual(2, code)
                self.assertEqual("interrupted", result["runtime"])
                self.assertFalse(result["healthy"])
            finally:
                release.set()
            self.assertEqual(2, first.result(timeout=10)[0])

    def test_first_start_waits_for_read_probe_without_false_instance_conflict(self):
        import mu_strategy.service_health as health
        self.store.prepare()
        waiting = threading.Event()
        original_lock = health._lock
        def lock(stream, **kwargs):
            if kwargs.get("wait"):
                waiting.set()
            return original_lock(stream, **kwargs)
        with self.store.liveness_path.open("a+b") as probe:
            original_lock(probe, shared=True)
            with patch.object(health, "_lock", side_effect=lock), ThreadPoolExecutor(max_workers=1) as pool:
                service = pool.submit(self.service([refresh_result(), scan_result_process()]).run, cycles=1)
                try:
                    self.assertTrue(waiting.wait(10))
                    self.assertFalse(self.store.is_running())
                    self.assertFalse(service.done())
                finally:
                    health._unlock(probe)
                service.result(timeout=10)
        self.assertEqual(1, self.store.read().last_cycle.number)

    def test_long_window_configuration_reaches_real_scan_worker(self):
        config = replace(self.config, refresh_days=365, scan_days=200)
        def run(argv, *, timeout):
            if argv[3] == "mu_strategy.commands.refresh_market_data":
                return refresh_result()
            return ProcessRunner().run(argv, timeout=timeout)
        SignalService(config, processes=Mock(run=run), clock=self.time).run(cycles=1)
        scan = self.store.read().last_cycle.scan
        self.assertIs(StepStatus.SUCCEEDED, scan.status)
        self.assertIs(StepStatus.SUCCEEDED, scan.persistence)
        self.assertIs(ObservationOutcome.DATA_GATE_BLOCKED, scan.cycle.observations[0].outcome)

    def test_new_nested_directory_entries_are_synced_before_any_worker(self):
        import mu_strategy.service_health as health
        nested = Path(self.temp.name) / "new-parent" / "nested"
        self.config = replace(self.config, data_dir=nested / "live")
        self.store = HealthStore(self.config.data_dir)
        synced = []
        def sync(directory):
            synced.append(directory)
        def inspect(argv, timeout):
            self.assertEqual([Path(self.temp.name).parent, Path(self.temp.name), nested.parent, nested], synced[:4])
        with patch.object(health, "fsync_directory", side_effect=sync):
            self.service([refresh_result(), scan_result_process()], on_process=inspect).run(cycles=1)

    def test_parent_sync_failure_prevents_first_worker_and_health_publication(self):
        service = self.service([refresh_result(), scan_result_process()])
        with patch("mu_strategy.service_health.fsync_directory", side_effect=OSError("directory sync failed")):
            with self.assertRaises(OSError):
                service.run(cycles=1)
        service.processes.run.assert_not_called()
        self.assertFalse(self.store.path.exists())

    def test_retry_repairs_last_directory_entry_after_partial_prepare_failure(self):
        nested = Path(self.temp.name) / "new-parent" / "nested"
        store = HealthStore(nested / "live")
        def fail_at_new_parent(directory):
            if directory == Path(self.temp.name):
                raise OSError("new parent entry not synced")
        with patch("mu_strategy.service_health.fsync_directory", side_effect=fail_at_new_parent):
            with self.assertRaises(OSError):
                store.prepare()
        self.assertTrue(nested.parent.exists())
        self.assertFalse(nested.exists())
        with patch("mu_strategy.service_health.fsync_directory") as sync:
            store.prepare()
        self.assertEqual(Path(self.temp.name), sync.call_args_list[0].args[0])


if __name__ == "__main__":
    unittest.main()
