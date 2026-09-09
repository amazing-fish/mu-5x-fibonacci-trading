import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.replay_readonly_scan import capture, SYMBOLS


class ReadOnlyScanReaderTests(unittest.TestCase):
    def test_real_strict_reader_matrix_keeps_generation_classification_and_side_effect_boundaries(self):
        with TemporaryDirectory() as directory:
            results = capture(Path(directory))
        blocked = {"stale", "missing_manifest", "invalid_manifest"}
        failures = {"scanner_error", "unknown", "invalid"}
        for case, surfaces in results.items():
            for surface, actual in surfaces.items():
                with self.subTest(case=case, surface=surface):
                    observations = actual["cycles"][0]["observations"]
                    if surface == "demo" and case in {"missing_manifest", "invalid_manifest"}:
                        self.assertEqual([], observations)
                        self.assertEqual("universe_load_failed", actual["payload"]["universe_error"]["reason"])
                        self.assertEqual([], actual["payload"]["scans"])
                        continue
                    expected = (["data_gate_blocked"] * 2 if case in blocked else
                                ["scan_failed"] * 2 if case in failures else
                                ["normal_no_action"] * 2 if case in {"fresh", "strategy_block"} else
                                ["data_gate_blocked", "ready_for_review"] if case in {"missing_cache", "corrupt_cache"} else
                                ["ready_for_review"] * 2)
                    self.assertEqual(list(SYMBOLS), [row["symbol"] for row in observations])
                    self.assertEqual(expected, [row["outcome"] for row in observations])
                    scanned = [item[1] for item in actual["events"] if item[0] == "scan"]
                    self.assertEqual(len(scanned), len(set(scanned)))
                    self.assertEqual([row["symbol"] for row in observations if row["trust_allowed"]], scanned)
                    for row in observations:
                        self.assertEqual("trading_strict", row["trust_policy_name"])
                        self.assertEqual(["5m", "15m", "1h"], row["effective_intervals"])
                    if case == "fresh":
                        self.assertTrue(all(row["scan_result"]["evaluated_candle_close_ms"] is not None for row in observations))
                    if case == "write_error":
                        self.assertEqual("persist", actual["events"][-1][0])
                        if surface == "service":
                            self.assertEqual("observation_write_failed", actual["health"]["error_code"])
                        else:
                            self.assertIsNone(actual["payload"])
                            self.assertEqual("ObservationCycleInvalidError", actual["exception"][0])


if __name__ == "__main__":
    unittest.main()
