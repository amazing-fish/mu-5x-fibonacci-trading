import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from uuid import uuid4

from mu_strategy.manual_positions import ManualPositionLedger
from mu_strategy.market_data.trusted_data.contracts import HealthReason, TrustedBundle, TrustDecision
from mu_strategy.market_data.trusted_data.refresh import RefreshTrustedMarketData, RefreshTrustedMarketDataRequest
from mu_strategy.market_data.trusted_data.store import TrustedDataStore
from mu_strategy.market_data.trusted_data.validation import aggregate_candles
from mu_strategy.models import Candle
from mu_strategy.position_management import BAR_MS, baseline_configuration, project_rule_fills, review_position
from mu_strategy.signal_review_server import make_review_server
from mu_strategy.viz.position_ledger import render_position_management_editor
from mu_strategy.viz.signal_review import render_signal_review
from tests import test_manual_positions as manual
from tests import test_signal_review as fixtures
from tests import test_trusted_segmented_storage as storage_fixtures
from tests.factories.scan_cycle import trusted_scan_bundle


class PositionManagementFixture(manual.ManualPositionTestCase):
    def position(self, identity):
        return next(row for row in self.ledger.read() if row["position_id"] == identity)

    def state(self, identity, *, at=fixtures.NOW, stage="1", stop="95"):
        position = self.position(identity)
        self.ledger.save_state({"request_id": uuid4().hex, "position_id": identity, "confirmed": "yes",
                                "expected_fill_sequence": str(position["fill_sequence"]),
                                "expected_state_revision": str(position["current_state"]["revision"]),
                                "stage": stage, "stop_price": stop}, now_ms=at)

    def management_payload(self, identity, **changes):
        position = self.position(identity)
        template = position["management_inputs"]["latest"] or baseline_configuration(position["symbol"])
        return {"request_id": uuid4().hex, "position_id": identity, "confirmed": "yes",
                "expected_fill_sequence": str(position["fill_sequence"]),
                "expected_state_revision": str(position["current_state"]["revision"]),
                "expected_management_revision": str(position["management_inputs"]["revision"]),
                "configuration_sha256": template["configuration_sha256"], "entry_anchor": "100",
                "initial_stop_price": "95", "actual_leverage": "2", "note": "已核对实际杠杆与成交归属",
                **{f'fill_stage_{row["fill_id"]}': "1" for row in position["fills"]
                   if row["action"] == "buy" and not row["voided"]}, **changes}

    def confirm(self, identity, *, at=fixtures.NOW, **changes):
        payload = self.management_payload(identity, **changes)
        self.ledger.save_management(payload, now_ms=at)
        return payload

    def ready(self, *, at=fixtures.NOW, stop="95"):
        identity = self.save(self.payload())
        self.state(identity, at=at, stop=stop)
        self.confirm(identity, at=at)
        return identity

    def loader(self, *, end=fixtures.NOW + 4 * BAR_MS, lows=None, trend=False):
        original = trusted_scan_bundle(symbol=fixtures.SYMBOL)
        candles = {}
        for interval, step, count in (("15m", BAR_MS, 320), ("1h", 4 * BAR_MS, 80)):
            stop = end // step * step
            rows = []
            for index, at in enumerate(range(stop - count * step, stop, step)):
                price = 100 + index ** 3 / 2_000_000 if trend else 100
                low = (lows or {}).get(at, price - 1)
                rows.append(Candle(at, price, price + 1, low, price, 10))
            candles[interval] = rows
        context = replace(original.load_context, observed_at_ms=end)
        bundle = TrustedBundle(fixtures.SYMBOL, candles, {}, 7,
                               {interval: context.manifest.datasets[(fixtures.SYMBOL, interval)] for interval in ("5m", "15m", "1h")},
                               TrustDecision(True, HealthReason.OK), context.manifest.run_id, load_context=context)
        loader = Mock()
        loader.open_context.return_value = context
        loader.execute.return_value = bundle
        return loader

    def review(self, identity, loader=None, *, at=fixtures.NOW + 4 * BAR_MS):
        return review_position(self.position(identity), self.fixture.data_dir, now_ms=at, loader=loader)


class PositionManagementTests(PositionManagementFixture):
    def test_read_only_old_ledger_and_new_inputs_do_not_change_actual_facts(self):
        identity = self.save(self.payload(stage="4", stop_price="80"))
        before = self.ledger.path.read_bytes()
        self.assertEqual("unknown", self.review(identity, Mock())["status"])
        self.assertEqual(before, self.ledger.path.read_bytes())
        with sqlite3.connect(self.ledger.path) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='position_management_revisions'").fetchone())
        self.state(identity)
        position = self.position(identity)
        self.confirm(identity)
        after = ManualPositionLedger(self.fixture.data_dir).read()[0]
        self.assertEqual({key: value for key, value in position.items() if key not in {"management_inputs", "management_history"}},
                         {key: value for key, value in after.items() if key not in {"management_inputs", "management_history"}})
        self.assertEqual("manual_confirmation", after["management_inputs"]["latest"]["leverage_source"])

    def test_two_partial_buys_map_to_one_stage_without_synthetic_ledger_fills(self):
        identity = self.save(self.payload(quantity="1", price="100"))
        self.save(self.payload(command="append", position_id=identity, quantity="3", price="104", executed_at="2026-09-06T10:15"))
        self.state(identity)
        self.confirm(identity)
        position = self.position(identity)
        projected = project_rule_fills(position, position["management_inputs"]["latest"])
        self.assertEqual((1, 4, 103, 2), (len(projected), projected[0]["units"], projected[0]["price"], len(projected[0]["sources"])))
        result = self.review(identity, self.loader())
        self.assertEqual("evaluated", result["status"])
        self.assertEqual(2, len(self.position(identity)["fills"]))

    def test_frozen_configuration_survives_default_changes_and_unknown_stays_unknown(self):
        identity = self.ready()
        frozen = self.position(identity)["management_inputs"]["latest"]["configuration"]
        with patch("mu_strategy.manual_positions.baseline_configuration", side_effect=AssertionError("do not replace frozen config")):
            self.confirm(identity, actual_leverage="", entry_anchor="", initial_stop_price="")
            reopened = ManualPositionLedger(self.fixture.data_dir).read()[0]
        self.assertEqual(frozen, reopened["management_inputs"]["latest"]["configuration"])
        self.assertIsNone(reopened["management_inputs"]["latest"]["leverage_source"])
        loader = Mock()
        self.assertEqual("unknown", self.review(identity, loader)["status"])
        loader.open_context.assert_not_called()

    def test_stale_fill_state_and_management_versions_are_rejected_atomically(self):
        for kind in ("fill", "state", "management"):
            with self.subTest(kind=kind):
                identity = self.ready()
                old = self.management_payload(identity)
                if kind == "fill":
                    self.save(self.payload(command="append", position_id=identity))
                elif kind == "state":
                    self.state(identity, stop="96")
                else:
                    self.confirm(identity, actual_leverage="3")
                before = self.position(identity)
                with self.assertRaisesRegex(ValueError, "已变化"):
                    self.ledger.save_management(old, now_ms=fixtures.NOW)
                self.assertEqual(before, self.position(identity))
                if kind != "management":
                    self.assertEqual("needs_review", self.review(identity, Mock())["status"])

    def test_retries_after_new_fill_are_idempotent_and_other_positions_do_not_invalidate(self):
        identity = self.ready()
        payload = self.confirm(identity)
        self.save(self.payload())
        self.assertEqual("confirmed", self.position(identity)["management_inputs"]["status"])
        self.save(self.payload(command="append", position_id=identity))
        before = self.position(identity)
        self.assertEqual(identity, self.ledger.save_management(payload, now_ms=fixtures.NOW))
        self.assertEqual(before, self.position(identity))
        with self.assertRaisesRegex(ValueError, "同次提交"):
            self.ledger.save_management({**payload, "actual_leverage": "4"}, now_ms=fixtures.NOW)

    def test_invalid_values_and_mappings_do_not_create_management_table(self):
        identity = self.save(self.payload())
        self.state(identity)
        fill = self.position(identity)["fills"][0]["fill_id"]
        before = self.ledger.path.read_bytes()
        for changes in ({"actual_leverage": "NaN"}, {"actual_leverage": "0"}, {"entry_anchor": []},
                        {"initial_stop_price": "-1"}, {"configuration_sha256": "a" * 64}, {"confirmed": ""},
                        {f"fill_stage_{fill}": "2"}, {f"fill_stage_{fill}": "1.5"}, {"fill_stage_" + "f" * 32: "1"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.confirm(identity, **changes)
            self.assertEqual(before, self.ledger.path.read_bytes())
        self.confirm(identity, **{f"fill_stage_{fill}": ""})
        self.assertEqual("unknown", self.review(identity, Mock())["status"])

    def test_partial_sell_is_unsupported_and_closed_position_cannot_be_reviewed(self):
        identity = self.ready()
        self.save(self.payload(command="append", position_id=identity, action="sell", quantity="1", executed_at="2026-09-06T11:00"))
        self.state(identity)
        self.confirm(identity)
        self.assertEqual("unsupported", self.review(identity, Mock())["status"])
        self.save(self.payload(command="append", position_id=identity, action="sell", quantity="1", executed_at="2026-09-06T11:30"))
        self.assertEqual("not_open", self.review(identity, Mock())["status"])
        with self.assertRaises(ValueError):
            self.confirm(identity)

    def test_earliest_exit_survives_recovery_and_queries_do_not_change_state(self):
        identity = self.ready()
        loader = self.loader(lows={fixtures.NOW - BAR_MS: 70, fixtures.NOW + BAR_MS: 94})
        before = self.ledger.path.read_bytes()
        result = self.review(identity, loader)
        evaluation = result["evaluation"]
        self.assertEqual("exit_review", evaluation["outcome"])
        self.assertEqual(fixtures.NOW + BAR_MS, evaluation["earliest_exit"]["candle_open_time_ms"])
        self.assertEqual(100, evaluation["latest_close"])
        self.assertIsNone(evaluation["addition"])
        self.assertEqual(4, evaluation["candle_count"])
        self.assertEqual(before, self.ledger.path.read_bytes())
        loader.open_context.assert_called_once_with(now_ms=fixtures.NOW + 4 * BAR_MS)
        self.assertIs(loader.execute.call_args.kwargs["context"], loader.open_context.return_value)
        again = self.review(identity, self.loader(lows={fixtures.NOW - BAR_MS: 70, fixtures.NOW + BAR_MS: 94}))
        self.assertEqual(result["provenance"]["review_identity"], again["provenance"]["review_identity"])

    def test_suggested_stop_is_not_carried_as_actual_into_next_candle(self):
        identity = self.save(self.payload())
        second = self.payload(command="append", position_id=identity, price="104", executed_at="2026-09-06T10:15")
        self.save(second)
        self.state(identity, stage="2")
        self.confirm(identity, **{f'fill_stage_{second["request_id"]}': "2"})
        result = self.review(identity, self.loader(lows={fixtures.NOW + BAR_MS: 97}))
        self.assertEqual("stop_review", result["evaluation"]["outcome"])
        self.assertIsNone(result["evaluation"]["earliest_exit"])
        self.assertEqual((95, 100), (result["evaluation"]["confirmed_stop"], result["evaluation"]["suggested_stop"]))
        self.assertEqual("95", self.position(identity)["current_state"]["stop_price"])

    def test_actual_leverage_overrides_only_risk_input_and_exit_has_priority(self):
        identity = self.ready(stop="40")
        loader = self.loader(lows={fixtures.NOW: 75})
        low_risk = self.review(identity, loader)
        self.assertIsNone(low_risk["evaluation"]["earliest_exit"])
        self.confirm(identity, actual_leverage="5")
        high_risk = self.review(identity, loader)
        self.assertEqual("non_session_liquidation_risk", high_risk["evaluation"]["earliest_exit"]["exit_reason"])
        self.assertEqual(low_risk["provenance"]["configuration_sha256"], high_risk["provenance"]["configuration_sha256"])
        self.assertNotEqual(low_risk["provenance"]["effective_configuration_sha256"], high_risk["provenance"]["effective_configuration_sha256"])

    def test_latest_shared_add_candidate_does_not_write_a_fill(self):
        at = int(datetime(2026, 9, 7, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
        identity = self.ready(at=at)
        loader = self.loader(end=at + 4 * BAR_MS, trend=True)
        before = self.position(identity)
        result = self.review(identity, loader, at=at + 4 * BAR_MS)
        self.assertEqual("add_candidate", result["evaluation"]["outcome"])
        self.assertEqual(2, result["evaluation"]["addition"]["stage"])
        self.assertEqual(before, self.position(identity))

    def test_waits_for_first_whole_candle_and_never_backcasts_new_stop(self):
        at = fixtures.NOW + 60_000
        identity = self.ready(at=at)
        result = self.review(identity, self.loader(end=fixtures.NOW + BAR_MS), at=fixtures.NOW + BAR_MS)
        self.assertEqual("waiting", result["status"])
        self.assertEqual(fixtures.NOW + BAR_MS, result["first_eligible_open_ms"])
        later = self.review(identity, self.loader(lows={fixtures.NOW: 10}))
        self.assertIsNone(later["evaluation"]["earliest_exit"])
        self.assertEqual(3, later["evaluation"]["candle_count"])

    def test_bad_trust_gap_hash_generation_and_incomplete_context_are_blocked(self):
        identity = self.ready()
        for kind in ("trust", "gap", "hash", "generation", "start", "warmup", "future"):
            with self.subTest(kind=kind):
                loader = self.loader()
                bundle = loader.execute.return_value
                if kind == "trust":
                    bundle = replace(bundle, trust_decision=TrustDecision(False, HealthReason.NOT_PUBLISHED))
                elif kind == "hash":
                    bundle.health_by_interval["5m"] = replace(bundle.health_by_interval["5m"], content_sha256=None)
                elif kind == "generation":
                    bundle = replace(bundle, run_id="different")
                elif kind == "gap":
                    del bundle.candles_by_interval["15m"][-3]
                elif kind == "start":
                    bundle.candles_by_interval["15m"] = bundle.candles_by_interval["15m"][-2:]
                elif kind == "warmup":
                    bundle.candles_by_interval["1h"] = bundle.candles_by_interval["1h"][-10:]
                else:
                    rows = bundle.candles_by_interval["15m"]
                    rows.append(replace(rows[-1], open_time_ms=fixtures.NOW + 4 * BAR_MS))
                loader.execute.return_value = bundle
                self.assertEqual("data_blocked", self.review(identity, loader)["status"])

    def test_real_trusted_store_missing_valid_stale_and_corrupt_generation(self):
        identity = self.ready()
        end = fixtures.NOW + 4 * BAR_MS
        self.assertEqual("data_blocked", self.review(identity)["status"])
        start = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp() * 1000)
        rows = [Candle(at, 100, 101, 94 if at == fixtures.NOW else 99, 100, 1) for at in range(start, end, 300_000)]
        provider = storage_fixtures.MutableBundleProvider({"5m": rows, "15m": aggregate_candles(rows, interval="15m"), "1h": aggregate_candles(rows, interval="1h")})
        store = TrustedDataStore(data_dir=self.fixture.data_dir)
        RefreshTrustedMarketData(store, provider).execute(RefreshTrustedMarketDataRequest(
            requested_intervals=("15m", "1h"), symbols=(fixtures.SYMBOL,), days=7, now_ms=end, run_id="d" * 32))
        result = self.review(identity)
        self.assertEqual("evaluated", result["status"], result)
        self.assertEqual("exit_review", result["evaluation"]["outcome"])
        self.assertEqual("d" * 32, result["provenance"]["generation_id"])
        self.assertEqual("data_blocked", self.review(identity, at=end + 24 * 4 * BAR_MS)["status"])
        store.current_path.write_text("{}")
        self.assertEqual("data_blocked", self.review(identity)["status"])

    def test_http_origin_versions_error_input_retention_and_static_read_only(self):
        identity = self.ready()
        self.server = make_review_server(self.fixture.data_dir, port=0, clock=self.fixture.clock)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)
        request = lambda *args, **kwargs: manual.ManualPositionServerTests.request(self, *args, **kwargs)
        self.assertEqual(400, request("/position-management")[0])
        self.assertEqual(404, request("/position-management?position_id=" + "f" * 32)[0])
        payload = self.management_payload(identity, entry_anchor="101.5", note="<script>保留输入</script>")
        self.assertEqual(403, request("/position-management", payload, origin="https://other.example")[0])
        self.assertEqual(303, request("/position-management", payload)[0])
        stale = self.management_payload(identity, entry_anchor="102.5", note="<script>旧表单</script>")
        self.state(identity, stop="96")
        status, _, page = request("/position-management", stale)
        self.assertEqual(400, status)
        self.assertIn('value="102.5"', page)
        self.assertIn("&lt;script&gt;旧表单&lt;/script&gt;", page)
        self.assertNotIn('value="yes" checked', page)
        self.assertEqual("101.5", self.position(identity)["management_inputs"]["latest"]["entry_anchor"])
        self.assertEqual(200, request("/position-management?position_id=" + identity)[0])
        static = render_position_management_editor(self.ledger.view(), stylesheet="", position_id=identity,
                                                   review=self.review(identity, Mock()), editable=False)
        self.assertNotIn('<form ', static)
        self.assertNotIn('href="/position-', static)
        self.assertNotIn('href="/positions', static)
        self.assertNotIn('href="/position-management', render_signal_review(self.fixture.read()))
