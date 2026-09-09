"""Full public outputs frozen on main b7da71f before extracting read-only scans.

Fixtures retain every cycle, ScanHealth and Demo payload field. The service test
seam changes with its internal runner contract; the fixture must not be rewritten
to accommodate the extraction. Real reader/pointer tests live alongside these.
"""

from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from mu_strategy.demo_trading import DemoTradingConfig, run_once
from mu_strategy.market_data.trusted_data.contracts import HealthReason
from mu_strategy.models import EntryDecisionCode
from mu_strategy.observations import ObservationCycleInvalidError
from mu_strategy.readonly_scan import scan_watchlist
from mu_strategy.signal_service import ServiceConfig, scan_once
from tests.factories.scan_cycle import scan_result, trusted_scan_bundle


FIXTURE = Path(__file__).with_name("fixtures") / "readonly_scan_b7da71f.json"
SYMBOLS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
CASES = ("wait", "ready", "strategy_block", "data_missing", "data_corrupt", "stale",
         "load_error", "scanner_error", "unknown", "invalid", "missing_hash",
         "wrong_generation", "v2", "write_error", "shadow", "shadow_error")


def _inputs(case, events):
    def load(symbol, **kwargs):
        events.append(["load", symbol])
        if case == "load_error":
            raise OSError("cache unavailable")
        bundle = trusted_scan_bundle(symbol=symbol)
        if case == "data_missing":
            return replace(bundle, candles_by_interval={})
        if case in {"data_corrupt", "stale"}:
            reason = HealthReason.CACHE_CONTENT_MISMATCH if case == "data_corrupt" else HealthReason.STALE_BY_CLOCK
            return trusted_scan_bundle(symbol=symbol, allowed=False, reason=reason)
        if case == "missing_hash":
            return replace(bundle, load_context=replace(bundle.load_context,
                           manifest=replace(bundle.load_context.manifest, datasets={})))
        if case == "wrong_generation":
            return replace(bundle, run_id="different-run")
        return bundle

    def scan(symbol, *args, **kwargs):
        events.append(["scan", symbol])
        if case == "scanner_error":
            raise RuntimeError("api_key=private-marker scanner failed")
        if case == "invalid":
            return {}
        code = {
            "wait": EntryDecisionCode.WAITING_SECOND_PULLBACK,
            "strategy_block": EntryDecisionCode.REGIME_BLOCKED,
            "unknown": EntryDecisionCode.UNKNOWN,
        }.get(case, EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY)
        result = scan_result(code, symbol=symbol)
        if case == "v2":
            result = replace(result, evaluated_candle_open_ms=0, evaluated_candle_close_ms=900_000,
                             calendar_id="fixture-calendar", calendar_sha256="d" * 64,
                             calendar_session="fixture-session", trading_windows_et=(("09:45", "11:30"),))
        return result
    return load, scan


def _repository(case, events):
    repository = Mock()
    def append(cycle):
        events.append(["persist", cycle.cycle_id])
        if case == "write_error":
            raise OSError("disk full")
    repository.append_cycle.side_effect = append
    return repository


def _service_runner(loader, scanner):
    def runner(**kwargs):
        return scan_watchlist(**kwargs, candle_loader=loader, scanner=scanner,
                              observation_clock=Mock(now_ms=lambda: 1_000),
                              observation_id_factory=iter(("cycle", "observation-1", "observation-2")).__next__)
    return runner


def capture(case):
    demo_events, service_events = [], []
    demo_repo = _repository(case, demo_events)
    service_repo = _repository(case, service_events)
    loader, scanner = _inputs(case, demo_events)
    service_loader, service_scanner = _inputs(case, service_events)
    def positions():
        demo_events.append(["positions"])
        if case == "shadow_error":
            raise OSError("position source unavailable")
        return {"code": "0", "data": [{"instId": SYMBOLS[0], "pos": "1", "avgPx": "100", "lever": "5"}]}

    with TemporaryDirectory() as directory, ExitStack() as guards:
        forbidden = [guards.enter_context(patch(target, side_effect=AssertionError(target))) for target in (
            "socket.create_connection", "urllib.request.urlopen", "smtplib.SMTP", "smtplib.SMTP_SSL",
            "mu_strategy.live.okx.OKXCredentials.from_env",
            "mu_strategy.market_data.trusted_data.refresh.RefreshTrustedMarketData.execute",
        )]
        exception = None
        try:
            payload = run_once(
                DemoTradingConfig(data_dir=Path(directory), universe_limit=0,
                                  watchlist_symbols=(SYMBOLS[0], "BTC", SYMBOLS[1], SYMBOLS[0])),
                broker=None, candle_loader=loader, scanner=scanner, observation_repository=demo_repo,
                observation_clock=Mock(now_ms=lambda: 1_000),
                observation_id_factory=iter(("cycle", "observation-1", "observation-2")).__next__,
                position_source=positions if case.startswith("shadow") else None,
            )
        except ObservationCycleInvalidError as exc:
            payload = None
            exception = [type(exc).__name__, str(exc), type(exc.__cause__).__name__, str(exc.__cause__)]
        health = scan_once(ServiceConfig(data_dir=Path(directory), symbols=(SYMBOLS[0], "BTC", SYMBOLS[1])),
                           repository=service_repo, runner=_service_runner(service_loader, service_scanner))
        for guard in forbidden:
            guard.assert_not_called()
    # Compare the complete JSON wire representation (including tuple fields).
    return json.loads(json.dumps({
        "demo_payload": payload, "demo_exception": exception,
        "demo_cycle": demo_repo.append_cycle.call_args.args[0].to_dict(),
        "scan_health": health.to_dict(), "demo_events": demo_events, "service_events": service_events,
    }, allow_nan=False))


class ReadOnlyScanEquivalenceTests(unittest.TestCase):
    def test_full_cycles_health_and_demo_outputs_match_pre_extraction_baseline(self):
        expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(list(CASES), list(expected))
        for case in CASES:
            with self.subTest(case=case):
                actual = capture(case)
                self.assertEqual(expected[case], actual)
                cycle = actual["demo_cycle"]
                self.assertEqual(list(SYMBOLS), [row["symbol"] for row in cycle["observations"]])
                self.assertEqual(cycle, actual["scan_health"]["cycle"])
                self.assertNotIn("private-marker", json.dumps(actual))

    def test_dry_run_with_forbidden_broker_and_service_never_read_accounts_or_plan_orders(self):
        broker = Mock(spec=("get_positions", "get_open_orders", "get_balance", "get_instruments",
                            "set_leverage", "place_limit_buy", "cancel_order"))
        for name in broker._mock_methods:
            getattr(broker, name).side_effect = AssertionError("forbidden broker call: " + name)
        with TemporaryDirectory() as directory:
            loader, scanner = _inputs("wait", [])
            payload = run_once(DemoTradingConfig(data_dir=Path(directory), universe_limit=0,
                               watchlist_symbols=SYMBOLS), broker=broker, candle_loader=loader, scanner=scanner)
            self.assertEqual([], payload["orders"])
            self.assertEqual([], broker.method_calls)
            with patch("mu_strategy.demo_trading.run_once", side_effect=AssertionError("Demo runner")) as demo, patch(
                "mu_strategy.demo_trading._build_order_plan", side_effect=AssertionError("Demo plan")
            ) as plan, patch("mu_strategy.live.okx.OKXRestClient", side_effect=AssertionError("broker construction")) as client:
                loader, scanner = _inputs("ready", [])
                health = scan_once(ServiceConfig(data_dir=Path(directory), symbols=SYMBOLS), repository=Mock(),
                                   runner=_service_runner(loader, scanner))
                self.assertEqual("succeeded", health.status.value)
                demo.assert_not_called()
                plan.assert_not_called()
                client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
