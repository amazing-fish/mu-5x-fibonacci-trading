import hashlib
import http.client
import threading
import unittest

from mu_strategy.signal_review_server import make_review_server
from mu_strategy.viz.signal_review import group_scan_records
from tests import test_signal_review as fixtures

NOW, cycle = fixtures.NOW, fixtures.cycle


class ReviewPresentationTests(unittest.TestCase):
    def rows(self, kinds):
        return [cycle(number, kind, at=NOW + number * 300_000).observations[0].to_dict()
                for number, kind in enumerate(kinds)]

    def test_repeated_waits_are_one_group_with_every_original_observation(self):
        records = self.rows(["wait"] * 30)
        groups = group_scan_records(records)
        self.assertEqual(1, len(groups))
        self.assertEqual(records, groups[0])

    def test_changes_actionable_records_gaps_dates_and_clock_reversals_split_groups(self):
        self.assertEqual([2, 1, 1, 2, 1, 1],
                         [len(group) for group in group_scan_records(self.rows(["wait", "wait", "ready", "ready", "wait", "wait", "blocked", "failed"]))])
        original = self.rows(["wait", "wait"])
        for change in ({"decision_code": "regime_blocked"}, {"strategy_config_fingerprint": "b" * 64},
                       {"created_at_ms": NOW + 3600000}, {"created_at_ms": NOW - 1},
                       {"observed_at_ms": NOW - 1}, {"created_at_ms": NOW + 86400000}):
            with self.subTest(change=change):
                self.assertEqual(2, len(group_scan_records([original[0], {**original[1], **change}])))

    def test_interleaved_symbols_group_independently_without_hiding_a_symbol_transition(self):
        a = self.rows(["wait", "wait", "ready", "wait"])
        b = cycle(20, at=NOW + 1, symbol="BTC-USDT-SWAP").observations[0].to_dict()
        groups = group_scan_records([a[0], b, a[1], a[2], a[3]])
        self.assertEqual([1, 2, 1, 1], [len(group) for group in groups])

    def test_waits_five_minutes_apart_split_at_beijing_midnight(self):
        midnight = NOW + 12 * 3600000
        records = [cycle(number, at=at).observations[0].to_dict()
                   for number, at in enumerate((midnight - 300000, midnight))]
        self.assertEqual([1, 1], [len(group) for group in group_scan_records(records)])


class ReviewLiveServerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SignalReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.initialize([cycle(1)])
        self.server = make_review_server(self.fixture.data_dir, port=0, clock=self.fixture.clock)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def request(self, path="/", *, method="GET", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode("utf-8")
        finally:
            connection.close()

    def test_live_requests_observe_new_scans_without_recreating_a_static_file(self):
        status, headers, first = self.request()
        self.assertEqual(200, status)
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertIn('data-live="true"', first)
        self.assertNotIn("observation-2", first)
        self.fixture.log.append_cycle(cycle(2, at=NOW + 300000))
        status, _, second = self.request("/report")
        self.assertEqual(200, status)
        self.assertIn("observation-2", second)
        self.assertIn("2026-09-06 12:05:00", second)
        self.assertIn("展开 2 次扫描记录", second)

    def test_source_failure_is_a_new_unavailable_view_not_an_old_healthy_response(self):
        self.request()
        self.fixture.log.invalid_marker_path.write_text("interrupted", encoding="utf-8")
        status, _, page = self.request("/report")
        self.assertEqual(200, status)
        self.assertIn('data-sources-verified="false"', page)
        self.assertIn("扫描日志暂不可验证", page)

    def test_default_window_rolls_forward_at_beijing_midnight(self):
        self.assertIn('value="2026-09-06"', self.request()[2])
        midnight = NOW + 12 * 3600000
        self.fixture.clock.now_ms = lambda: midnight
        self.fixture.log.append_cycle(cycle(2, at=midnight))
        status, _, page = self.request("/report")
        self.assertEqual(200, status)
        self.assertIn('value="2026-09-07"', page)
        self.assertIn("2026-09-07 00:00:00", page)
        self.assertIn("observation-2", page)

    def test_readonly_routes_and_loopback_host_boundary(self):
        def files():
            return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.fixture.root.rglob("*") if p.is_file()}
        before = files()
        self.assertEqual(200, self.request()[0])
        self.assertEqual(404, self.request("/observations.jsonl")[0])
        self.assertEqual(405, self.request("/", method="POST")[0])
        self.assertEqual(403, self.request(headers={"Host": "attacker.example"})[0])
        self.assertEqual(403, self.request(headers={"Origin": "https://attacker.example"})[0])
        self.assertEqual(before, files())


if __name__ == "__main__":
    unittest.main()
