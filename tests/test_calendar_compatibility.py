import copy
import unittest
from dataclasses import replace
from unittest.mock import patch

from mu_strategy.core.trading_calendar import LEGACY_CALENDAR, US_EQUITIES_CALENDAR
from mu_strategy.observations import Stage0ObservationCycle, ObservationSchemaError
from mu_strategy.research.strategy_releases import (
    StrategyConfigPayloadV1, StrategyConfigPayloadV2, StrategyReleaseSchemaError,
    StrategyReleaseCandidateV1, parse_strategy_config_payload,
)
from mu_strategy.strategies.registry import baseline_strategy_group
from mu_strategy.strategy import StrategyConfig
from tests import test_signal_review as review
from tests import test_position_management as positions
from tests.test_strategy_release_provenance import _candidate


def legacy_config():
    # Values/hash verified against the pre-calendar implementation, not today's registry.
    return StrategyConfigPayloadV1.from_config(StrategyConfig(symbol='MU-USDT-SWAP', entry_execution='second_pullback', fib_lookback=8))


def legacy_cycle():
    wire = review.cycle(1).to_dict()
    wire['schema_version'] = 1
    row = wire['observations'][0]
    row['schema_version'] = 1
    for key in ('evaluated_candle_open_ms', 'evaluated_candle_close_ms', 'calendar_id', 'calendar_sha256', 'calendar_session', 'trading_windows_et'):
        row['scan_result'].pop(key)
    row['strategy_config_fingerprint'] = 'e54c9e0ff49bb601f4579dbefba2e486073072edbaadacfa2547dae547604549'
    row['result_fingerprint'] = '33ab311b46722d618e22f83cfb10ec4df983fb90cc47d5853914a0ba0d01b390'
    return wire


class CalendarCompatibilityTests(unittest.TestCase):
    def test_v1_config_keeps_original_shape_hash_and_replay_semantics(self):
        original = legacy_config()
        self.assertEqual('2646c855861c7bda4a2358aa91521a9e494df844a8ebf73b84739e851646b371', original.strategy_config_sha256)
        self.assertNotIn('trading_calendar_id', original.to_dict()['fields'])
        restored = parse_strategy_config_payload(original.to_dict())
        self.assertEqual(original, restored)
        self.assertEqual(LEGACY_CALENDAR, restored.to_strategy_config().trading_calendar_id)
        self.assertEqual(original.to_dict(), StrategyConfigPayloadV1.from_config(restored.to_strategy_config()).to_dict())
        candidate = _candidate(config=original)
        self.assertEqual(candidate, StrategyReleaseCandidateV1.from_dict(candidate.to_dict()))

    def test_v2_binds_calendar_and_cannot_be_downgraded_or_malformed(self):
        config = baseline_strategy_group('MU-USDT-SWAP').config
        payload = StrategyConfigPayloadV2.from_config(config)
        self.assertEqual(US_EQUITIES_CALENDAR, payload.values['trading_calendar_id'])
        self.assertEqual(config, parse_strategy_config_payload(payload.to_dict()).to_strategy_config())
        with self.assertRaises(StrategyReleaseSchemaError):
            StrategyConfigPayloadV1.from_config(config)
        with self.assertRaises(StrategyReleaseSchemaError):
            StrategyConfigPayloadV1.from_dict(payload.to_dict())
        for update in ({'trading_calendar_sha256': 'a' * 64}, {'trading_calendar_id': 'unknown'}):
            with self.assertRaises(StrategyReleaseSchemaError):
                parse_strategy_config_payload({**payload.to_dict(), 'fields': {**payload.to_dict()['fields'], **update}})
        for version in (True, 3):
            with self.assertRaises(StrategyReleaseSchemaError):
                parse_strategy_config_payload({**payload.to_dict(), 'schema_version': version})
        changed = replace(config, trading_calendar_id=LEGACY_CALENDAR, trading_calendar_sha256=None)
        self.assertNotEqual(payload.strategy_config_sha256, StrategyConfigPayloadV2.from_config(changed).strategy_config_sha256)

    def test_old_observation_round_trips_without_inventing_candle_time(self):
        wire = legacy_cycle()
        restored = Stage0ObservationCycle.from_dict(wire)
        self.assertEqual(wire, restored.to_dict())
        self.assertIsNone(restored.observations[0].scan_result.evaluated_candle_close_ms)
        new = copy.deepcopy(wire)
        new['observations'][0]['scan_result']['evaluated_candle_close_ms'] = review.NOW
        with self.assertRaises(ObservationSchemaError):
            Stage0ObservationCycle.from_dict(new)
        new = copy.deepcopy(wire)
        new['schema_version'] = 2
        with self.assertRaises(ObservationSchemaError):
            Stage0ObservationCycle.from_dict(new)

    def test_new_record_context_is_bound_to_fingerprint(self):
        from mu_strategy.entry.scanner import scan_entry
        from mu_strategy.scan_cycle import ScanCycle
        from tests.factories.scan_cycle import trusted_scan_bundle
        from tests.test_entry_scanner import _candles_ending_at, _utc_ms
        from unittest.mock import Mock
        opened = _utc_ms(2026, 9, 7, 8, 15)
        config = baseline_strategy_group('MU-USDT-SWAP').config
        bundle = replace(trusted_scan_bundle(symbol=config.symbol), candles_by_interval={'15m': _candles_ending_at(opened), '1h': []})
        scanner = ScanCycle(clock=Mock(now_ms=lambda: opened + 2_000_000))
        scanner.scan_symbol(symbol=config.symbol, source='watchlist', bundle=bundle, requested_intervals=('15m', '1h'), strategy_name='baseline', strategy_config=config, scanner=scan_entry, data_failure=None)
        wire = scanner.observations().to_dict()
        self.assertEqual(opened + 900_000, wire['observations'][0]['scan_result']['evaluated_candle_close_ms'])
        self.assertEqual(wire, Stage0ObservationCycle.from_dict(wire).to_dict())
        wire['observations'][0]['scan_result']['evaluated_candle_open_ms'] -= 900_000
        wire['observations'][0]['scan_result']['evaluated_candle_close_ms'] -= 900_000
        with self.assertRaisesRegex(ObservationSchemaError, 'fingerprint'):
            Stage0ObservationCycle.from_dict(wire)

    def test_v1_and_v2_cycles_coexist_in_one_unchanged_log(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from mu_strategy.observations import JsonlObservationRepository
        old = Stage0ObservationCycle.from_dict(legacy_cycle())
        new = review.cycle(2)
        with TemporaryDirectory() as directory:
            repository = JsonlObservationRepository(Path(directory) / 'observations.jsonl')
            repository.append_cycle(old)
            first = repository.path.read_bytes()
            repository.append_cycle(new)
            saved = repository.path.read_bytes()
            self.assertTrue(saved.startswith(first))
            self.assertEqual((old, new), repository.read_cycles())
            self.assertEqual(saved, repository.path.read_bytes())

    def test_existing_position_v1_config_remains_frozen_on_reopen_and_reconfirm(self):
        fixture = positions.PositionManagementFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        template = positions.baseline_configuration(review.SYMBOL)
        old = legacy_config()
        template.update(configuration=old.to_dict(), configuration_sha256=old.strategy_config_sha256)
        with patch('mu_strategy.manual_positions.baseline_configuration', return_value=template), patch('tests.test_position_management.baseline_configuration', return_value=template):
            identity = fixture.ready()
        before = fixture.ledger.path.read_bytes()
        self.assertTrue(fixture.ledger.view()['available'])
        self.assertEqual(before, fixture.ledger.path.read_bytes())
        fixture.confirm(identity)
        latest = fixture.position(identity)['management_inputs']['latest']
        self.assertEqual(old.to_dict(), latest['configuration'])
        self.assertEqual(old.strategy_config_sha256, latest['configuration_sha256'])
