import io
import json
import shutil
import unittest
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy import cli
from mu_strategy.canonical import canonical_sha256
from mu_strategy.commands import okx_demo_loop
from mu_strategy.experiments import strategy_ladder
from mu_strategy.market_data.trusted_data.store import TrustedDataStore, candles_content_sha256
from mu_strategy.research.historical_data import HistoricalGenerationError, load_historical_window
from mu_strategy.viz import backtest as viz_backtest


ROOT = Path(__file__).resolve().parents[1]
RUN_ID = 'e702be27d2de4b2d92b12bf01c70d02d'
SYMBOL = 'MU-USDT-SWAP'


class HistoricalReplayTests(unittest.TestCase):
    def test_provenance_binds_actual_configuration_and_selected_candles(self):
        window = load_historical_window(data_dir=ROOT / 'data/live', generation_id=RUN_ID, symbol=SYMBOL, days=2)
        configuration = {'leverage': 3, 'strategy': 'baseline'}
        first = json.loads(window.provenance(configuration))
        second = json.loads(window.provenance({**configuration, 'leverage': 5}))
        self.assertEqual(configuration, first['configuration'])
        self.assertEqual(canonical_sha256(configuration), first['configuration_sha256'])
        self.assertNotEqual(first['configuration_sha256'], second['configuration_sha256'])
        self.assertEqual(first['code_sha256'], second['code_sha256'])
        self.assertEqual(64, len(first['code_sha256']))
        for interval, bars in window.candles_by_interval.items():
            self.assertEqual(candles_content_sha256(list(bars)), first['input_content_sha256_by_interval'][interval])

    def test_default_cli_still_blocks_stale_data_and_demo_rejects_replay_flag(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = copy_generation(root)
            (data / 'current.json').write_text(json.dumps({
                'schema_version': 1, 'generation_id': RUN_ID,
                'manifest': f'generations/{RUN_ID}/manifest.json',
            }), encoding='utf-8')
            with patch('sys.argv', ['cli', '--data-dir', str(data), '--days', '2', '--report', str(root / 'report.md')]), patch(
                'mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms', return_value=1800000000000,
            ), redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit) as error:
                cli.main()
            self.assertEqual(2, error.exception.code)
            self.assertIn('stale', stderr.getvalue())
            self.assertFalse((root / 'report.md').exists())
        with patch('sys.argv', ['okx_demo_loop', '--generation-id', RUN_ID, '--dry-run', '--once']), redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit) as error:
            okx_demo_loop.main()
        self.assertEqual(2, error.exception.code)
        self.assertIn('unrecognized arguments', stderr.getvalue())

    def test_replay_cannot_overwrite_the_trusted_pointer_or_manifest(self):
        for module, flag in ((cli, '--report'), (viz_backtest, '--output')):
            with self.subTest(module=module.__name__), TemporaryDirectory() as tmp:
                data = copy_generation(Path(tmp))
                for output in (data / 'current.json', data / 'generations' / RUN_ID / 'manifest.json'):
                    before = file_bytes(data)
                    with patch('sys.argv', ['research', '--generation-id', RUN_ID, '--data-dir', str(data), flag, str(output)]), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                        module.main()
                    self.assertEqual(2, error.exception.code)
                    self.assertEqual(before, file_bytes(data))

    def test_real_bundle_window_uses_common_closed_boundary_and_exact_coverage(self):
        window = load_historical_window(data_dir=ROOT / 'data/live', generation_id=RUN_ID, symbol=SYMBOL, days=2)
        self.assertEqual(2 * 86400000, window.end_ms - window.start_ms)
        self.assertEqual(0, window.end_ms % 3600000)
        for interval, step in (('15m', 900000), ('1h', 3600000)):
            bars = window.candles_by_interval[interval]
            self.assertEqual(list(range(window.start_ms, window.end_ms, step)), [bar.open_time_ms for bar in bars])
            self.assertLessEqual(bars[-1].open_time_ms + step, window.generation.completed_at_ms)

    def test_invalid_missing_and_insufficient_generations_fail_closed(self):
        for run_id in ('', '..', '../escape', 'a/b', 'a\\b', 'C:escape', '/absolute', ' missing ', 'does-not-exist'):
            with self.subTest(run_id=run_id), self.assertRaises(HistoricalGenerationError):
                load_historical_window(data_dir=ROOT / 'data/live', generation_id=run_id, symbol=SYMBOL, days=2)
        for days in (0, -1, 9999):
            with self.subTest(days=days), self.assertRaises(HistoricalGenerationError):
                load_historical_window(data_dir=ROOT / 'data/live', generation_id=RUN_ID, symbol=SYMBOL, days=days)

    def test_all_ordinary_entries_replay_without_pointer_clock_network_or_data_writes(self):
        for name, module, extra, filenames in (
            ('cli', cli, ['--days', '2', '--report', 'report.md'], ('report.md',)),
            ('visualize', viz_backtest, ['--days', '2', '--output', 'report.html'], ('report.html',)),
            ('ladder', strategy_ladder, ['--window-days', '1', '--windows', '2', '--report', 'ladder.md', '--html-report', 'ladder.html', '--conclusion-index', 'conclusions.json'], ('ladder.md', 'ladder.html', 'conclusions.json')),
        ):
            with self.subTest(entry=name), TemporaryDirectory() as tmp:
                root = Path(tmp)
                data = copy_generation(root)
                pointer = data / 'current.json'
                pointer.write_text('malformed current pointer', encoding='utf-8')
                before = file_bytes(data)
                argv = [name, '--generation-id', RUN_ID, '--data-dir', str(data), *extra]
                for index, arg in enumerate(argv):
                    if arg in filenames:
                        argv[index] = str(root / arg)
                with ExitStack() as stack:
                    stack.enter_context(patch.object(TrustedDataStore, 'read_manifest', side_effect=AssertionError('current pointer used')))
                    stack.enter_context(patch.object(TrustedDataStore, 'write_segmented_dataset', side_effect=AssertionError('data writer used')))
                    stack.enter_context(patch('mu_strategy.market_data.trusted_data.contracts.SystemClock.now_ms', side_effect=AssertionError('wall clock used')))
                    stack.enter_context(patch('urllib.request.urlopen', side_effect=AssertionError('network used')))
                    stack.enter_context(patch.object(module, 'refresh_trusted_candle_bundle', side_effect=AssertionError('live loader used')))
                    stack.enter_context(patch('sys.argv', argv))
                    stack.enter_context(redirect_stdout(io.StringIO()))
                    module.main()
                    first = {path: (root / path).read_bytes() for path in filenames}
                    pointer.write_text('{"run_id":"different"}', encoding='utf-8')
                    module.main()
                    self.assertEqual(first, {path: (root / path).read_bytes() for path in filenames})
                after = file_bytes(data)
                self.assertEqual({k:v for k,v in before.items() if k != 'current.json'}, {k:v for k,v in after.items() if k != 'current.json'})
                for filename, content in first.items():
                    text = content.decode('utf-8')
                    if filename != 'conclusions.json':
                        for label in ('historical_replay', RUN_ID, 'code_sha256', 'configuration_sha256', 'content_sha256_by_interval', 'start_ms', 'end_ms'):
                            self.assertIn(label, text)
                        self.assertNotIn(str(data), text)

    def test_hash_status_and_timing_failures_do_not_write_reports(self):
        for failure in ('hash', 'failed', 'unusable', 'gap', 'future', 'missing'):
            with self.subTest(failure=failure), TemporaryDirectory() as tmp:
                root = Path(tmp)
                data = copy_generation(root)
                manifest_path = data / 'generations' / RUN_ID / 'manifest.json'
                manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                if failure == 'failed':
                    manifest['attempt_status'] = 'failed'
                elif failure == 'unusable':
                    manifest['snapshot_usability'] = 'invalid'
                elif failure == 'future':
                    manifest['completed_at_ms'] = manifest['started_at_ms']
                    health = manifest['symbols'][SYMBOL]['intervals']['1h']
                    manifest['completed_at_ms'] = health['last_timestamp_ms']
                    manifest['started_at_ms'] = manifest['completed_at_ms'] - 1000
                else:
                    csv = data / 'generations' / RUN_ID / 'okx' / SYMBOL / '1h.csv'
                    if failure == 'missing':
                        csv.unlink()
                    else:
                        lines = csv.read_text(encoding='utf-8').splitlines()
                        del lines[-2]
                        csv.write_text('\n'.join(lines) + '\n', encoding='utf-8')
                        if failure == 'gap':
                            health = manifest['symbols'][SYMBOL]['intervals']['1h']
                            candles = TrustedDataStore(data_dir=data).read_csv(csv)
                            health['rows'] = len(candles)
                            health['content_sha256'] = candles_content_sha256(candles)
                manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
                report = root / 'report.md'
                with patch('sys.argv', ['cli', '--generation-id', RUN_ID, '--data-dir', str(data), '--days', '2', '--report', str(report)]), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    cli.main()
                self.assertEqual(2, error.exception.code)
                self.assertFalse(report.exists())


def copy_generation(root):
    data = root / 'data'
    shutil.copytree(ROOT / 'data/live/generations' / RUN_ID, data / 'generations' / RUN_ID)
    return data


def file_bytes(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob('*') if path.is_file()}
