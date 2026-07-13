import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from mu_strategy.research.strategy_artifact_publication import (
    StrategyArtifactConflictError,
    StrategyArtifactPublicationError,
    StrategyArtifactRecoveryRequiredError,
    publish_strategy_artifact,
    read_strategy_artifact_text,
    recover_strategy_artifact,
    strategy_artifact_commit_witness_path,
    strategy_artifact_pending_marker_path,
)


class StrategyArtifactPublicationTests(unittest.TestCase):
    def test_publish_flushes_marker_created_directories_and_commit_in_order(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "one" / "two" / "artifact.json"
            flushed_directories: list[Path] = []

            with patch(
                "mu_strategy.research.strategy_artifact_publication._fsync_directory",
                side_effect=lambda directory: flushed_directories.append(directory),
            ):
                publish_strategy_artifact(path, '{"value":1}')

            self.assertEqual('{"value":1}', path.read_text(encoding="utf-8"))
            self.assertFalse(strategy_artifact_pending_marker_path(path).exists())
            self.assertTrue(strategy_artifact_commit_witness_path(path).exists())
            self.assertEqual(
                [
                    path.parent,
                    path.parent.parent,
                    root,
                    path.parent,
                    path.parent,
                    path.parent,
                ],
                flushed_directories,
            )
            self.assertEqual([], list(path.parent.glob(f".{path.name}.*.tmp")))

    def test_identical_republish_is_idempotent_and_different_content_conflicts(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            with patch("mu_strategy.research.strategy_artifact_publication._fsync_directory"):
                publish_strategy_artifact(path, "same")

            flushed_directories: list[Path] = []
            with patch("mu_strategy.research.strategy_artifact_publication.os.link") as link, patch(
                "mu_strategy.research.strategy_artifact_publication._fsync_directory",
                side_effect=lambda directory: flushed_directories.append(directory),
            ):
                publish_strategy_artifact(path, "same")
            link.assert_not_called()
            self.assertEqual([path.parent], flushed_directories)

            with patch("mu_strategy.research.strategy_artifact_publication._fsync_directory"):
                with self.assertRaises(StrategyArtifactConflictError):
                    publish_strategy_artifact(path, "different")
            self.assertEqual("same", path.read_text(encoding="utf-8"))

    def test_concurrent_conflicting_final_creation_is_never_overwritten(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            marker = strategy_artifact_pending_marker_path(path)
            original_link = os.link

            def create_conflict_before_install(source: Path, destination: Path) -> None:
                Path(destination).write_text("conflict", encoding="utf-8")
                original_link(source, destination)

            with patch(
                "mu_strategy.research.strategy_artifact_publication.os.link",
                side_effect=create_conflict_before_install,
            ), patch("mu_strategy.research.strategy_artifact_publication._fsync_directory"):
                with self.assertRaises(StrategyArtifactConflictError):
                    publish_strategy_artifact(path, "expected")

            self.assertEqual("conflict", path.read_text(encoding="utf-8"))
            self.assertTrue(marker.exists())

    def test_short_write_fails_before_publication(self):
        handle = Mock()
        handle.__enter__ = Mock(return_value=handle)
        handle.__exit__ = Mock(return_value=False)
        handle.write.return_value = 2

        with TemporaryDirectory() as tmp, patch.object(Path, "open", return_value=handle):
            path = Path(tmp) / "artifact.json"
            with self.assertRaises(StrategyArtifactPublicationError) as raised:
                publish_strategy_artifact(path, "payload")

            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertIn("short strategy artifact write", str(raised.exception.__cause__))
            self.assertFalse(path.exists())
            self.assertFalse(strategy_artifact_pending_marker_path(path).exists())
        handle.flush.assert_not_called()

    def test_temp_and_marker_write_failures_are_typed_and_do_not_publish(self):
        from mu_strategy.research import strategy_artifact_publication as publication

        original_write = publication._write_bytes_durably
        for failed_stage in ("temp", "marker"):
            with self.subTest(failed_stage=failed_stage), TemporaryDirectory() as tmp:
                path = Path(tmp) / "artifact.json"
                marker = strategy_artifact_pending_marker_path(path)

                def fail_selected_write(
                    subject: Path,
                    content: bytes,
                    *,
                    exclusive: bool,
                ) -> None:
                    is_marker = subject == marker
                    if (failed_stage == "marker") == is_marker:
                        raise OSError(f"{failed_stage} write failed")
                    original_write(subject, content, exclusive=exclusive)

                with patch.object(
                    publication,
                    "_write_bytes_durably",
                    side_effect=fail_selected_write,
                ), patch.object(publication, "_fsync_directory"):
                    with self.assertRaises(StrategyArtifactPublicationError):
                        publish_strategy_artifact(path, "payload")

                self.assertFalse(path.exists())
                self.assertFalse(marker.exists())

    def test_file_fsync_failure_leaves_no_final_or_pending_marker(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            with patch(
                "mu_strategy.research.strategy_artifact_publication.os.fsync",
                side_effect=OSError("file fsync failed"),
            ), patch("mu_strategy.research.strategy_artifact_publication._fsync_directory"):
                with self.assertRaises(StrategyArtifactPublicationError):
                    publish_strategy_artifact(path, "payload")

            self.assertFalse(path.exists())
            self.assertFalse(strategy_artifact_pending_marker_path(path).exists())

    def test_atomic_install_failure_retains_pending_marker_and_requires_recovery(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            marker = strategy_artifact_pending_marker_path(path)
            with patch(
                "mu_strategy.research.strategy_artifact_publication.os.link",
                side_effect=OSError("install failed"),
            ), patch("mu_strategy.research.strategy_artifact_publication._fsync_directory"):
                with self.assertRaises(StrategyArtifactPublicationError):
                    publish_strategy_artifact(path, "payload")

            self.assertFalse(path.exists())
            self.assertTrue(marker.exists())
            self.assertFalse(strategy_artifact_commit_witness_path(path).exists())
            with self.assertRaises(StrategyArtifactRecoveryRequiredError):
                publish_strategy_artifact(path, "payload")
            with self.assertRaises(StrategyArtifactRecoveryRequiredError):
                read_strategy_artifact_text(path)

            with patch("mu_strategy.research.strategy_artifact_publication._fsync_directory"):
                recover_strategy_artifact(path, "payload")
            self.assertEqual("payload", read_strategy_artifact_text(path))
            self.assertFalse(marker.exists())
            self.assertTrue(strategy_artifact_commit_witness_path(path).exists())

    def test_commit_witness_link_failure_leaves_pending_state_fail_closed(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            marker = strategy_artifact_pending_marker_path(path)
            witness = strategy_artifact_commit_witness_path(path)
            original_link = os.link
            link_calls = 0

            def fail_witness_link(source: Path, destination: Path) -> None:
                nonlocal link_calls
                link_calls += 1
                if link_calls == 2:
                    raise OSError("commit witness link failed")
                original_link(source, destination)

            with patch(
                "mu_strategy.research.strategy_artifact_publication.os.link",
                side_effect=fail_witness_link,
            ), patch("mu_strategy.research.strategy_artifact_publication._fsync_directory"):
                with self.assertRaises(StrategyArtifactPublicationError):
                    publish_strategy_artifact(path, "payload")

            self.assertTrue(path.exists())
            self.assertTrue(marker.exists())
            self.assertFalse(witness.exists())
            with self.assertRaises(StrategyArtifactRecoveryRequiredError):
                read_strategy_artifact_text(path)

    def test_commit_witness_directory_failure_rolls_back_to_pending_state(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            marker = strategy_artifact_pending_marker_path(path)
            witness = strategy_artifact_commit_witness_path(path)
            directory_fsync_calls = 0

            def fail_witness_directory_flush(_directory: Path) -> None:
                nonlocal directory_fsync_calls
                directory_fsync_calls += 1
                if directory_fsync_calls == 3:
                    raise OSError("commit witness directory fsync failed")

            with patch(
                "mu_strategy.research.strategy_artifact_publication._fsync_directory",
                side_effect=fail_witness_directory_flush,
            ):
                with self.assertRaises(StrategyArtifactPublicationError):
                    publish_strategy_artifact(path, "payload")

            self.assertTrue(path.exists())
            self.assertTrue(marker.exists())
            self.assertFalse(witness.exists())
            with self.assertRaises(StrategyArtifactRecoveryRequiredError):
                read_strategy_artifact_text(path)

    def test_reader_blocks_while_witness_entry_is_not_yet_durable(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            directory_fsync_calls = 0
            observed_pending_witness_window = False

            def inspect_before_witness_directory_flush(_directory: Path) -> None:
                nonlocal directory_fsync_calls, observed_pending_witness_window
                directory_fsync_calls += 1
                if directory_fsync_calls == 3:
                    observed_pending_witness_window = True
                    self.assertTrue(strategy_artifact_pending_marker_path(path).exists())
                    self.assertTrue(strategy_artifact_commit_witness_path(path).exists())
                    with self.assertRaises(StrategyArtifactRecoveryRequiredError):
                        read_strategy_artifact_text(path)

            with patch(
                "mu_strategy.research.strategy_artifact_publication._fsync_directory",
                side_effect=inspect_before_witness_directory_flush,
            ):
                publish_strategy_artifact(path, "payload")

            self.assertTrue(observed_pending_witness_window)
            self.assertEqual("payload", read_strategy_artifact_text(path))

    def test_witness_flush_and_rollback_double_failure_remains_pending(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            marker = strategy_artifact_pending_marker_path(path)
            witness = strategy_artifact_commit_witness_path(path)
            original_unlink = Path.unlink
            directory_fsync_calls = 0

            def fail_witness_flush(_directory: Path) -> None:
                nonlocal directory_fsync_calls
                directory_fsync_calls += 1
                if directory_fsync_calls == 3:
                    raise OSError("commit witness directory fsync failed")

            def fail_witness_rollback(subject: Path, *args, **kwargs):
                if subject == witness:
                    raise OSError("commit witness rollback failed")
                return original_unlink(subject, *args, **kwargs)

            with patch(
                "mu_strategy.research.strategy_artifact_publication._fsync_directory",
                side_effect=fail_witness_flush,
            ), patch.object(Path, "unlink", new=fail_witness_rollback):
                with self.assertRaises(StrategyArtifactPublicationError):
                    publish_strategy_artifact(path, "payload")

            self.assertTrue(path.exists())
            self.assertTrue(marker.exists())
            self.assertTrue(witness.exists())
            with self.assertRaises(StrategyArtifactRecoveryRequiredError):
                read_strategy_artifact_text(path)

            with patch("mu_strategy.research.strategy_artifact_publication._fsync_directory"):
                recover_strategy_artifact(path, "payload")
            self.assertEqual("payload", read_strategy_artifact_text(path))

    def test_pending_publication_is_recoverable_by_a_fresh_process(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            with patch(
                "mu_strategy.research.strategy_artifact_publication.os.link",
                side_effect=OSError("install failed"),
            ), patch("mu_strategy.research.strategy_artifact_publication._fsync_directory"):
                with self.assertRaises(StrategyArtifactPublicationError):
                    publish_strategy_artifact(path, "payload")

            script = "\n".join(
                (
                    "import sys",
                    "from pathlib import Path",
                    "from mu_strategy.research.strategy_artifact_publication import "
                    "recover_strategy_artifact, read_strategy_artifact_text",
                    "path = Path(sys.argv[1])",
                    "recover_strategy_artifact(path, 'payload')",
                    "raise SystemExit(0 if read_strategy_artifact_text(path) == 'payload' else 1)",
                )
            )
            completed = subprocess.run(
                (sys.executable, "-c", script, str(path)),
                cwd=Path(__file__).resolve().parents[1],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertFalse(strategy_artifact_pending_marker_path(path).exists())

    def test_marker_fsync_failure_leaves_recoverable_pending_state(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            fsync_calls = 0

            def fail_marker_fsync(_descriptor: int) -> None:
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 2:
                    raise OSError("marker fsync failed")

            with patch(
                "mu_strategy.research.strategy_artifact_publication.os.fsync",
                side_effect=fail_marker_fsync,
            ), patch("mu_strategy.research.strategy_artifact_publication._fsync_directory"):
                with self.assertRaises(StrategyArtifactPublicationError):
                    publish_strategy_artifact(path, "payload")

            self.assertFalse(path.exists())
            self.assertTrue(strategy_artifact_pending_marker_path(path).exists())
            with patch("mu_strategy.research.strategy_artifact_publication._fsync_directory"):
                recover_strategy_artifact(path, "payload")
            self.assertEqual("payload", read_strategy_artifact_text(path))

    def test_new_parent_fsync_failure_is_recovered_using_marker_lineage(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "one" / "two" / "artifact.json"
            directory_fsync_calls = 0

            def fail_first_created_parent_flush(_directory: Path) -> None:
                nonlocal directory_fsync_calls
                directory_fsync_calls += 1
                if directory_fsync_calls == 2:
                    raise OSError("new parent fsync failed")

            with patch(
                "mu_strategy.research.strategy_artifact_publication._fsync_directory",
                side_effect=fail_first_created_parent_flush,
            ):
                with self.assertRaises(StrategyArtifactPublicationError):
                    publish_strategy_artifact(path, "payload")

            self.assertFalse(path.exists())
            self.assertTrue(strategy_artifact_pending_marker_path(path).exists())
            recovered_flushes: list[Path] = []
            with patch(
                "mu_strategy.research.strategy_artifact_publication._fsync_directory",
                side_effect=lambda directory: recovered_flushes.append(directory),
            ):
                recover_strategy_artifact(path, "payload")
            self.assertEqual(
                [
                    path.parent,
                    path.parent.parent,
                    root,
                    path.parent,
                    path.parent,
                    path.parent,
                ],
                recovered_flushes,
            )
            self.assertEqual("payload", read_strategy_artifact_text(path))

    def test_final_directory_fsync_failure_keeps_visible_final_fail_closed_until_recovery(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            marker = strategy_artifact_pending_marker_path(path)
            calls = 0

            def fail_second_directory_flush(_directory: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("final directory fsync failed")

            with patch(
                "mu_strategy.research.strategy_artifact_publication._fsync_directory",
                side_effect=fail_second_directory_flush,
            ):
                with self.assertRaises(StrategyArtifactPublicationError):
                    publish_strategy_artifact(path, "payload")

            self.assertTrue(path.exists())
            self.assertTrue(marker.exists())
            with self.assertRaises(StrategyArtifactRecoveryRequiredError):
                read_strategy_artifact_text(path)

            recovered_files: list[Path] = []
            recovered_directories: list[Path] = []
            with patch(
                "mu_strategy.research.strategy_artifact_publication._fsync_file",
                side_effect=lambda subject: recovered_files.append(subject),
            ), patch(
                "mu_strategy.research.strategy_artifact_publication._fsync_directory",
                side_effect=lambda directory: recovered_directories.append(directory),
            ):
                recover_strategy_artifact(path, "payload")
            self.assertEqual([marker, path], recovered_files)
            self.assertEqual(
                [path.parent, path.parent, path.parent, path.parent],
                recovered_directories,
            )
            self.assertEqual("payload", read_strategy_artifact_text(path))
            self.assertFalse(marker.exists())

    def test_pending_cleanup_fsync_failure_requires_explicit_recovery(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            marker = strategy_artifact_pending_marker_path(path)
            witness = strategy_artifact_commit_witness_path(path)
            calls = 0

            def fail_pending_cleanup_flush(_directory: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 4:
                    raise OSError("pending cleanup fsync failed")

            with patch(
                "mu_strategy.research.strategy_artifact_publication._fsync_directory",
                side_effect=fail_pending_cleanup_flush,
            ):
                with self.assertRaises(StrategyArtifactPublicationError):
                    publish_strategy_artifact(path, "payload")

            self.assertTrue(path.exists())
            self.assertTrue(witness.exists())
            self.assertTrue(marker.exists())
            with self.assertRaises(StrategyArtifactRecoveryRequiredError):
                read_strategy_artifact_text(path)
            with patch("mu_strategy.research.strategy_artifact_publication._fsync_directory"):
                recover_strategy_artifact(path, "payload")
            self.assertEqual("payload", read_strategy_artifact_text(path))

    def test_cleanup_flush_and_restore_link_double_failure_keeps_pending_barrier(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            marker = strategy_artifact_pending_marker_path(path)
            witness = strategy_artifact_commit_witness_path(path)
            original_link = os.link
            directory_fsync_calls = 0

            def fail_pending_removal_flush(_directory: Path) -> None:
                nonlocal directory_fsync_calls
                directory_fsync_calls += 1
                if directory_fsync_calls == 4:
                    raise OSError("pending removal fsync failed")

            def fail_restore_link(source: Path, destination: Path) -> None:
                if Path(source) == witness and Path(destination) == marker:
                    raise OSError("pending restore link failed")
                original_link(source, destination)

            with patch(
                "mu_strategy.research.strategy_artifact_publication._fsync_directory",
                side_effect=fail_pending_removal_flush,
            ), patch(
                "mu_strategy.research.strategy_artifact_publication.os.link",
                side_effect=fail_restore_link,
            ):
                with self.assertRaises(StrategyArtifactPublicationError):
                    publish_strategy_artifact(path, "payload")

            self.assertTrue(path.exists())
            self.assertTrue(witness.exists())
            self.assertTrue(marker.exists())
            with self.assertRaises(StrategyArtifactRecoveryRequiredError):
                read_strategy_artifact_text(path)

            with patch("mu_strategy.research.strategy_artifact_publication._fsync_directory"):
                recover_strategy_artifact(path, "payload")
            self.assertEqual("payload", read_strategy_artifact_text(path))

    def test_failed_barrier_recreation_can_confirm_durable_removal(self):
        from mu_strategy.research import strategy_artifact_publication as publication

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            marker = strategy_artifact_pending_marker_path(path)
            witness = strategy_artifact_commit_witness_path(path)
            original_link = os.link
            original_write = publication._write_bytes_durably
            directory_fsync_calls = 0

            def fail_initial_pending_removal_flush(_directory: Path) -> None:
                nonlocal directory_fsync_calls
                directory_fsync_calls += 1
                if directory_fsync_calls == 4:
                    raise OSError("pending removal fsync failed")

            def fail_restore_link(source: Path, destination: Path) -> None:
                if Path(source) == witness and Path(destination) == marker:
                    raise OSError("pending restore link failed")
                original_link(source, destination)

            def fail_restore_write(
                subject: Path,
                content: bytes,
                *,
                exclusive: bool,
            ) -> None:
                if subject == marker and witness.exists():
                    raise OSError("pending restore write failed")
                original_write(subject, content, exclusive=exclusive)

            with patch.object(
                publication,
                "_fsync_directory",
                side_effect=fail_initial_pending_removal_flush,
            ), patch.object(
                publication.os,
                "link",
                side_effect=fail_restore_link,
            ), patch.object(
                publication,
                "_write_bytes_durably",
                side_effect=fail_restore_write,
            ):
                publish_strategy_artifact(path, "payload")

            self.assertEqual(5, directory_fsync_calls)
            self.assertTrue(path.exists())
            self.assertFalse(marker.exists())
            self.assertTrue(witness.exists())
            self.assertEqual("payload", read_strategy_artifact_text(path))

    def test_failed_barrier_recreation_and_deletion_proof_returns_committed_state(self):
        from mu_strategy.research import strategy_artifact_publication as publication

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            marker = strategy_artifact_pending_marker_path(path)
            witness = strategy_artifact_commit_witness_path(path)
            original_link = os.link
            original_write = publication._write_bytes_durably
            directory_fsync_calls = 0

            def fail_pending_removal_flush_and_proof(_directory: Path) -> None:
                nonlocal directory_fsync_calls
                directory_fsync_calls += 1
                if directory_fsync_calls in {4, 5}:
                    raise OSError("pending removal durability is unavailable")

            def fail_restore_link(source: Path, destination: Path) -> None:
                if Path(source) == witness and Path(destination) == marker:
                    raise OSError("pending restore link failed")
                original_link(source, destination)

            def fail_restore_write(
                subject: Path,
                content: bytes,
                *,
                exclusive: bool,
            ) -> None:
                if subject == marker and witness.exists():
                    raise OSError("pending restore write failed")
                original_write(subject, content, exclusive=exclusive)

            with patch.object(
                publication,
                "_fsync_directory",
                side_effect=fail_pending_removal_flush_and_proof,
            ), patch.object(
                publication.os,
                "link",
                side_effect=fail_restore_link,
            ), patch.object(
                publication,
                "_write_bytes_durably",
                side_effect=fail_restore_write,
            ):
                publish_strategy_artifact(path, "payload")

            self.assertEqual(5, directory_fsync_calls)
            self.assertTrue(path.exists())
            self.assertFalse(marker.exists())
            self.assertTrue(witness.exists())
            self.assertEqual("payload", read_strategy_artifact_text(path))

    def test_pending_unlink_failure_requires_explicit_recovery(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            marker = strategy_artifact_pending_marker_path(path)
            witness = strategy_artifact_commit_witness_path(path)
            original_unlink = Path.unlink

            def fail_pending_unlink(subject: Path, *args, **kwargs):
                if subject == marker:
                    raise OSError("pending unlink failed")
                return original_unlink(subject, *args, **kwargs)

            with patch.object(Path, "unlink", new=fail_pending_unlink), patch(
                "mu_strategy.research.strategy_artifact_publication._fsync_directory"
            ):
                with self.assertRaises(StrategyArtifactPublicationError):
                    publish_strategy_artifact(path, "payload")

            self.assertTrue(path.exists())
            self.assertTrue(marker.exists())
            self.assertTrue(witness.exists())
            with self.assertRaises(StrategyArtifactRecoveryRequiredError):
                read_strategy_artifact_text(path)
            with patch("mu_strategy.research.strategy_artifact_publication._fsync_directory"):
                recover_strategy_artifact(path, "payload")
            self.assertEqual("payload", read_strategy_artifact_text(path))

    def test_recovery_rejects_malformed_or_mismatched_pending_state(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            marker = strategy_artifact_pending_marker_path(path)

            marker.write_text("not-json", encoding="utf-8")
            with self.assertRaises(StrategyArtifactRecoveryRequiredError):
                recover_strategy_artifact(path, "payload")
            self.assertTrue(marker.exists())

            marker.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "content_sha256": "0" * 64,
                        "created_parent_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(StrategyArtifactConflictError):
                recover_strategy_artifact(path, "payload")
            self.assertTrue(marker.exists())

    def test_recovery_rejects_a_conflicting_visible_final(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            marker = strategy_artifact_pending_marker_path(path)
            with patch(
                "mu_strategy.research.strategy_artifact_publication.os.link",
                side_effect=OSError("install failed"),
            ), patch("mu_strategy.research.strategy_artifact_publication._fsync_directory"):
                with self.assertRaises(StrategyArtifactPublicationError):
                    publish_strategy_artifact(path, "expected")

            path.write_text("conflict", encoding="utf-8")
            with self.assertRaises(StrategyArtifactConflictError):
                recover_strategy_artifact(path, "expected")
            self.assertEqual("conflict", path.read_text(encoding="utf-8"))
            self.assertTrue(marker.exists())

    def test_corrupt_or_mismatched_commit_witness_fails_closed(self):
        for witness_text in (
            "not-json",
            json.dumps(
                {
                    "schema_version": 1,
                    "content_sha256": "0" * 64,
                    "created_parent_count": 0,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ):
            with self.subTest(witness_text=witness_text), TemporaryDirectory() as tmp:
                path = Path(tmp) / "artifact.json"
                path.write_text("payload", encoding="utf-8")
                strategy_artifact_commit_witness_path(path).write_text(
                    witness_text,
                    encoding="utf-8",
                )

                with self.assertRaises(StrategyArtifactPublicationError):
                    read_strategy_artifact_text(path)

    def test_dangling_pending_and_witness_entries_fail_closed(self):
        for sidecar_factory in (
            strategy_artifact_pending_marker_path,
            strategy_artifact_commit_witness_path,
        ):
            with self.subTest(sidecar=sidecar_factory.__name__), TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "artifact.json"
                path.write_text("payload", encoding="utf-8")
                sidecar = sidecar_factory(path)
                try:
                    sidecar.symlink_to(root / "missing-publication-record")
                except (NotImplementedError, OSError) as exc:
                    self.skipTest(f"sidecar symlinks are unavailable: {exc}")

                with self.assertRaises(StrategyArtifactRecoveryRequiredError):
                    read_strategy_artifact_text(path)

    def test_sidecar_directory_entries_use_lstat_not_target_existence(self):
        original_lstat = Path.lstat

        for sidecar_factory in (
            strategy_artifact_pending_marker_path,
            strategy_artifact_commit_witness_path,
        ):
            with self.subTest(sidecar=sidecar_factory.__name__), TemporaryDirectory() as tmp:
                path = Path(tmp) / "artifact.json"
                path.write_text("payload", encoding="utf-8")
                sidecar = sidecar_factory(path)

                def report_dangling_symlink(subject: Path):
                    if subject == sidecar:
                        return os.stat_result((stat.S_IFLNK | 0o777,) + (0,) * 9)
                    return original_lstat(subject)

                with patch.object(Path, "lstat", new=report_dangling_symlink):
                    with self.assertRaises(StrategyArtifactRecoveryRequiredError):
                        read_strategy_artifact_text(path)

    def test_valid_record_symlinks_are_not_accepted_as_sidecars(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "artifact.json"
            with patch("mu_strategy.research.strategy_artifact_publication._fsync_directory"):
                publish_strategy_artifact(path, "payload")

            witness = strategy_artifact_commit_witness_path(path)
            record_target = root / "publication-record.json"
            record_target.write_bytes(witness.read_bytes())

            witness.unlink()
            try:
                witness.symlink_to(record_target)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"sidecar symlinks are unavailable: {exc}")
            with self.assertRaises(StrategyArtifactRecoveryRequiredError):
                read_strategy_artifact_text(path)

            witness.unlink()
            witness.write_bytes(record_target.read_bytes())
            marker = strategy_artifact_pending_marker_path(path)
            marker.symlink_to(record_target)
            with self.assertRaises(StrategyArtifactRecoveryRequiredError):
                recover_strategy_artifact(path, "payload")

    def test_lstat_rejects_valid_sidecar_content_behind_symlink_metadata(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            with patch("mu_strategy.research.strategy_artifact_publication._fsync_directory"):
                publish_strategy_artifact(path, "payload")

            witness = strategy_artifact_commit_witness_path(path)
            original_lstat = Path.lstat

            def report_witness_symlink(subject: Path):
                if subject == witness:
                    return os.stat_result((stat.S_IFLNK | 0o777,) + (0,) * 9)
                return original_lstat(subject)

            with patch.object(Path, "lstat", new=report_witness_symlink):
                with self.assertRaises(StrategyArtifactRecoveryRequiredError):
                    read_strategy_artifact_text(path)

    def test_read_checks_pending_marker_both_before_and_after_read(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            path.write_text("payload", encoding="utf-8")
            marker = strategy_artifact_pending_marker_path(path)

            original_read_text = Path.read_text

            def create_marker_during_read(subject: Path, *args, **kwargs):
                text = original_read_text(subject, *args, **kwargs)
                marker.write_text("pending", encoding="utf-8")
                return text

            with patch.object(Path, "read_text", new=create_marker_during_read):
                with self.assertRaises(StrategyArtifactRecoveryRequiredError):
                    read_strategy_artifact_text(path)

    def test_unrelated_temporary_files_are_never_read_as_artifacts(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            path.write_text("committed", encoding="utf-8")
            path.with_name(f".{path.name}.leftover.tmp").write_text("partial", encoding="utf-8")

            self.assertEqual("committed", read_strategy_artifact_text(path))

    def test_publication_has_no_trusted_data_private_api_or_broker_mutation_side_effects(self):
        with TemporaryDirectory() as tmp, patch(
            "mu_strategy.market_data.trusted_data.store.TrustedDataStore.commit_generation_publication"
        ) as publish_generation, patch(
            "mu_strategy.market_data.trusted_data.store.TrustedDataStore.replace_current"
        ) as replace_current, patch(
            "mu_strategy.live.okx.OKXRestClient.get_balance"
        ) as private_read, patch(
            "mu_strategy.live.okx.OKXRestClient.set_leverage"
        ) as leverage, patch(
            "mu_strategy.live.okx.OKXRestClient.place_demo_order"
        ) as submit, patch(
            "mu_strategy.live.okx.OKXRestClient.cancel_order"
        ) as cancel, patch(
            "mu_strategy.research.strategy_artifact_publication._fsync_directory"
        ):
            publish_strategy_artifact(Path(tmp) / "artifact.json", "payload")

        for prohibited in (
            publish_generation,
            replace_current,
            private_read,
            leverage,
            submit,
            cancel,
        ):
            prohibited.assert_not_called()


if __name__ == "__main__":
    unittest.main()
