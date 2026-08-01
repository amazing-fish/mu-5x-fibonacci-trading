import errno
import json
import unittest
import warnings
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch


class FileSystemDurabilityTests(unittest.TestCase):
    def test_persistence_domains_share_one_directory_sync_implementation(self):
        from mu_strategy import observations
        from mu_strategy.fs_durability import fsync_directory
        from mu_strategy.market_data.trusted_data import store
        from mu_strategy.research import strategy_artifact_publication

        self.assertIs(fsync_directory, observations._fsync_directory)
        self.assertIs(fsync_directory, store._fsync_directory)
        self.assertIs(fsync_directory, strategy_artifact_publication._fsync_directory)

    def test_posix_directory_sync_opens_flushes_and_closes_in_order(self):
        from mu_strategy import fs_durability

        directory = Path("state")
        events = []

        def open_directory(path, flags):
            events.append(("open", path, flags))
            return 41

        def flush_directory(descriptor):
            events.append(("flush", descriptor))

        def close_directory(descriptor):
            events.append(("close", descriptor))

        with patch.object(fs_durability, "_platform_name", return_value="posix"), patch.object(
            fs_durability.os, "open", side_effect=open_directory
        ), patch.object(fs_durability.os, "fsync", side_effect=flush_directory), patch.object(
            fs_durability.os, "close", side_effect=close_directory
        ):
            fs_durability.fsync_directory(directory)

        self.assertEqual(["open", "flush", "close"], [event[0] for event in events])
        self.assertEqual(directory, events[0][1])
        self.assertEqual(41, events[1][1])
        self.assertEqual(41, events[2][1])

    def test_posix_open_failure_is_not_suppressed(self):
        from mu_strategy import fs_durability

        with patch.object(fs_durability, "_platform_name", return_value="posix"), patch.object(
            fs_durability.os, "open", side_effect=OSError("directory open failed")
        ), patch.object(fs_durability.os, "fsync") as fsync, patch.object(
            fs_durability.os, "close"
        ) as close:
            with self.assertRaisesRegex(OSError, "directory open failed"):
                fs_durability.fsync_directory(Path("state"))

        fsync.assert_not_called()
        close.assert_not_called()

    def test_posix_flush_failure_still_closes_and_is_not_suppressed(self):
        from mu_strategy import fs_durability

        with patch.object(fs_durability, "_platform_name", return_value="posix"), patch.object(
            fs_durability.os, "open", return_value=42
        ), patch.object(fs_durability.os, "fsync", side_effect=OSError("directory flush failed")), patch.object(
            fs_durability.os, "close"
        ) as close:
            with self.assertRaisesRegex(OSError, "directory flush failed"):
                fs_durability.fsync_directory(Path("state"))

        close.assert_called_once_with(42)

    def test_posix_flush_failure_remains_primary_when_close_also_fails(self):
        from mu_strategy import fs_durability

        with patch.object(fs_durability, "_platform_name", return_value="posix"), patch.object(
            fs_durability.os, "open", return_value=44
        ), patch.object(
            fs_durability.os,
            "fsync",
            side_effect=OSError("directory flush failed"),
        ), patch.object(
            fs_durability.os,
            "close",
            side_effect=OSError("directory close failed"),
        ):
            with self.assertRaisesRegex(OSError, "directory flush failed"):
                fs_durability.fsync_directory(Path("state"))

    def test_posix_close_failure_is_not_suppressed(self):
        from mu_strategy import fs_durability

        with patch.object(fs_durability, "_platform_name", return_value="posix"), patch.object(
            fs_durability.os, "open", return_value=43
        ), patch.object(fs_durability.os, "fsync"), patch.object(
            fs_durability.os, "close", side_effect=OSError("directory close failed")
        ):
            with self.assertRaisesRegex(OSError, "directory close failed"):
                fs_durability.fsync_directory(Path("state"))

    def test_windows_open_failure_is_not_suppressed(self):
        from mu_strategy import fs_durability

        api = _windows_api(create_result=-1, error_codes=(5,))
        with patch.object(fs_durability, "_platform_name", return_value="nt"), patch.object(
            fs_durability, "_load_windows_directory_api", return_value=api
        ):
            with self.assertRaises(OSError) as raised:
                fs_durability.fsync_directory(Path("state"))

        self.assertEqual(5, raised.exception.errno)
        api.flush_file_buffers.assert_not_called()
        api.close_handle.assert_not_called()

    def test_windows_flush_failure_closes_handle_and_is_not_suppressed(self):
        from mu_strategy import fs_durability

        api = _windows_api(flush_result=False, error_codes=(6,))
        with patch.object(fs_durability, "_platform_name", return_value="nt"), patch.object(
            fs_durability, "_load_windows_directory_api", return_value=api
        ):
            with self.assertRaises(OSError) as raised:
                fs_durability.fsync_directory(Path("state"))

        self.assertEqual(6, raised.exception.errno)
        api.close_handle.assert_called_once_with(99)

    def test_windows_flush_failure_remains_primary_when_close_also_fails(self):
        from mu_strategy import fs_durability

        api = _windows_api(
            flush_result=False,
            close_result=False,
            error_codes=(6, 7),
        )
        with patch.object(fs_durability, "_platform_name", return_value="nt"), patch.object(
            fs_durability, "_load_windows_directory_api", return_value=api
        ):
            with self.assertRaises(OSError) as raised:
                fs_durability.fsync_directory(Path("state"))

        self.assertEqual(6, raised.exception.errno)
        api.close_handle.assert_called_once_with(99)

    def test_windows_close_failure_is_not_suppressed(self):
        from mu_strategy import fs_durability

        api = _windows_api(close_result=False, error_codes=(7,))
        with patch.object(fs_durability, "_platform_name", return_value="nt"), patch.object(
            fs_durability, "_load_windows_directory_api", return_value=api
        ):
            with self.assertRaises(OSError) as raised:
                fs_durability.fsync_directory(Path("state"))

        self.assertEqual(7, raised.exception.errno)

    def test_unsupported_platform_fails_closed(self):
        from mu_strategy import fs_durability

        with patch.object(fs_durability, "_platform_name", return_value="java"), patch.object(
            fs_durability.os, "open"
        ) as open_directory:
            with self.assertRaises(OSError) as raised:
                fs_durability.fsync_directory(Path("state"))

        self.assertEqual(errno.ENOTSUP, raised.exception.errno)
        open_directory.assert_not_called()


class TrustedStoreDurabilityTests(unittest.TestCase):
    def test_run_log_file_sync_failure_is_not_suppressed(self):
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            with patch(
                "mu_strategy.market_data.trusted_data.store.os.fsync",
                side_effect=OSError("file sync failed"),
            ):
                with self.assertRaisesRegex(OSError, "file sync failed"):
                    store.append_run_log({"run_id": "run-a"})

    def test_generation_directory_sync_failure_is_not_suppressed(self):
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore

        with TemporaryDirectory() as tmp:
            store = TrustedDataStore(data_dir=Path(tmp))
            with patch(
                "mu_strategy.fs_durability._platform_name",
                return_value="posix",
            ), patch(
                "mu_strategy.fs_durability.os.open",
                side_effect=OSError("directory open failed"),
            ):
                with self.assertRaisesRegex(OSError, "directory open failed"):
                    store.prepare_generation("run-a")

    def test_current_pointer_directory_sync_failure_is_reported_without_false_failure(self):
        from mu_strategy.market_data.trusted_data.store import TrustedDataStore
        from tests.factories.trusted_publication import write_generation_manifest_and_caches

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_generation_manifest_and_caches(
                data_dir,
                symbol="MU-USDT-SWAP",
                days=1,
                run_id="run-old",
            )
            write_generation_manifest_and_caches(
                data_dir,
                symbol="MU-USDT-SWAP",
                days=1,
                run_id="run-new",
            )
            store = TrustedDataStore(data_dir=data_dir)
            store.replace_current("run-old")

            with patch(
                "mu_strategy.market_data.trusted_data.store._fsync_directory",
                side_effect=OSError("directory sync failed"),
            ), self.assertLogs(
                "mu_strategy.market_data.trusted_data.store",
                level="WARNING",
            ) as captured, warnings.catch_warnings():
                warnings.simplefilter("error")
                result = store.replace_current("run-new")

            self.assertEqual(data_dir / "current.json", result)
            self.assertEqual("run-new", store.read_manifest().generation_id)
            self.assertIn(
                "current_pointer_directory_sync_failed: OSError: directory sync failed",
                "\n".join(captured.output),
            )

    def test_commit_reports_current_pointer_directory_sync_failure_as_warning(self):
        from mu_strategy.market_data.trusted_data.store import GenerationRetentionPolicy, TrustedDataStore
        from tests.factories.trusted_publication import write_generation_manifest_and_caches

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_generation_manifest_and_caches(
                data_dir,
                symbol="MU-USDT-SWAP",
                days=1,
                run_id="run-old",
            )
            manifest = write_generation_manifest_and_caches(
                data_dir,
                symbol="MU-USDT-SWAP",
                days=1,
                run_id="run-new",
            )
            store = TrustedDataStore(
                data_dir=data_dir,
                retention_policy=GenerationRetentionPolicy(keep_recent=1),
            )
            store.replace_current("run-old")

            def fail_current_pointer_directory(directory):
                if Path(directory) == data_dir:
                    raise OSError("directory sync failed")

            with patch(
                "mu_strategy.market_data.trusted_data.store._fsync_directory",
                side_effect=fail_current_pointer_directory,
            ):
                warnings = store.commit_generation_publication(
                    "run-new",
                    manifest,
                    {"run_id": "run-new"},
                )

            self.assertEqual(
                (
                    "current_pointer_directory_sync_failed: OSError: directory sync failed",
                    "generation_reclamation_failed: generations: CurrentPointerDurabilityUnconfirmed: "
                    "reclamation skipped because current pointer directory sync failed",
                ),
                warnings,
            )
            self.assertEqual("run-new", store.read_manifest().generation_id)
            self.assertTrue((data_dir / "generations" / "run-old").is_dir())
            self.assertEqual((), store.last_reclamation_report.removed_ids)
            self.assertEqual(
                "CurrentPointerDurabilityUnconfirmed",
                store.last_reclamation_report.failures[0].error_type,
            )
            run_log = json.loads(store.run_log_path.read_text(encoding="utf-8"))
            self.assertEqual([], run_log["reclamation"]["removed_ids"])
            self.assertEqual(
                "CurrentPointerDurabilityUnconfirmed",
                run_log["reclamation"]["failures"][0]["error_type"],
            )


def _windows_api(
    *,
    create_result=99,
    flush_result=True,
    close_result=True,
    error_codes=(),
):
    return SimpleNamespace(
        create_file=Mock(return_value=create_result),
        flush_file_buffers=Mock(return_value=flush_result),
        close_handle=Mock(return_value=close_result),
        invalid_handle_value=-1,
        get_last_error=Mock(side_effect=error_codes),
        format_error=lambda code: f"Windows error {code}",
    )


if __name__ == "__main__":
    unittest.main()
