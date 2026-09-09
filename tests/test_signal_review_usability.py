"""Status wording and the existing manual-fill journey, using isolated ledgers."""
import copy
import html
import re
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from mu_strategy.manual_positions import ManualPositionLedger
from mu_strategy.observations import Stage0ObservationCycle
from mu_strategy.signal_feedback import SignalFeedbackStore
from mu_strategy.signal_review import current_conclusions
from mu_strategy.signal_review_server import make_review_server
from mu_strategy.viz.signal_review import _current_card, render_signal_review
from tests import test_signal_review as fixtures
from tests import test_manual_positions as manual
from tests.test_calendar_compatibility import legacy_cycle
from tests.test_signal_review_calendar import evaluated_cycle, sources
from tests.test_entry_scanner import _utc_ms


class ReviewStatusUsabilityTests(unittest.TestCase):
    def test_fresh_legacy_scan_keeps_service_window_and_missing_evidence_separate(self):
        # This is a validated schema-1 fixture, not a guessed or repaired record.
        row = Stage0ObservationCycle.from_dict(legacy_cycle()).observations[0].to_dict()
        original = copy.deepcopy(row)
        service, observations = sources(row)
        item = current_conclusions(service, observations, now_ms=row['observed_at_ms'])[0]
        page = _current_card(item, live=True)
        self.assertTrue(service['view']['healthy'])
        self.assertEqual('running', service['view']['runtime'])
        self.assertEqual('unavailable', item['status'])
        self.assertIn('最近记录缺少评估依据，当前结论待核实', page)
        self.assertIn('开始时间缺失', page)
        self.assertIn('结束时间缺失', page)
        self.assertIn('参考日历 ID', page)
        self.assertNotIn('过期', item['message'])
        self.assertNotIn('超过', item['message'])
        self.assertNotIn('停', item['message'])
        self.assertEqual(original, row)

    def test_each_missing_field_is_named_without_inventing_other_missing_fields(self):
        fields = {'evaluated_candle_open_ms': '评估 K 线开始时间',
                  'evaluated_candle_close_ms': '评估 K 线结束时间', 'calendar_id': '参考日历 ID',
                  'calendar_sha256': '日历内容指纹', 'calendar_session': '评估时日历状态',
                  'trading_windows_et': '评估时策略窗口'}
        for field, label in fields.items():
            for remove in (False, True):
                with self.subTest(field=field, remove=remove):
                    row = evaluated_cycle().observations[0].to_dict()
                    if remove:
                        row['scan_result'].pop(field)
                    else:
                        row['scan_result'][field] = None
                    item = current_conclusions(*sources(row), now_ms=row['observed_at_ms'])[0]
                    self.assertEqual('unavailable', item['status'])
                    self.assertEqual('最近记录缺少评估依据，当前结论待核实。缺少：' + label + '。', item['message'])

    def test_complete_window_inside_outside_holiday_and_next_beijing_day(self):
        cases = [(_utc_ms(2026, 9, 8, 14, 20), 'current', '当前处于策略窗口内', '2026-09-08 21:45'),
                 (_utc_ms(2026, 9, 8, 12, 0), 'waiting', '当前处于策略窗口外', '2026-09-08 21:45'),
                 (_utc_ms(2026, 9, 7, 8, 40), 'waiting', '当前参考日休市', '2026-09-08 21:45'),
                 (_utc_ms(2026, 9, 8, 20, 20), 'waiting', '当前处于策略窗口外', '2026-09-09 21:45')]
        for at, status, headline, window in cases:
            with self.subTest(at=at):
                row = evaluated_cycle(at=at).observations[0].to_dict()
                item = current_conclusions(*sources(row), now_ms=at)[0]
                page = _current_card(item)
                self.assertEqual(status, item['status'])
                self.assertIn(headline, page)
                self.assertIn(window, page)
                for label in ('扫描记录时间（观察时点）', '评估 K 线 / 15m', '当前查询时间', '日历规则与来源'):
                    self.assertIn(label, page)
                self.assertIn('开放日不等于全天允许入场', page)
                self.assertIn('参考市场休市不代表 OKX 合约停市', page)
                if status == 'current':
                    self.assertLess(page.index('当前窗口：'), page.index('下一窗口：'))
                self.assertNotIn(' open>', page)

    def test_last_known_ready_is_not_promoted_for_source_run_config_or_age_failures(self):
        row = evaluated_cycle().observations[0].to_dict()
        at = row['observed_at_ms']
        for kind in ('old_run', 'different_observation', 'unreadable', 'truncated', 'old_scan', 'old_candle', 'changed_config'):
            with self.subTest(kind=kind):
                candidate = copy.deepcopy(row)
                now = at
                if kind == 'old_scan':
                    now += 600_001
                if kind == 'old_candle':
                    candidate['scan_result']['evaluated_candle_open_ms'] -= 1_800_000
                    candidate['scan_result']['evaluated_candle_close_ms'] -= 1_800_000
                if kind == 'changed_config':
                    candidate['strategy_config_fingerprint'] = 'f' * 64
                service, observations = sources(candidate)
                if kind == 'old_run':
                    service['view']['last_cycle']['service_run_id'] = 'previous'
                if kind == 'different_observation':
                    service['view']['last_cycle']['scan']['cycle']['observations'][0]['observation_id'] = 'other'
                if kind in ('unreadable', 'truncated'):
                    observations['state'] = 'unavailable' if kind == 'unreadable' else 'incomplete'
                item = current_conclusions(service, observations, now_ms=now)[0]
                self.assertEqual('unavailable', item['status'])
                self.assertIn('仅为记录当时的结论', _current_card(item))
                self.assertNotIn('最近一轮可用于人工复核', _current_card(item))


class ReviewFillJourneyTests(manual.ManualPositionTestCase):
    # Reuse the real localhost handler, SQLite ledger and request helpers.
    request = manual.ManualPositionServerTests.request

    def setUp(self):
        super().setUp()
        self.server = make_review_server(self.fixture.data_dir, port=0, clock=self.fixture.clock)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def form_values(self, page):
        return {name: html.unescape(value) for name, value in
                re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', page)}

    def test_current_card_uses_only_exact_unique_entry_observation(self):
        report = self.fixture.read()
        page = render_signal_review(report, live=True)
        card = page.split('data-current-symbol="MU-USDT-SWAP"', 1)[1].split('</article>', 1)[0]
        self.assertIn('/positions?event_id=' + self.entry.event_id, card)
        for kind in ('different', 'ambiguous', 'unavailable', 'absent'):
            with self.subTest(kind=kind):
                changed = copy.deepcopy(report)
                notifications = changed['sources']['notifications']
                if kind == 'absent':
                    notifications['records'] = []
                elif kind == 'unavailable':
                    notifications['state'] = 'unavailable'
                else:
                    entry = next(item for item in notifications['records'] if item['event']['kind'] == 'entry_review')
                    if kind == 'different':
                        entry['event']['observation']['observation_id'] = 'same-symbol-nearby-event'
                    else:
                        notifications['records'].append(copy.deepcopy(entry))
                page = render_signal_review(changed, live=True)
                card = page.split('data-current-symbol="MU-USDT-SWAP"', 1)[1].split('</article>', 1)[0]
                self.assertIn('/positions?symbol=MU-USDT-SWAP', card)
                self.assertNotIn('event_id=', card)

    def test_linked_and_unlinked_save_reach_position_and_state_without_duplicate_fills(self):
        for linked in (True, False):
            with self.subTest(linked=linked):
                entry = '/positions?' + urlencode({'event_id': self.entry.event_id} if linked else {'symbol': fixtures.SYMBOL})
                if not linked:
                    entry += '&new=1'  # Intentionally independent from the first position.
                status, _, page = self.request(entry)
                self.assertEqual(200, status)
                self.assertIn('name="symbol" type="text" value="MU-USDT-SWAP"', page)
                for name in ('price', 'quantity', 'executed_at'):
                    self.assertRegex(page, f'name="{name}" type="[^"]+" value=""')
                self.assertIn('请选择单位', page)
                if not linked:
                    self.assertIn('信号来源未知', page)
                payload = self.payload(**self.form_values(page), note='独立浏览器等价验收')
                status, headers, _ = self.request('/positions', payload)
                self.assertEqual(303, status)
                receipt_url = urlsplit(headers['Location'])
                self.assertEqual('save-result', receipt_url.fragment)
                self.assertEqual(headers['Location'], self.request('/positions', payload)[1]['Location'])
                receipt = self.request(headers['Location'].split('#')[0])[2]
                identity = payload['position_id']
                self.assertIn('已保存实际成交记录（人工确认）', receipt)
                self.assertIn(f'href="#position-{identity}"', receipt)
                self.assertIn(f'/position-state?position_id={identity}#position-form', receipt)
                self.assertIn('href="/positions#position-form">记录成交', receipt)
                self.assertNotIn('action="/positions"', receipt)
                position = next(p for p in self.ledger.read() if p['position_id'] == identity)
                self.assertEqual(1, len(position['fills']))
                self.assertEqual('unconfirmed', position['current_state']['status'])
                self.assertEqual('unconfigured', position['management_inputs']['status'])
                self.assertEqual(self.entry.event_id if linked else None, (position['signal_source'] or {}).get('event_id'))
                state_page = self.request(f'/position-state?position_id={identity}')[2]
                state = {**self.form_values(state_page), 'stage': '', 'stop_price': '', 'note': '暂不确定', 'confirmed': 'yes'}
                status, headers, _ = self.request('/position-state', state)
                self.assertEqual(303, status)
                self.assertIn('已保存当前持仓状态', self.request(headers['Location'].split('#')[0])[2])
                self.assertEqual(303, self.request('/position-state', state)[0])
                position = next(p for p in self.ledger.read() if p['position_id'] == identity)
                self.assertEqual(1, len(position['state_history']))
                self.assertIsNone(position['current_state']['stage'])
                self.assertIsNone(position['current_state']['stop_price'])
                self.assertEqual(1, len(position['fills']))

    def test_existing_open_positions_offer_append_then_explicit_independent_creation(self):
        first = self.save(self.payload(label='第一笔'))
        second = self.save(self.payload(label='第二笔', event_id=''))
        for entry in ('/positions', '/positions?symbol=MU-USDT-SWAP', '/positions?event_id=' + self.entry.event_id):
            with self.subTest(entry=entry):
                page = self.request(entry)[2]
                self.assertIn(f'/positions?position_id={first}#position-form', page)
                self.assertIn(f'/positions?position_id={second}#position-form', page)
                self.assertNotIn('action="/positions"', page)
                self.assertIn('这是另一笔独立持仓，继续新建', page)
        append = self.request('/positions?position_id=' + first)[2]
        payload = self.payload(**self.form_values(append))
        self.assertEqual('append', payload['command'])
        self.assertEqual(303, self.request('/positions', payload)[0])
        self.assertEqual(303, self.request('/positions', payload)[0])
        self.assertEqual(2, len(self.ledger.read()))
        self.assertEqual(2, len(next(p for p in self.ledger.read() if p['position_id'] == first)['fills']))

    def test_validation_and_storage_failure_keep_identity_and_values_without_success(self):
        for failure in ('validation', 'storage'):
            with self.subTest(failure=failure):
                payload = self.payload(quantity='bad' if failure == 'validation' else '2', note='保留输入')
                if failure == 'storage':
                    with patch.object(ManualPositionLedger, 'save', side_effect=OSError('fixture unavailable')):
                        status, _, page = self.request('/positions', payload)
                else:
                    status, _, page = self.request('/positions', payload)
                self.assertEqual(400 if failure == 'validation' else 503, status)
                self.assertIn(payload['request_id'], page)
                self.assertIn('保留输入', page)
                self.assertIn('保留关联事件', page)
                self.assertNotIn('已保存实际成交记录', page)
                self.assertFalse(self.ledger.path.exists())

    def test_feedback_and_read_only_exports_do_not_create_fills_or_offer_writes(self):
        SignalFeedbackStore(self.fixture.data_dir).save(self.entry.event_id, 'traded', '', now_ms=fixtures.NOW)
        report = self.fixture.read()
        self.assertEqual([], report['positions']['positions'])
        self.assertFalse(self.ledger.path.exists())
        static = render_signal_review(report)
        self.assertIn('导出快照 · 只读', static)
        self.assertIn('只是提醒处理标记', static)
        self.assertNotIn('href="/positions', static)
        self.assertNotIn('action="/position', static)
        self.assertNotIn('class="feedback-form"', static)

    def test_prefill_parameters_keep_loopback_and_source_validation(self):
        for query in ('symbol=MU-USDT-SWAP&symbol=BTC-USDT-SWAP', 'symbol=%3Cscript%3E',
                      'symbol=MU-USDT-SWAP&event_id=' + self.entry.event_id,
                      'symbol=MU-USDT-SWAP&position_id=' + 'f' * 32, 'new=2'):
            self.assertIn(self.request('/positions?' + query)[0], (400, 404))
        self.assertEqual(403, self.request('/positions?symbol=MU-USDT-SWAP', origin='https://attacker.example')[0])
        self.assertNotIn('已保存实际成交记录', self.request('/positions?saved=1')[2])


if __name__ == '__main__':
    unittest.main()
