import unittest
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.entry.scanner import scan_entry
from mu_strategy.models import EntryDecisionCode
from mu_strategy.strategies.registry import baseline_strategy_group
from mu_strategy.strategies.instruments import read_instrument_calendars, instrument_calendar
from mu_strategy.core.trading_calendar import (
    CalendarUnavailable, CONTINUOUS_CALENDAR, LEGACY_CALENDAR, US_EQUITIES_CALENDAR,
    evaluate_trading_window, trading_window_schedule,
)
from mu_strategy.strategy import StrategyConfig, is_preferred_us_cash_window
from tests.test_entry_scanner import _candles_ending_at, _utc_ms


class CalendarRegressionTests(unittest.TestCase):
    def test_outside_window_precedes_all_indicator_blocks(self):
        candles = _candles_ending_at(_utc_ms(2026, 9, 8, 8, 15))
        for execution in ('second_pullback', 'break_high', 'direct_next_open'):
            with self.subTest(execution=execution):
                config = replace(baseline_strategy_group('BTC-USDT-SWAP').config, entry_execution=execution)
                with patch('mu_strategy.entry.scanner.build_hourly_context', return_value={c.open_time_ms: 'red' for c in candles}), patch('mu_strategy.entry.scanner.rsi', return_value=[30.] * len(candles)), patch('mu_strategy.entry.scanner.macd', return_value=([], [], [-.1] * (len(candles)-1) + [-.2])):
                    result = scan_entry(config.symbol, candles, candles, config=config)
                self.assertEqual(EntryDecisionCode.CURRENT_BAR_OUTSIDE_TRADING_WINDOW, result.decision_code)

    def test_labor_day_is_closed_for_configured_mu(self):
        candles = _candles_ending_at(_utc_ms(2026, 9, 7, 14, 0))
        config = baseline_strategy_group('MU-USDT-SWAP').config
        result = scan_entry(config.symbol, candles, candles, config=config)
        self.assertEqual('reference_market_closed', result.decision_code.value)

    def test_closed_bar_has_exact_evaluation_time_and_no_indicator_claim(self):
        opened = _utc_ms(2026, 9, 7, 8, 15)
        candles = _candles_ending_at(opened)
        config = baseline_strategy_group('MU-USDT-SWAP').config
        with patch('mu_strategy.entry.scanner.macd', side_effect=AssertionError('must gate first')):
            result = scan_entry(config.symbol, candles, candles, config=config)
        self.assertEqual((opened, opened + 900_000), (result.evaluated_candle_open_ms, result.evaluated_candle_close_ms))
        self.assertIsNone(result.macd_hist)
        self.assertEqual(US_EQUITIES_CALENDAR, result.calendar_id)
        self.assertEqual('holiday', result.calendar_session)


class TradingCalendarTests(unittest.TestCase):
    def setUp(self):
        self.config = baseline_strategy_group('MU-USDT-SWAP').config

    def test_holidays_weekends_and_next_window(self):
        at = _utc_ms(2026, 9, 7, 8, 40)
        schedule = trading_window_schedule(at, self.config)
        self.assertEqual('holiday', schedule['session'])
        self.assertFalse(schedule['allowed'])
        self.assertEqual([], schedule['today_windows'])
        self.assertEqual(_utc_ms(2026, 9, 8, 13, 45), schedule['next_window'][0])
        for date in ((2025, 1, 9), (2026, 9, 6), (2027, 6, 18), (2028, 7, 4)):
            self.assertFalse(is_preferred_us_cash_window(_utc_ms(*date, 14, 0), self.config))

    def test_dst_and_beijing_midnight_preserve_eastern_windows(self):
        for at in (_utc_ms(2026, 3, 6, 14, 45), _utc_ms(2026, 3, 9, 13, 45),
                   _utc_ms(2026, 9, 8, 18, 30), _utc_ms(2026, 11, 2, 19, 30)):
            self.assertTrue(is_preferred_us_cash_window(at, self.config))
        self.assertFalse(is_preferred_us_cash_window(_utc_ms(2026, 3, 6, 13, 45), self.config))
        self.assertTrue(is_preferred_us_cash_window(_utc_ms(2026, 9, 8, 15, 30) + 59_999, self.config))
        self.assertFalse(is_preferred_us_cash_window(_utc_ms(2026, 9, 8, 15, 31), self.config))

    def test_early_close_clips_even_custom_window_exclusively(self):
        config = replace(self.config, trading_windows_et=(('09:45', '15:45'),))
        self.assertTrue(is_preferred_us_cash_window(_utc_ms(2026, 11, 27, 17, 59) + 59_999, config))
        self.assertFalse(is_preferred_us_cash_window(_utc_ms(2026, 11, 27, 18, 0), config))
        schedule = trading_window_schedule(_utc_ms(2026, 11, 27, 19, 30), self.config)
        self.assertEqual('early_close', schedule['session'])
        self.assertEqual(1, len(schedule['today_windows']))
        self.assertEqual(_utc_ms(2026, 11, 30, 14, 45), schedule['next_window'][0])

    def test_continuous_is_explicit_and_preserves_strategy_windows(self):
        config = replace(self.config, trading_calendar_id=CONTINUOUS_CALENDAR, trading_calendar_sha256=None)
        self.assertTrue(is_preferred_us_cash_window(_utc_ms(2026, 9, 7, 14, 0), config))
        self.assertTrue(is_preferred_us_cash_window(_utc_ms(2026, 9, 6, 14, 0), config))
        self.assertFalse(is_preferred_us_cash_window(_utc_ms(2026, 9, 7, 8, 0), config))
        self.assertEqual(LEGACY_CALENDAR, instrument_calendar('BTC'))
        self.assertEqual(US_EQUITIES_CALENDAR, instrument_calendar('MUUSDT'))
        self.assertEqual(US_EQUITIES_CALENDAR, instrument_calendar('Space X'))

    def test_unknown_dates_calendar_digest_and_timezone_fail_closed(self):
        with self.assertRaises(CalendarUnavailable):
            evaluate_trading_window(_utc_ms(2029, 1, 2, 15, 0), self.config)
        with self.assertRaises(CalendarUnavailable):
            StrategyConfig(trading_calendar_id='guess')
        with self.assertRaises(ValueError):
            replace(self.config, trading_calendar_sha256='a' * 64)
        with patch('mu_strategy.core.trading_calendar.ZoneInfo', side_effect=RuntimeError('tz missing')):
            with self.assertRaisesRegex(CalendarUnavailable, 'timezone'):
                evaluate_trading_window(_utc_ms(2026, 9, 8, 14, 0), self.config)

    def test_holiday_blocks_add_but_keeps_stop_and_leverage_risk_evaluation(self):
        from mu_strategy.models import Candle
        from mu_strategy.strategies.position_rules import PositionFillSnapshot, PositionStateSnapshot, decide_pyramid_add
        from mu_strategy.live_exit import evaluate_exit
        at = _utc_ms(2026, 9, 7, 14, 0)
        position = PositionStateSnapshot((PositionFillSnapshot(at - 900_000, 100, 1),), 95, 100, 95)
        candle = Candle(at, 104, 105, 94, 104, 1000)
        decision = decide_pyramid_add(position, candle, rsi_value=60, macd_hist=.2, previous_macd_hist=.1, regime='green', config=self.config)
        self.assertFalse(decision.should_add)
        exit_result = evaluate_exit(position, candle, index=0, candles=[candle], regime='green', config=self.config)
        self.assertEqual('stop', exit_result.exit_reason)
        candle = replace(candle, low=75)
        self.assertEqual('non_session_liquidation_risk', evaluate_exit(position, candle, index=0, candles=[candle], regime='green', config=self.config).exit_reason)

    def test_unavailable_calendar_preserves_stop_but_never_asserts_no_exit(self):
        from mu_strategy.models import Candle
        from mu_strategy.strategies.position_rules import PositionFillSnapshot, PositionStateSnapshot, decide_pyramid_add
        from mu_strategy.live_exit import evaluate_exit, observe_okx_position

        for symbol in ('MU-USDT-SWAP', 'META-USDT-SWAP', 'SPCX-USDT-SWAP'):
            config = baseline_strategy_group(symbol).config
            for year in (1970, 2024, 2029):
                with self.subTest(symbol=symbol, year=year):
                    at = _utc_ms(year, 1, 2, 15, 0)
                    position = PositionStateSnapshot((PositionFillSnapshot(at, 100, 1),), 95, 100, 95)
                    candle = Candle(at, 100, 101, 94, 100, 1000)
                    result = evaluate_exit(position, candle, index=0, candles=[candle], regime='green', config=config)
                    self.assertTrue(result.exit_triggered)
                    self.assertEqual('stop', result.exit_reason)
                    self.assertIn('2025 through 2028', result.calendar_error)
                    self.assertEqual(95, result.stop_after_candle_if_open)
                    safe = replace(candle, low=99)
                    unknown = evaluate_exit(position, safe, index=0, candles=[safe], regime='green', config=config)
                    self.assertIsNone(unknown.exit_triggered)
                    self.assertEqual('calendar_unavailable', unknown.trigger_basis)
                    shadow = observe_okx_position({'instId': symbol, 'pos': '1', 'avgPx': '100'}, candles=[candle], regime='green', config=config)
                    self.assertEqual('unknown', shadow.decision_status)
                    self.assertTrue(shadow.assumption_evaluation.exit_triggered)
                    with self.assertRaises(CalendarUnavailable):
                        scan_entry(symbol, [candle], [candle], config=config)
                    with self.assertRaises(CalendarUnavailable):
                        decide_pyramid_add(position, candle, rsi_value=60, macd_hist=.2, previous_macd_hist=.1, regime='green', config=config)

    def test_missing_timezone_preserves_stop_without_guessing_session(self):
        from mu_strategy.models import Candle
        from mu_strategy.strategies.position_rules import PositionFillSnapshot, PositionStateSnapshot
        from mu_strategy.live_exit import evaluate_exit
        at = _utc_ms(2026, 9, 8, 14, 0)
        position = PositionStateSnapshot((PositionFillSnapshot(at, 100, 1),), 95, 100, 95)
        candle = Candle(at, 100, 101, 75, 100, 1000)
        with patch('mu_strategy.core.trading_calendar.ZoneInfo', side_effect=RuntimeError('tz missing')):
            result = evaluate_exit(position, candle, index=0, candles=[candle], regime='green', config=self.config)
        self.assertTrue(result.exit_triggered)
        self.assertEqual('stop', result.exit_reason)
        self.assertIn('timezone', result.calendar_error)

    def test_instrument_file_is_strict_and_does_not_change_refresh_classification(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'instruments.json'
            good = {'schema_version': 1, 'default_calendar': LEGACY_CALENDAR, 'symbols': {'MU-USDT-SWAP': US_EQUITIES_CALENDAR}}
            path.write_text(json.dumps(good))
            self.assertEqual(good, read_instrument_calendars(path))
            for bad in ([], {**good, 'future': 1}, {**good, 'schema_version': True},
                        {**good, 'symbols': {'MU': US_EQUITIES_CALENDAR}}, {**good, 'default_calendar': 'unknown'}):
                path.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    read_instrument_calendars(path)
            path.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaises(ValueError):
                read_instrument_calendars(path)
        members = json.loads((Path(__file__).resolve().parents[1] / 'config/okx_stock_tokens.json').read_text())
        self.assertEqual({'MU-USDT-SWAP', 'META-USDT-SWAP', 'SPCX-USDT-SWAP'}, set(members))
