import hashlib
import http.client
import json
import threading
import unittest

from mu_strategy.notifications.events import AlertKind
from mu_strategy.signal_feedback import SignalFeedbackStore
from mu_strategy.signal_review_server import make_review_server
from mu_strategy.viz.signal_review import render_signal_review
from tests import test_signal_review as fixtures


class SignalFeedbackTests(unittest.TestCase):
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
        self.store = SignalFeedbackStore(self.fixture.data_dir)
        self.server = make_review_server(self.fixture.data_dir, port=0, clock=self.fixture.clock)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def post(self, payload, *, origin=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.request("POST", "/feedback", json.dumps(payload), {
            "Content-Type": "application/json",
            "Origin": origin or f"http://127.0.0.1:{self.server.server_port}",
        })
        response = connection.getresponse()
        return response.status, response.read().decode()

    def test_read_creates_no_files_and_save_survives_reopening_without_changing_evidence(self):
        def evidence():
            return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.fixture.health.root.rglob("*") if p.is_file()}
        before = evidence()
        self.assertEqual({}, self.store.read())
        self.assertFalse(self.store.path.parent.exists())
        self.assertEqual(200, self.post({"event_id": self.entry.event_id, "status": "traded", "note": "手动成交，稍后补充"})[0])
        reopened = SignalFeedbackStore(self.fixture.data_dir)
        self.assertEqual("traded", reopened.read()[self.entry.event_id]["status"])
        self.assertEqual("手动成交，稍后补充", reopened.read()[self.entry.event_id]["note"])
        self.assertEqual(200, self.post({"event_id": self.entry.event_id, "status": "unreviewed", "note": ""})[0])
        self.assertEqual(1, len(reopened.read()))
        self.assertEqual("", reopened.read()[self.entry.event_id]["note"])
        self.assertEqual(before, evidence())

    def test_bad_requests_and_other_event_kinds_cannot_write_feedback(self):
        valid = {"event_id": self.entry.event_id, "status": "seen", "note": ""}
        self.assertEqual(403, self.post(valid, origin="https://attacker.example")[0])
        for payload in ([valid], {**valid, "event_id": self.fault.event_id},
                        {**valid, "event_id": "f" * 64}, {**valid, "status": []},
                        {**valid, "note": "x" * 2001}):
            with self.subTest(payload=str(payload)[:80]):
                self.assertEqual(400, self.post(payload)[0])
        self.assertFalse(self.store.path.exists())

    def test_static_export_includes_feedback_as_text_and_live_page_can_edit(self):
        note = '</textarea><script>alert("x")</script>\n第二行'
        self.store.save(self.entry.event_id, "skipped", note, now_ms=fixtures.NOW)
        report = self.fixture.read()
        static = render_signal_review(report)
        live = render_signal_review(report, live=True)
        self.assertIn("人工处理：已跳过", static)
        self.assertIn("&lt;script&gt;", static)
        self.assertNotIn(note, static)
        self.assertNotIn('<form class="feedback-form"', static)
        self.assertEqual(1, live.count('<form class="feedback-form"'))
        self.assertIn('value="skipped" selected', live)

    def test_unreadable_feedback_does_not_claim_unreviewed_or_hide_the_signal(self):
        self.store.path.parent.mkdir()
        self.store.path.write_text("not a database")
        page = render_signal_review(self.fixture.read(), live=True)
        self.assertIn("人工反馈暂不可用", page)
        self.assertIn("入场复核", page)
        self.assertNotIn('<form class="feedback-form"', page)
        self.assertNotIn('data-feedback="unreviewed"', page)
        self.assertEqual(503, self.post({"event_id": self.entry.event_id, "status": "seen", "note": ""})[0])


if __name__ == "__main__":
    unittest.main()
