import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.experiments.release_candidate import (
    HistoricalGenerationError,
    HistoricalTrustedGenerationReader,
)
from mu_strategy.market_data.trusted_data.store import TrustedDataStore


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TRACKED_DATA_DIR = REPOSITORY_ROOT / "data" / "live"
TRACKED_RUN_ID = "e702be27d2de4b2d92b12bf01c70d02d"


class HistoricalTrustedGenerationReaderTests(unittest.TestCase):
    def test_reads_explicit_tracked_generation_without_current_pointer_or_refresh(self):
        reader = HistoricalTrustedGenerationReader(data_dir=TRACKED_DATA_DIR)

        with patch.object(TrustedDataStore, "read_manifest", side_effect=AssertionError("current pointer used")):
            with patch(
                "mu_strategy.market_data.trusted_data.refresh.refresh_with_okx_provider",
                side_effect=AssertionError("refresh used"),
            ) as refresh:
                generation = reader.read(run_id=TRACKED_RUN_ID, symbol="MU-USDT-SWAP")

        self.assertEqual(TRACKED_RUN_ID, generation.reference.run_id)
        self.assertEqual(("5m", "15m", "1h"), generation.reference.effective_intervals)
        self.assertEqual(35_257, len(generation.candles_by_interval["5m"]))
        self.assertEqual(11_752, len(generation.candles_by_interval["15m"]))
        self.assertEqual(2_938, len(generation.candles_by_interval["1h"]))
        self.assertEqual("fresh", generation.published_freshness_by_interval["15m"])
        refresh.assert_not_called()

    def test_reader_is_independent_from_current_pointer_contents(self):
        with TemporaryDirectory() as tmp:
            data_dir = _copy_generation(Path(tmp))
            (data_dir / "current.json").write_text('{"schema_version":1,"generation_id":"different"}', encoding="utf-8")

            generation = HistoricalTrustedGenerationReader(data_dir=data_dir).read(
                run_id=TRACKED_RUN_ID,
                symbol="MU-USDT-SWAP",
            )

        self.assertEqual(TRACKED_RUN_ID, generation.reference.run_id)

    def test_reader_rejects_content_hash_mismatch(self):
        with TemporaryDirectory() as tmp:
            data_dir = _copy_generation(Path(tmp))
            csv_path = data_dir / "generations" / TRACKED_RUN_ID / "okx" / "MU-USDT-SWAP" / "15m.csv"
            lines = csv_path.read_text(encoding="utf-8").splitlines()
            header = lines[0].split(",")
            first_row = lines[1].split(",")
            close_index = header.index("close")
            first_row[close_index] = str(float(first_row[close_index]) + 1)
            lines[1] = ",".join(first_row)
            csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(HistoricalGenerationError, "content SHA-256"):
                HistoricalTrustedGenerationReader(data_dir=data_dir).read(
                    run_id=TRACKED_RUN_ID,
                    symbol="MU-USDT-SWAP",
                )

    def test_reader_rejects_manifest_identity_schema_and_source_path_mismatch(self):
        cases = (
            ("run_id", "b" * 32, "run_id"),
            ("schema_version", 2, "schema_version"),
            ("source_file", "../../escape.csv", "source_file"),
        )
        for field_name, value, message in cases:
            with self.subTest(field=field_name):
                with TemporaryDirectory() as tmp:
                    data_dir = _copy_generation(Path(tmp))
                    manifest_path = data_dir / "generations" / TRACKED_RUN_ID / "manifest.json"
                    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if field_name == "source_file":
                        payload["symbols"]["MU-USDT-SWAP"]["intervals"]["15m"][field_name] = value
                    else:
                        payload[field_name] = value
                    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

                    with self.assertRaisesRegex(HistoricalGenerationError, message):
                        HistoricalTrustedGenerationReader(data_dir=data_dir).read(
                            run_id=TRACKED_RUN_ID,
                            symbol="MU-USDT-SWAP",
                        )


def _copy_generation(root: Path) -> Path:
    data_dir = root / "data" / "live"
    target = data_dir / "generations" / TRACKED_RUN_ID
    target.parent.mkdir(parents=True)
    shutil.copytree(TRACKED_DATA_DIR / "generations" / TRACKED_RUN_ID, target)
    return data_dir


if __name__ == "__main__":
    unittest.main()
