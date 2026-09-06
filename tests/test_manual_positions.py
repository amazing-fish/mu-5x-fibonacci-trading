import hashlib
import http.client
import threading
import unittest
from urllib.parse import urlencode
from uuid import uuid4

from mu_strategy.manual_positions import ManualPositionLedger
from mu_strategy.notifications.events import AlertKind
from mu_strategy.signal_feedback import SignalFeedbackStore
from mu_strategy.signal_review_server import make_review_server
from mu_strategy.viz.position_ledger import render_position_editor
from mu_strategy.viz.signal_review import render_signal_review
from tests import test_signal_review as fixtures


class ManualPositionTestCase(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SignalReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        ready = fixtures.cycle(1, "ready")
        self.fixture.initialize([ready])
        self.entry = self.fixture.event(1, kind=AlertKind.ENTRY_REVIEW, observation=ready.observations[0])
        self.fault = self.fixture.event(2)
        with self.fixture.store.connection() as db, self.fixture.store.transaction(db):
            for event in (self.entry, self.fault):
                self.fixture.store.enqueue(db, event, now_ms=fixtures.NOW)
        self.ledger = ManualPositionLedger(self.fixture.data_dir)

    def payload(self, **changes):
        return {"command": "create", "request_id": uuid4().hex, "position_id": uuid4().hex,
                "symbol": fixtures.SYMBOL, "unit": "contracts", "event_id": self.entry.event_id,
                "action": "buy", "quantity": "2", "price": "100", "executed_at": "2026-09-06T10:00:00",
                "confirmed": "yes", **changes}

    def save(self, payload):
        return self.ledger.save(payload, now_ms=fixtures.NOW)


class ManualPositionTests(ManualPositionTestCase):
    def test_read_only_missing_ledger_and_traded_feedback_do_not_create_fills(self):
        SignalFeedbackStore(self.fixture.data_dir).save(self.entry.event_id, "traded", "已交易", now_ms=fixtures.NOW)
        view = self.fixture.read()["positions"]
        self.assertTrue(view["available"])
        self.assertEqual([], view["positions"])
        self.assertFalse(self.ledger.path.exists())
        self.assertIn("尚无成交记录。这不代表账户空仓", render_signal_review(self.fixture.read()))

    def test_manual_facts_survive_reopen_and_leave_source_evidence_unchanged(self):
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.fixture.health.root.rglob("*") if p.is_file()}
        payload = self.payload(price="101.23", stage="2", stop_price="95")
        self.save(payload)
        position = ManualPositionLedger(self.fixture.data_dir).read()[0]
        self.assertEqual("101.23", position["average_entry_price"])
        self.assertEqual("2", position["recorded_quantity"])
        self.assertEqual(2, position["recorded_stage"])
        self.assertEqual("95", position["recorded_stop_price"])
        self.assertIsNone(position["transition_state"])
        self.assertEqual("unknown", position["management_status"])
        self.assertEqual(self.entry.event_id, position["signal_source"]["event_id"])
        self.assertEqual("manual_confirmation", position["source"])
        self.assertLess(position["fills"][0]["time_ms"], position["fills"][0]["recorded_at_ms"])
        self.assertEqual(before, {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in self.fixture.health.root.rglob("*") if p.is_file()})

    def test_retries_are_idempotent_but_two_identical_fills_are_distinct(self):
        first = self.payload()
        identity = self.save(first)
        self.assertEqual(identity, self.save(first))
        with self.assertRaises(ValueError):
            self.save({**first, "price": "102"})
        second = self.payload(command="append", position_id=identity)
        self.save(second)
        position = self.ledger.read()[0]
        self.assertEqual("4", position["recorded_quantity"])
        self.assertEqual(2, len(position["fills"]))
        self.assertIsNone(position["recorded_stage"])
        # A retry remains acknowledged even if the original notification source disappears.
        self.fixture.store.path.unlink()
        self.assertEqual(identity, self.save(first))

    def test_partial_sales_weighted_cost_corrections_and_backdated_fills(self):
        first = self.payload()
        identity = self.save(first)
        later = self.payload(command="append", position_id=identity, quantity="2", price="120", executed_at="2026-09-06T11:00:00")
        self.save(later)
        sale = self.payload(command="append", position_id=identity, action="sell", quantity="1", price="200", executed_at="2026-09-06T11:30:00")
        self.save(sale)
        self.assertEqual("110", self.ledger.read()[0]["average_entry_price"])
        correction = self.payload(command="revise", position_id=identity, fill_id=later["request_id"], expected_revision="1",
                                  price="140", note="第二笔实际成交价更正", executed_at="2026-09-06T11:00:00")
        self.save(correction)
        position = self.ledger.read()[0]
        self.assertEqual("3", position["recorded_quantity"])
        self.assertEqual("120", position["average_entry_price"])
        self.assertEqual(4, len(position["history"]))
        self.assertEqual("120", position["history"][1]["price"])
        # A later-submitted earlier fill is ordered by actual execution time.
        self.save(self.payload(command="append", position_id=identity, quantity="1", price="60", executed_at="2026-09-06T10:30:00"))
        self.assertEqual("108", self.ledger.read()[0]["average_entry_price"])
        self.assertEqual("4", self.ledger.read()[0]["recorded_quantity"])

    def test_void_preserves_original_and_invalid_revision_is_atomic(self):
        first = self.payload()
        identity = self.save(first)
        sale = self.payload(command="append", position_id=identity, action="sell", quantity="1", executed_at="2026-09-06T11:00:00")
        self.save(sale)
        with self.assertRaises(ValueError):
            self.save(self.payload(command="revise", position_id=identity, fill_id=first["request_id"],
                                   expected_revision="1", voided="yes", note="作废会导致此前卖出超额"))
        self.assertEqual(2, len(self.ledger.read()[0]["history"]))
        correction = self.payload(command="revise", position_id=identity, fill_id=sale["request_id"], expected_revision="1",
                                  action="sell", quantity="1", voided="yes", note="误录卖出", executed_at="2026-09-06T11:00:00")
        self.save(correction)
        self.assertEqual("2", self.ledger.read()[0]["recorded_quantity"])
        self.assertEqual(3, len(self.ledger.read()[0]["history"]))
        page = render_position_editor(self.ledger.view(), stylesheet="", position_id=identity, fill_id=sale["request_id"])
        self.assertIn('name="voided" value="yes" checked', page)
        with self.assertRaises(ValueError):
            self.save({**correction, "request_id": uuid4().hex})

    def test_closed_positions_and_same_symbol_identities_are_separate(self):
        identity = self.save(self.payload(quantity="0.3", unit="base"))
        with self.assertRaises(ValueError):
            self.save(self.payload(command="append", position_id=identity, action="sell", quantity="0.1"))
        self.save(self.payload(command="append", position_id=identity, unit="base", action="sell", quantity="0.1", executed_at="2026-09-06T11:00:00"))
        self.save(self.payload(command="append", position_id=identity, unit="base", action="sell", quantity="0.2", executed_at="2026-09-06T11:01:00"))
        self.assertEqual("closed", self.ledger.read()[0]["status"])
        with self.assertRaises(ValueError):
            self.save(self.payload(command="append", position_id=identity, executed_at="2026-09-06T11:02:00"))
        self.save(self.payload())
        self.assertEqual(2, len(self.ledger.read()))
        other = self.ledger.read()[0]
        with self.assertRaises(ValueError):
            self.save(self.payload(command="revise", position_id=identity, fill_id=other["fills"][0]["fill_id"], expected_revision="1", note="错误归属"))

    def test_invalid_first_records_create_no_ledger(self):
        invalid = ({"price": "NaN"}, {"quantity": "0"}, {"quantity": "-1"}, {"price": "1e999"},
                   {"price": []}, {"action": "sell"}, {"unit": "USDT"}, {"confirmed": ""},
                   {"executed_at": "2026-09-06"}, {"executed_at": "2026-09-07T10:00"},
                   {"executed_at": "2026-09-06T10:00+08:00"}, {"symbol": "<script>"},
                   {"symbol": "BTC-USDT-SWAP"}, {"event_id": self.fault.event_id}, {"event_id": "f" * 64})
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.save(self.payload(**changes))
            self.assertFalse(self.ledger.path.exists())

    def test_static_cards_escape_notes_and_corruption_does_not_claim_empty_positions(self):
        self.save(self.payload(note="<script>bad()</script>"))
        page = render_signal_review(self.fixture.read())
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>bad()", page)
        self.assertNotIn('href="/positions', page)
        self.ledger.path.write_text("not sqlite")
        page = render_signal_review(self.fixture.read(), live=True)
        self.assertIn("成交台账暂不可用", page)
        self.assertIn("入场复核", page)
        self.assertNotIn("尚无成交记录", page)


class ManualPositionServerTests(ManualPositionTestCase):
    def setUp(self):
        super().setUp()
        self.server = make_review_server(self.fixture.data_dir, port=0, clock=self.fixture.clock)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def request(self, path, payload=None, *, origin=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            headers = {"Origin": origin or f"http://127.0.0.1:{self.server.server_port}"}
            if payload is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            connection.request("POST" if payload is not None else "GET", path, urlencode(payload) if payload is not None else None, headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    def test_live_create_redirect_form_source_and_saved_position(self):
        status, _, page = self.request("/positions?event_id=" + self.entry.event_id)
        self.assertEqual(200, status)
        self.assertIn('value="MU-USDT-SWAP"', page)
        self.assertIn('name="price" type="text" value=""', page)
        self.assertFalse(self.ledger.path.exists())
        status, headers, _ = self.request("/positions", self.payload())
        self.assertEqual(303, status)
        self.assertIn("saved=1", headers["Location"])
        self.assertIn("已记录持仓", self.request("/")[2])
        self.assertEqual(400, self.request("/positions?event_id=" + self.fault.event_id)[0])
        self.assertEqual(404, self.request("/positions?position_id=" + "f" * 32)[0])

    def test_local_origin_and_save_failure_keep_entered_values(self):
        payload = self.payload(quantity="bad", note="<script>keep my input</script>")
        self.assertEqual(403, self.request("/positions", payload, origin="https://attacker.example")[0])
        status, _, page = self.request("/positions", payload)
        self.assertEqual(400, status)
        self.assertIn('name="quantity" type="text" value="bad"', page)
        self.assertIn("&lt;script&gt;keep my input&lt;/script&gt;", page)
        self.assertIn(payload["request_id"], page)
        self.assertFalse(self.ledger.path.exists())
        self.ledger.path.parent.mkdir(exist_ok=True)
        self.ledger.path.write_text("broken")
        status, _, page = self.request("/positions", self.payload(note="保存失败仍保留"))
        self.assertEqual(503, status)
        self.assertIn("保存失败仍保留", page)
        self.assertIn("成交台账暂不可用", page)


if __name__ == "__main__":
    unittest.main()
