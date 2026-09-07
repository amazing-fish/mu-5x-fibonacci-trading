import copy
import unittest
from dataclasses import replace
from unittest.mock import Mock

from mu_strategy.core.trading_calendar import evaluate_trading_window
from mu_strategy.models import EntryDecisionCode
from mu_strategy.observations import ObservationCorruptionError
from mu_strategy.scan_cycle import ScanCycle
from mu_strategy.signal_review import current_conclusions, read_scan_evidence
from mu_strategy.strategies.registry import baseline_strategy_group
from mu_strategy.viz.signal_review import render_signal_review
from tests import test_signal_review as fixtures
from tests import test_signal_review_live as live
from tests.factories.scan_cycle import scan_result, trusted_scan_bundle
from tests.test_entry_scanner import _utc_ms
from tests.test_calendar_compatibility import legacy_cycle


def evaluated_cycle(number=1, *, at=None, symbol=fixtures.SYMBOL):
    at = at or _utc_ms(2026, 9, 8, 14, 20)
    opened = (at // 900_000 - 1) * 900_000
    config = baseline_strategy_group(symbol).config
    calendar = evaluate_trading_window(opened, config)
    code = EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY if calendar.allowed else EntryDecisionCode(calendar.reason)
    result = replace(scan_result(code, symbol=symbol), evaluated_candle_open_ms=opened,
                     evaluated_candle_close_ms=opened + 900_000, calendar_id=calendar.calendar_id,
                     calendar_sha256=calendar.calendar_sha256, calendar_session=calendar.session,
                     trading_windows_et=config.trading_windows_et)
    bundle = trusted_scan_bundle(symbol=symbol)
    bundle = replace(bundle, observed_at_ms=at, load_context=replace(bundle.load_context, observed_at_ms=at))
    cycle = ScanCycle(clock=Mock(now_ms=lambda: at), id_factory=iter((f'cycle-{number}', f'observation-{number}')).__next__)
    cycle.scan_symbol(symbol=symbol, source='watchlist', bundle=bundle, requested_intervals=('15m', '1h'),
                      strategy_name='baseline', strategy_config=config, scanner=Mock(return_value=result), data_failure=None)
    return cycle.observations()


def sources(row):
    service = {'state': 'ok', 'symbols': [row['symbol']], 'view': {
        'runtime': 'running', 'healthy': True, 'run_id': 'service-current',
        'last_cycle': {'service_run_id': 'service-current', 'scan': {'cycle': {'observations': [copy.deepcopy(row)]}}}}}
    return service, {'state': 'ok', 'latest_all': [row]}


class CurrentConclusionTests(unittest.TestCase):
    def test_current_requires_source_cycle_configuration_and_time_alignment(self):
        row = evaluated_cycle().observations[0].to_dict()
        at = row['observed_at_ms']
        service, observations = sources(row)
        self.assertEqual('current', current_conclusions(service, observations, now_ms=at)[0]['status'])
        cases = []
        stopped = copy.deepcopy(service); stopped['view']['runtime'] = 'stopped'; cases.append((stopped, observations))
        prior = copy.deepcopy(service); prior['view']['last_cycle']['service_run_id'] = 'previous'; cases.append((prior, observations))
        cases.extend((service, {**observations, 'state': state}) for state in ('incomplete', 'unavailable'))
        mismatch = copy.deepcopy(service); mismatch['view']['last_cycle']['scan']['cycle']['observations'][0]['observation_id'] = 'different'; cases.append((mismatch, observations))
        for current_service, current_observations in cases:
            with self.subTest(service=current_service, state=current_observations['state']):
                self.assertEqual('unavailable', current_conclusions(current_service, current_observations, now_ms=at)[0]['status'])
        self.assertEqual('unavailable', current_conclusions(service, observations, now_ms=at + 600_001)[0]['status'])
        self.assertEqual('unavailable', current_conclusions(service, observations, now_ms=at - 1)[0]['status'])

    def test_current_calendar_is_separate_from_original_scan_and_faults_win(self):
        at = _utc_ms(2026, 9, 7, 8, 40)
        row = evaluated_cycle(at=at).observations[0].to_dict()
        service, observations = sources(row)
        result = current_conclusions(service, observations, now_ms=at)[0]
        self.assertEqual('waiting', result['status'])
        self.assertIn('休市', result['message'])
        self.assertEqual(_utc_ms(2026, 9, 8, 13, 45), result['schedule']['next_window'][0])
        for kind in ('blocked', 'failed'):
            broken = fixtures.cycle(3, kind, at=at).observations[0].to_dict()
            result = current_conclusions(*sources(broken), now_ms=at)[0]
            self.assertEqual('unavailable', result['status'])
            self.assertIn('休市', render_signal_review({**self.report(), 'current_conclusions': [result]}))
        old = legacy_cycle()['observations'][0]
        result = current_conclusions(*sources(old), now_ms=old['observed_at_ms'])[0]
        self.assertEqual('unavailable', result['status'])
        self.assertIn('旧记录', result['message'])
        self.assertEqual(old, result['latest'])

    def report(self):
        fixture = fixtures.SignalReviewTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.initialize([fixtures.cycle(1)])
        return fixture.read()

    def test_historical_window_does_not_choose_current_record(self):
        fixture = fixtures.SignalReviewTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.initialize([fixtures.cycle(1)])
        fixture.log.append_cycle(fixtures.cycle(2, at=fixtures.NOW + 300_000))
        fixture.window = {**fixture.window, 'end_ms': fixtures.NOW + 1}
        report = fixture.read()
        self.assertEqual('observation-1', report['sources']['observations']['latest'][0]['observation_id'])
        self.assertEqual('observation-2', report['current_conclusions'][0]['latest']['observation_id'])
        self.assertEqual('unavailable', report['current_conclusions'][0]['status'])

    def test_live_page_removes_bulk_raw_json_and_static_keeps_audit(self):
        report = self.report()
        record = report['sources']['observations']['records'][0]
        live_page = render_signal_review(report, live=True)
        static = render_signal_review(report)
        self.assertIn('/scan-evidence?', live_page)
        # Service health has one current observation; scan history no longer embeds every payload.
        self.assertNotIn('id="scan-evidence-', live_page)
        self.assertIn('id="scan-evidence-', static)
        self.assertIn(record['result_fingerprint'], static)
        self.assertIn('当前结论', live_page)
        self.assertNotIn('action="/position', static)


class ScanEvidenceEndpointTests(unittest.TestCase):
    def setUp(self):
        self.fixture = live.ReviewLiveServerTests(); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)

    def test_exact_record_query_readonly_escaped_and_fail_closed(self):
        source = self.fixture.fixture
        before = source.log.path.read_bytes()
        query = '/scan-evidence?cycle_id=cycle-1&observation_id=observation-1'
        status, headers, page = self.fixture.request(query)
        self.assertEqual(200, status)
        self.assertIn('observation-1', page)
        self.assertEqual('no-store', headers['Cache-Control'])
        self.assertEqual(before, source.log.path.read_bytes())
        self.assertEqual(404, self.fixture.request(query.replace('observation-1', 'absent'))[0])
        for invalid in ('/scan-evidence?cycle_id=../health.json&observation_id=x', query + '&path=health.json',
                        '/scan-evidence?cycle_id=&observation_id=x', '/scan-evidence?cycle_id=a&cycle_id=b'):
            self.assertEqual(400, self.fixture.request(invalid)[0])
        self.assertEqual(403, self.fixture.request(query, headers={'Origin': 'https://example.com'})[0])
        source.log.invalid_marker_path.write_text('interrupted')
        self.assertEqual(503, self.fixture.request(query)[0])
        with self.assertRaises(ObservationCorruptionError):
            read_scan_evidence(source.data_dir, 'cycle-1', 'observation-1')
