import hashlib
import sqlite3
import threading
from uuid import uuid4

from mu_strategy.manual_positions import ManualPositionLedger
from mu_strategy.signal_review_server import make_review_server
from mu_strategy.viz.position_ledger import render_position_state_editor
from mu_strategy.viz.signal_review import render_signal_review
from tests import test_signal_review as fixtures
from tests import test_manual_positions as manual_fixtures


class PositionStateTests(manual_fixtures.ManualPositionTestCase):
    def position(self, identity):
        return next(item for item in self.ledger.read() if item["position_id"] == identity)

    def state_payload(self, identity, **changes):
        position = self.position(identity)
        return {"request_id": uuid4().hex, "position_id": identity, "confirmed": "yes",
                "expected_fill_sequence": str(position["fill_sequence"]),
                "expected_state_revision": str(position["current_state"]["revision"]),
                "stage": "2", "stop_price": "95", "note": "本次人工核对", **changes}

    def confirm(self, payload):
        return self.ledger.save_state(payload, now_ms=fixtures.NOW)

    def test_old_ledger_read_is_unchanged_and_fill_notes_are_not_current_state(self):
        identity = self.save(self.payload(stage="3", stop_price="91"))
        before = self.ledger.path.read_bytes()
        position = self.position(identity)
        self.assertEqual("unconfirmed", position["current_state"]["status"])
        self.assertIsNone(position["current_state"]["stage"])
        self.assertIsNone(position["current_state"]["stop_price"])
        self.assertEqual([], position["state_history"])
        page = render_position_state_editor(self.ledger.view(), stylesheet="", position_id=identity)
        self.assertIn('name="stage" inputmode="numeric" maxlength="2" value=""', page)
        self.assertIn('name="stop_price" inputmode="decimal" maxlength="31" value=""', page)
        self.assertEqual(before, self.ledger.path.read_bytes())
        with sqlite3.connect(self.ledger.path) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='position_state_revisions'").fetchone())

    def test_state_revisions_survive_reopen_without_changing_fills_or_source(self):
        identity = self.save(self.payload())
        before = self.position(identity)
        evidence = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.fixture.health.root.rglob("*") if p.is_file()}
        self.confirm(self.state_payload(identity, stop_price="95.00"))
        self.ledger = ManualPositionLedger(self.fixture.data_dir)
        after = self.position(identity)
        self.assertEqual({k: v for k, v in before.items() if k not in {"current_state", "state_history"}},
                         {k: v for k, v in after.items() if k not in {"current_state", "state_history"}})
        self.assertEqual("confirmed", after["current_state"]["status"])
        self.assertEqual(2, after["current_state"]["stage"])
        self.assertEqual("95", after["current_state"]["stop_price"])
        self.assertEqual("unknown", after["management_status"])
        self.confirm(self.state_payload(identity, stage="", stop_price="", note="目前无法确认"))
        after = self.position(identity)
        self.assertEqual(2, after["current_state"]["revision"])
        self.assertIsNone(after["current_state"]["stage"])
        self.assertIsNone(after["current_state"]["stop_price"])
        self.assertEqual("95", after["state_history"][0]["stop_price"])
        self.assertEqual("目前无法确认", after["state_history"][1]["note"])
        self.assertEqual(evidence, {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in self.fixture.health.root.rglob("*") if p.is_file()})

    def test_fill_changes_invalidate_confirmation_until_explicitly_reconfirmed(self):
        first = self.payload()
        identity = self.save(first)
        self.confirm(self.state_payload(identity))
        changes = [self.payload(command="append", position_id=identity, action="sell", quantity="1", executed_at="2026-09-06T11:00"),
                   self.payload(command="append", position_id=identity, quantity="2", executed_at="2026-09-06T09:00"),
                   self.payload(command="revise", position_id=identity, fill_id=first["request_id"], expected_revision="1", note="仅更正手记"),
                   self.payload(command="revise", position_id=identity, fill_id=first["request_id"], expected_revision="2", voided="yes", note="作废误录")]
        for change in changes:
            with self.subTest(command=change["command"], request=change["request_id"]):
                self.save(change)
                state = self.position(identity)["current_state"]
                self.assertEqual("needs_review", state["status"])
                self.assertIsNone(state["stage"])
                self.assertIsNone(state["stop_price"])
                self.confirm(self.state_payload(identity, stage="1", stop_price="90"))
                self.assertEqual("confirmed", self.position(identity)["current_state"]["status"])

    def test_other_positions_and_fill_retries_do_not_invalidate_state(self):
        first = self.payload()
        identity = self.save(first)
        self.confirm(self.state_payload(identity))
        self.save(first)
        self.save(self.payload())
        self.assertEqual("confirmed", self.position(identity)["current_state"]["status"])
        self.assertEqual(1, len(self.position(identity)["state_history"]))

    def test_stale_fill_and_state_versions_reject_atomically(self):
        identity = self.save(self.payload())
        old = self.state_payload(identity)
        self.save(self.payload(command="append", position_id=identity))
        before = self.position(identity)
        with self.assertRaisesRegex(ValueError, "已变化"):
            self.confirm(old)
        self.assertEqual(before, self.position(identity))
        first, competing = self.state_payload(identity), self.state_payload(identity)
        self.confirm(first)
        with self.assertRaisesRegex(ValueError, "已变化"):
            self.confirm(competing)
        self.assertEqual(1, len(self.position(identity)["state_history"]))

    def test_state_retries_are_idempotent_even_after_later_fills(self):
        identity = self.save(self.payload())
        payload = self.state_payload(identity)
        self.assertEqual(identity, self.confirm(payload))
        self.assertEqual(identity, self.confirm(payload))
        self.save(self.payload(command="append", position_id=identity))
        self.assertEqual(identity, self.confirm(payload))
        self.assertEqual("needs_review", self.position(identity)["current_state"]["status"])
        with self.assertRaisesRegex(ValueError, "同次提交"):
            self.confirm({**payload, "stop_price": "94"})
        self.assertEqual(1, len(self.position(identity)["state_history"]))

    def test_closed_empty_and_reopened_positions_do_not_reuse_current_state(self):
        identity = self.save(self.payload())
        self.confirm(self.state_payload(identity))
        sale = self.payload(command="append", position_id=identity, action="sell", executed_at="2026-09-06T11:00")
        self.save(sale)
        self.assertEqual("not_open", self.position(identity)["current_state"]["status"])
        with self.assertRaisesRegex(ValueError, "仅可确认"):
            self.confirm(self.state_payload(identity))
        self.save(self.payload(command="revise", position_id=identity, fill_id=sale["request_id"], expected_revision="1",
                               voided="yes", action="sell", note="误录卖出", executed_at="2026-09-06T11:00"))
        self.assertEqual("needs_review", self.position(identity)["current_state"]["status"])
        first = self.payload()
        empty = self.save(first)
        self.save(self.payload(command="revise", position_id=empty, fill_id=first["request_id"], expected_revision="1", voided="yes", note="误录"))
        with self.assertRaisesRegex(ValueError, "仅可确认"):
            self.confirm(self.state_payload(empty))

    def test_invalid_confirmations_do_not_create_state_table_or_ledger(self):
        with self.assertRaises(ValueError):
            self.confirm({"position_id": uuid4().hex, "request_id": uuid4().hex})
        self.assertFalse(self.ledger.path.exists())
        identity = self.save(self.payload())
        before = self.ledger.path.read_bytes()
        for change in ({"stage": "0"}, {"stage": "100"}, {"stage": "1.5"}, {"stage": True},
                       {"stop_price": "NaN"}, {"stop_price": "Infinity"}, {"stop_price": "0"},
                       {"stop_price": "-2"}, {"stop_price": []}, {"note": "x" * 2001},
                       {"confirmed": ""}, {"expected_fill_sequence": ""}, {"expected_state_revision": ""}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.confirm(self.state_payload(identity, **change))
            self.assertEqual(before, self.ledger.path.read_bytes())

    def test_static_state_cards_are_read_only_and_corruption_is_visible(self):
        identity = self.save(self.payload())
        self.confirm(self.state_payload(identity, note="<script>state note</script>"))
        self.save(self.payload(command="append", position_id=identity))
        self.confirm(self.state_payload(identity, stop_price="97"))
        page = render_signal_review(self.fixture.read())
        self.assertIn("已人工确认", page)
        self.assertIn("&lt;script&gt;state note&lt;/script&gt;", page)
        self.assertNotIn('href="/position-state', page)
        self.assertNotIn('action="/position-state', page)
        self.assertIn("对应成交记录版本", page)
        for version in (1, 2):
            anchor = f"position-fill-version-{identity}-{version}"
            self.assertIn(f'href="#{anchor}">v{version}</a>', page)
            self.assertIn(f'<tr id="{anchor}">', page)
        with sqlite3.connect(self.ledger.path) as db:
            db.execute("UPDATE position_state_revisions SET payload='{}'")
        self.assertFalse(self.ledger.view()["available"])
        self.assertIn("成交台账暂不可用", render_signal_review(self.fixture.read()))

    def test_http_state_form_success_origin_and_stale_input_preservation(self):
        self.server = make_review_server(self.fixture.data_dir, port=0, clock=self.fixture.clock)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)
        request = lambda *args, **kwargs: manual_fixtures.ManualPositionServerTests.request(self, *args, **kwargs)
        identity = self.save(self.payload())
        self.assertEqual(400, request("/position-state")[0])
        self.assertEqual(404, request("/position-state?position_id=" + "f" * 32)[0])
        self.assertEqual(400, request("/position-state?position_id=" + identity + "&fill_id=x")[0])
        self.assertEqual(200, request("/position-state?position_id=" + identity)[0])
        payload = self.state_payload(identity, note="<script>keep input</script>")
        self.assertEqual(403, request("/position-state", payload, origin="https://attacker.example")[0])
        self.assertEqual(403, request("/position-state?position_id=" + identity, origin="https://attacker.example")[0])
        status, headers, _ = request("/position-state", payload)
        self.assertEqual(303, status)
        self.assertIn("saved=state", headers["Location"])
        self.assertIn("已保存当前持仓状态", request(headers["Location"].split("#")[0])[2])
        self.assertEqual(1, len(self.position(identity)["history"]))
        stale = self.state_payload(identity, stage="3", stop_price="102", note="<script>keep input</script>")
        self.save(self.payload(command="append", position_id=identity, action="sell", quantity="1", executed_at="2026-09-06T11:00"))
        status, _, page = request("/position-state", stale)
        self.assertEqual(400, status)
        self.assertIn('name="stop_price" inputmode="decimal" maxlength="31" value="102"', page)
        self.assertIn("&lt;script&gt;keep input&lt;/script&gt;", page)
        self.assertIn(stale["request_id"], page)
        self.assertIn('name="expected_fill_sequence" value="1"', page)
        self.assertIn("重新打开最新持仓", page)
        self.assertNotIn('name="confirmed" value="yes" required checked', page)
        self.assertEqual("needs_review", self.position(identity)["current_state"]["status"])
        self.ledger.path.write_text("broken")
        status, _, page = request("/position-state", stale)
        self.assertEqual(503, status)
        self.assertIn("keep input", page)
        self.assertIn("成交台账暂不可用", page)
