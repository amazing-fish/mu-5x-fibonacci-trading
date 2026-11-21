import json
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mu_strategy.canonical import canonical_json, canonical_sha256
from mu_strategy.entry.scanner import EntryScanResult
from mu_strategy.file_locks import FileLockBusyError
from mu_strategy.market_data.trusted_data.contracts import HealthReason
from mu_strategy.models import EntryDecisionCode, EntryDecisionStage, EntryDisposition
from mu_strategy.observations import (
    JsonlObservationRepository,
    ObservationCorruptionError,
    ObservationFailureCode,
    ObservationOutcome,
    ObservationSchemaError,
    ObservationWriteError,
    Stage0Observation,
    Stage0ObservationCycle,
    TrustedObservationReference,
    build_stage0_observation,
    canonical_payload_sha256,
    sanitize_observation_text,
)


class Stage0ObservationContractTests(unittest.TestCase):
    def test_shared_canonical_json_matches_stage0_contract(self):
        payload = {"z": ("é",), "a": 1}

        self.assertEqual('{"a":1,"z":["\\u00e9"]}', canonical_json(payload))
        self.assertEqual(canonical_payload_sha256(payload), canonical_sha256(payload))

    def test_shared_canonical_json_rejects_non_finite_numbers(self):
        with self.assertRaises(ValueError):
            canonical_json({"value": float("nan")})

    def test_classifies_closed_outcomes_from_typed_control_fields(self):
        blocked = _observation(
            trusted=_trusted(allowed=False, reason=HealthReason.MANIFEST_INVALID, run_id=None, hashes=()),
            result=None,
            failure_code=ObservationFailureCode.TRUSTED_DATA_BLOCKED,
        )
        failed = _observation(
            result=None,
            failure_code=ObservationFailureCode.SCANNER_EXCEPTION,
            error_type="RuntimeError",
            error_message="scanner exploded",
        )
        waiting = _observation(result=_scan(EntryDecisionCode.NO_CONFIRMED_FIB_RETEST, reason="wait text"))
        ready = _observation(result=_scan(EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY, reason="ready text"))

        self.assertIs(ObservationOutcome.DATA_GATE_BLOCKED, blocked.outcome)
        self.assertIs(ObservationOutcome.SCAN_FAILED, failed.outcome)
        self.assertIs(ObservationOutcome.NORMAL_NO_ACTION, waiting.outcome)
        self.assertIs(ObservationOutcome.READY_FOR_REVIEW, ready.outcome)

    def test_signal_stage_block_is_normal_no_action_but_input_block_is_data_blocked(self):
        signal_block = _observation(result=_scan(EntryDecisionCode.REGIME_BLOCKED))
        input_block = _observation(result=_scan(EntryDecisionCode.MARKET_DATA_UNAVAILABLE))

        self.assertIs(ObservationOutcome.NORMAL_NO_ACTION, signal_block.outcome)
        self.assertIs(ObservationOutcome.DATA_GATE_BLOCKED, input_block.outcome)

    def test_unknown_decision_code_fails_closed_without_using_action_or_reason(self):
        result = _scan(EntryDecisionCode.UNKNOWN, action="enter", reason="looks ready")

        with self.assertRaisesRegex(ValueError, "decision_code"):
            _observation(result=result)

    def test_allowed_observation_requires_complete_generation_provenance(self):
        with self.assertRaisesRegex(ValueError, "trusted run_id"):
            _trusted(run_id=None, hashes=())

    def test_non_strict_or_inconsistent_trust_identity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "trading_strict"):
            TrustedObservationReference(
                run_id="trusted-run",
                requested_intervals=("15m", "1h"),
                effective_intervals=("5m", "15m", "1h"),
                content_sha256_by_interval=(("5m", "a" * 64), ("15m", "b" * 64), ("1h", "c" * 64)),
                policy_name="observe_only",
                policy_version=1,
                allowed=True,
                reason=HealthReason.OK,
            )
        with self.assertRaisesRegex(ValueError, "reason OK"):
            _trusted(allowed=True, reason=HealthReason.MANIFEST_INVALID)

    def test_fingerprint_is_stable_across_ids_timestamps_and_presentation_text(self):
        first = _observation(
            observation_id="obs-a",
            created_at_ms=100,
            observed_at_ms=90,
            result=_scan(EntryDecisionCode.NO_CONFIRMED_FIB_RETEST, reason="first wording"),
        )
        second = _observation(
            observation_id="obs-b",
            created_at_ms=200,
            observed_at_ms=190,
            result=_scan(EntryDecisionCode.NO_CONFIRMED_FIB_RETEST, reason="second wording"),
        )
        changed_control = _observation(result=_scan(EntryDecisionCode.REGIME_BLOCKED))

        self.assertEqual(first.result_fingerprint, second.result_fingerprint)
        self.assertNotEqual(first.result_fingerprint, changed_control.result_fingerprint)

    def test_sanitizes_secret_values_and_bounds_diagnostic_length(self):
        text = (
            'api_key=abc secret: def passphrase=ghi Authorization: Bearer bearer-token '
            'OKX_SECRET_KEY=okx-secret OKX_PASSPHRASE=okx-pass "OK-ACCESS-KEY": "header-key" '
            "'access_token': 'json-token' cookie=session-value "
            + ("x" * 1000)
        )

        sanitized = sanitize_observation_text(text)

        self.assertNotIn("abc", sanitized)
        self.assertNotIn("def", sanitized)
        self.assertNotIn("ghi", sanitized)
        self.assertNotIn("Bearer-token", sanitized)
        self.assertNotIn("bearer-token", sanitized)
        self.assertNotIn("okx-secret", sanitized)
        self.assertNotIn("okx-pass", sanitized)
        self.assertNotIn("header-key", sanitized)
        self.assertNotIn("json-token", sanitized)
        self.assertNotIn("session-value", sanitized)
        self.assertLessEqual(len(sanitized), 512)

    def test_cycle_round_trip_preserves_typed_fields_and_uses_one_jsonl_record(self):
        cycle = Stage0ObservationCycle(
            cycle_id="cycle-1",
            created_at_ms=123,
            observations=(
                _observation(observation_id="obs-wait"),
                _observation(
                    observation_id="obs-ready",
                    result=_scan(EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY),
                ),
            ),
        )

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            JsonlObservationRepository(path).append_cycle(cycle)

            lines = path.read_text(encoding="utf-8").splitlines()
            restarted = JsonlObservationRepository(path).read_cycles()

        self.assertEqual(1, len(lines))
        self.assertEqual((cycle,), restarted)
        self.assertIs(ObservationOutcome.READY_FOR_REVIEW, restarted[0].observations[1].outcome)
        self.assertIs(EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY, restarted[0].observations[1].decision_code)
        self.assertEqual({"5m", "15m", "1h"}, set(restarted[0].observations[1].content_sha256_by_interval))

    def test_restart_reads_prior_complete_cycles(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            JsonlObservationRepository(path).append_cycle(_cycle("cycle-a", "obs-a"))
            JsonlObservationRepository(path).append_cycle(_cycle("cycle-b", "obs-b"))

            restarted = JsonlObservationRepository(path).read_cycles()

        self.assertEqual(["cycle-a", "cycle-b"], [cycle.cycle_id for cycle in restarted])

    def test_unknown_schema_is_rejected_without_downgrade(self):
        cycle = _cycle("cycle-a", "obs-a")
        payload = cycle.to_dict()
        payload["schema_version"] = 999

        with self.assertRaisesRegex(ObservationSchemaError, "schema_version"):
            Stage0ObservationCycle.from_dict(payload)

    def test_unknown_fields_are_rejected_instead_of_hiding_private_payloads(self):
        payload = _cycle("cycle-a", "obs-a").to_dict()
        payload["api_key"] = "must-not-be-accepted"

        with self.assertRaisesRegex(ObservationSchemaError, "unknown fields"):
            Stage0ObservationCycle.from_dict(payload)

    def test_reader_rejects_allowed_record_with_missing_run_id_even_if_schema_is_current(self):
        payload = _cycle("cycle-a", "obs-a").to_dict()
        payload["observations"][0]["trusted_run_id"] = None

        with self.assertRaisesRegex(ObservationSchemaError, "trusted run_id"):
            Stage0ObservationCycle.from_dict(payload)

    def test_corrupted_trailing_record_fails_closed(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            repository.append_cycle(_cycle("cycle-a", "obs-a"))
            with path.open("ab") as handle:
                handle.write(b'{"schema_version":1,"cycle_id":"partial"')

            with self.assertRaisesRegex(ObservationCorruptionError, "line 2"):
                JsonlObservationRepository(path).read_cycles()

    def test_reader_rechecks_failed_write_marker_after_reading_visible_cycle(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            repository.append_cycle(_cycle("cycle-a", "obs-a"))
            original_from_dict = Stage0ObservationCycle.from_dict

            def parse_then_expose_failed_writer(payload):
                cycle = original_from_dict(payload)
                repository.invalid_marker_path.write_text("failed-cycle", encoding="utf-8")
                return cycle

            with patch.object(Stage0ObservationCycle, "from_dict", side_effect=parse_then_expose_failed_writer):
                with self.assertRaisesRegex(ObservationCorruptionError, "failed-write marker"):
                    repository.read_cycles()

    def test_scan_result_rejects_non_numeric_and_non_finite_fields(self):
        from mu_strategy.observations import Stage0ScanResult

        payload = _observation().scan_result.to_dict()
        cases = (
            ("string", "not-a-number"),
            ("nan", float("nan")),
            ("infinity", float("inf")),
            ("bool", True),
            ("overflow", 10**10000),
        )
        for label, invalid in cases:
            with self.subTest(case=label):
                malformed = dict(payload, last_close=invalid)
                with self.assertRaises((TypeError, ValueError)):
                    Stage0ScanResult.from_dict(malformed)

    def test_fsync_failure_rolls_back_uncommitted_bytes_without_poisoning_prior_cycles(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            first = _cycle("cycle-a", "obs-a")
            repository.append_cycle(first)
            real_fsync = __import__("os").fsync
            calls = []

            def fail_append_then_sync_rollback(fd):
                calls.append(fd)
                if len(calls) == 1:
                    raise OSError("disk failure")
                return real_fsync(fd)

            with patch("mu_strategy.observations.os.fsync", side_effect=fail_append_then_sync_rollback):
                with self.assertRaisesRegex(ObservationWriteError, "disk failure"):
                    repository.append_cycle(_cycle("cycle-b", "obs-b"))

            self.assertEqual(2, len(calls), "rollback must fsync the truncated file before releasing the fence")
            self.assertFalse(repository.invalid_marker_path.exists())
            self.assertEqual((first,), JsonlObservationRepository(path).read_cycles())

    def test_rollback_fsync_failure_quarantines_log_until_repair(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            first = _cycle("cycle-a", "obs-a")
            repository.append_cycle(first)
            with patch("mu_strategy.observations.os.fsync", side_effect=OSError("persistent disk failure")):
                with self.assertRaisesRegex(ObservationWriteError, "persistent disk failure"):
                    repository.append_cycle(_cycle("cycle-b", "obs-b"))

            self.assertTrue(repository.invalid_marker_path.exists())
            with self.assertRaisesRegex(ObservationWriteError, "invalid marker"):
                repository.append_cycle(_cycle("cycle-c", "obs-c"))
            with self.assertRaisesRegex(ObservationCorruptionError, "failed-write marker"):
                repository.read_cycles()

    def test_terminated_writer_before_commit_boundary_is_rejected_by_readers_and_repaired_before_append(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            first = _cycle("cycle-a", "obs-a")
            second = _cycle("cycle-b", "obs-b")
            third = _cycle("cycle-c", "obs-c")
            repository.append_cycle(first)
            _, cursor_a = repository.read_batch()
            committed_log_size = path.stat().st_size
            committed_index_size = repository.index_path.stat().st_size

            # Simulate SIGKILL after writing complete cycle B + index entry before commit advancement.
            with path.open("ab") as handle:
                handle.write((second.to_json() + "\n").encode("utf-8"))
            with repository.index_path.open("ab") as idx_handle:
                idx_handle.write(b"\x00" * 80)

            with self.assertRaisesRegex(ObservationCorruptionError, "uncommitted"):
                repository.read_batch(offset=cursor_a[0], anchor_start=cursor_a[1], anchor_sha256=cursor_a[2])
            with self.assertRaisesRegex(ObservationCorruptionError, "uncommitted"):
                repository.read_cycles()
            with self.assertRaisesRegex(ObservationCorruptionError, "uncommitted"):
                repository.read_tail(limit=5)

            # Supported explicit repair truncates & fsyncs both log and index, keeping cursor_a valid.
            real_fsync = __import__("os").fsync
            sync_fds: list[int] = []
            with patch("mu_strategy.observations.os.fsync", side_effect=lambda fd: (sync_fds.append(fd), real_fsync(fd))[1]):
                repaired = repository.repair_uncommitted_tail()
            self.assertTrue(repaired)
            self.assertEqual(2, len(sync_fds), "repair must fsync both truncated log and index")
            self.assertEqual(committed_log_size, path.stat().st_size)
            self.assertEqual(committed_index_size, repository.index_path.stat().st_size)
            self.assertEqual(((), cursor_a), repository.read_batch(
                offset=cursor_a[0], anchor_start=cursor_a[1], anchor_sha256=cursor_a[2]
            ))
            self.assertEqual((first,), repository.read_cycles())

            # A subsequent crash before commit is also repaired automatically before the next append.
            with path.open("ab") as handle:
                handle.write((second.to_json() + "\n").encode("utf-8"))
            with repository.index_path.open("ab") as idx_handle:
                idx_handle.write(b"\x00" * 80)
            repository.append_cycle(third)
            self.assertEqual((first, third), repository.read_cycles())
            delivered, _ = repository.read_batch(
                offset=cursor_a[0], anchor_start=cursor_a[1], anchor_sha256=cursor_a[2]
            )
            self.assertEqual((third,), delivered)

            # Truncation BEFORE the durable commit boundary must still fail closed.
            path.write_bytes(path.read_bytes()[: committed_log_size - 5])
            with self.assertRaisesRegex(ObservationWriteError, "truncated"):
                repository.repair_uncommitted_tail()
            with self.assertRaisesRegex(ObservationWriteError, "truncated"):
                repository.append_cycle(_cycle("cycle-d", "obs-d"))

    def test_unreadable_newest_commit_slot_refuses_truncation_and_preserves_acknowledged_cycles(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            first = _cycle("cycle-a", "obs-a")
            second = _cycle("cycle-b", "obs-b")
            third = _cycle("cycle-c", "obs-c")
            repository.append_cycle(first)
            repository.append_cycle(second)
            acknowledged_log = path.read_bytes()
            acknowledged_index = repository.index_path.read_bytes()
            commit_bytes = bytearray(repository.commit_path.read_bytes())

            # Damage newest slot (slot 0, seq=2) while leaving older slot 1 (seq=1) valid.
            commit_bytes[0:10] = b"CORRUPTED!"
            repository.commit_path.write_bytes(bytes(commit_bytes))

            older_state, has_unreadable = repository.inspect_commit_slots()
            self.assertEqual(1, older_state.seq)
            self.assertTrue(has_unreadable)

            # Neither repair_uncommitted_tail() nor append_cycle() may delete acknowledged cycle-b!
            with self.assertRaisesRegex(ObservationWriteError, "unreadable slot"):
                repository.repair_uncommitted_tail()
            with self.assertRaisesRegex(ObservationWriteError, "unreadable slot"):
                repository.append_cycle(third)
            self.assertEqual(acknowledged_log, path.read_bytes())
            self.assertEqual(acknowledged_index, repository.index_path.read_bytes())

        # Also verify a torn short write of slot 1 (between 160 and 320 bytes) fails closed without truncating.
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            first = _cycle("cycle-a", "obs-a")
            repository.append_cycle(first)
            acknowledged_log = path.read_bytes()
            acknowledged_index = repository.index_path.read_bytes()
            raw_commit = repository.commit_path.read_bytes()
            repository.commit_path.write_bytes(raw_commit[:200])

            _, has_unreadable = repository.inspect_commit_slots()
            self.assertTrue(has_unreadable)
            with self.assertRaisesRegex(ObservationWriteError, "unreadable slot"):
                repository.repair_uncommitted_tail()
            with self.assertRaisesRegex(ObservationWriteError, "unreadable slot"):
                repository.append_cycle(_cycle("cycle-b", "obs-b"))
            self.assertEqual(acknowledged_log, path.read_bytes())
            self.assertEqual(acknowledged_index, repository.index_path.read_bytes())

    def test_failed_initial_commit_slot_write_recovers_cleanly_while_corrupt_commit_rejected(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            first = _cycle("cycle-a", "obs-a")

            with patch("mu_strategy.observations.os.fsync", side_effect=OSError("initial commit fsync failed")):
                with self.assertRaisesRegex(ObservationWriteError, "initial commit fsync failed"):
                    repository.append_cycle(first)

            self.assertFalse(repository.commit_path.exists())
            self.assertFalse(path.exists())
            self.assertEqual((), repository.read_cycles())

            # Retry after I/O fault clears succeeds cleanly.
            repository.append_cycle(first)
            self.assertEqual((first,), repository.read_cycles())

            # Damaged commit slot on an existing log is still strictly rejected.
            repository.commit_path.write_bytes(b"corrupted-commit-slot")
            with self.assertRaisesRegex(ObservationCorruptionError, "commit boundary"):
                repository.read_commit_state()
            with self.assertRaisesRegex(ObservationCorruptionError, "commit boundary"):
                repository.read_cycles()
            with self.assertRaisesRegex(ObservationWriteError, "commit boundary"):
                repository.append_cycle(_cycle("cycle-b", "obs-b"))

    def test_interrupted_legacy_migration_is_retryable_without_mutating_legacy_log(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            first = _cycle("cycle-a", "obs-a")
            second = _cycle("cycle-b", "obs-b")
            third = _cycle("cycle-c", "obs-c")
            legacy_bytes = (first.to_json() + "\n").encode("utf-8")
            path.write_bytes(legacy_bytes)
            repository = JsonlObservationRepository(path)

            # Fault 1: I/O failure while writing/fsyncing staged index leaves legacy log untouched and retryable.
            with patch("mu_strategy.observations.os.fsync", side_effect=OSError("legacy index fsync failed")):
                with self.assertRaisesRegex(ObservationWriteError, "legacy index fsync failed"):
                    repository.append_cycle(second)
            self.assertEqual(legacy_bytes, path.read_bytes())
            self.assertFalse(repository.index_path.exists())
            self.assertFalse(repository.commit_path.exists())

            # Fault 2: Parent directory MUST be fsynced after writing .commit.staging and BEFORE publishing
            # either final sidecar; crash between publishing .index and .commit stays retryable.
            real_replace = __import__("os").replace
            events: list[tuple[str, str]] = []

            def record_dir_fsync(directory):
                events.append(("dir_fsync", str(directory)))
                self.assertTrue(Path(f"{path}.commit.staging").exists())

            def fail_final_commit_rename(src, dst):
                events.append(("replace", Path(dst).name))
                if Path(dst) == repository.commit_path:
                    raise OSError("final commit rename failed")
                return real_replace(src, dst)

            with patch("mu_strategy.observations._fsync_directory", side_effect=record_dir_fsync), patch(
                "mu_strategy.observations.os.replace", side_effect=fail_final_commit_rename
            ):
                with self.assertRaisesRegex(ObservationWriteError, "final commit rename failed"):
                    repository.append_cycle(second)
            self.assertEqual(
                [("dir_fsync", str(path.parent)), ("replace", repository.index_path.name), ("replace", repository.commit_path.name)],
                events,
            )
            self.assertEqual(legacy_bytes, path.read_bytes())
            self.assertTrue(repository.index_path.exists())
            self.assertFalse(repository.commit_path.exists())
            self.assertTrue(Path(f"{path}.commit.staging").exists())

            # Retry after fault clears completes migration and appends second cycle cleanly.
            repository.append_cycle(second)
            self.assertFalse(Path(f"{path}.commit.staging").exists())
            self.assertTrue(repository.commit_path.exists())
            self.assertEqual((first, second), repository.read_cycles())

            # Symmetric partial state (commit exists, index missing, durable staging marker present) also retries cleanly.
            repository.index_path.unlink()
            Path(f"{path}.commit.staging").write_bytes(b"migrating\n")
            repository.append_cycle(third)
            self.assertFalse(Path(f"{path}.commit.staging").exists())
            self.assertEqual((first, second, third), repository.read_cycles())

            # Externally deleting .commit or .index on an already-migrated repo (no staging marker) still fails closed.
            repository.commit_path.unlink()
            with self.assertRaisesRegex(ObservationWriteError, "missing for non-empty log"):
                repository.append_cycle(_cycle("cycle-d", "obs-d"))

    def test_oversized_cycle_rejected_before_writing_and_limit_boundary_matches_readers(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            first = _cycle("cycle-a", "obs-a")
            second = _cycle("cycle-b", "obs-b")
            repository.append_cycle(first)
            exact_size = len((second.to_json() + "\n").encode("utf-8"))

            with patch("mu_strategy.observations._MAX_RECORD_BYTES", exact_size - 1):
                with self.assertRaisesRegex(ObservationWriteError, "maximum record size"):
                    repository.append_cycle(second)
            self.assertEqual((first,), repository.read_cycles())

            with patch("mu_strategy.observations._MAX_RECORD_BYTES", exact_size):
                repository.append_cycle(second)
                self.assertEqual((first, second), repository.read_batch()[0])
                self.assertEqual((first, second), repository.read_tail(limit=5))

    def test_append_fsyncs_parent_directory_only_on_initial_log_creation(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            with patch.object(repository, "_fsync_parent_directory", create=True) as fsync_parent:
                repository.append_cycle(_cycle("cycle-a", "obs-a"))
                self.assertEqual(1, fsync_parent.call_count)
                self.assertFalse(repository.invalid_marker_path.exists())

                repository.append_cycle(_cycle("cycle-b", "obs-b"))
                self.assertEqual(1, fsync_parent.call_count)
                self.assertFalse(repository.invalid_marker_path.exists())

    def test_first_append_fsyncs_each_new_directory_entry_and_new_log_without_marker(self):
        with TemporaryDirectory() as tmp:
            existing_root = Path(tmp)
            path = existing_root / "data" / "observations" / "stage0.jsonl"
            cycle = _cycle("cycle-a", "obs-a")

            with patch("mu_strategy.observations._fsync_directory") as fsync_directory:
                JsonlObservationRepository(path).append_cycle(cycle)

            flushed_directories = [invocation.args[0] for invocation in fsync_directory.call_args_list]
            self.assertEqual(
                [
                    path.parent.parent,
                    existing_root,
                    path.parent,
                ],
                flushed_directories,
            )
            self.assertFalse(JsonlObservationRepository(path).invalid_marker_path.exists())
            self.assertEqual((cycle,), JsonlObservationRepository(path).read_cycles())

    def test_new_parent_directory_fsync_failure_writes_no_log_and_allows_clean_retry(self):
        with TemporaryDirectory() as tmp:
            existing_root = Path(tmp)
            path = existing_root / "data" / "observations" / "stage0.jsonl"
            repository = JsonlObservationRepository(path)
            flushed_directories = []

            def fail_new_parent_flush(directory):
                flushed_directories.append(directory)
                if directory == path.parent.parent:
                    raise OSError("new parent directory fsync failed")

            with patch("mu_strategy.observations._fsync_directory", side_effect=fail_new_parent_flush):
                with self.assertRaises(ObservationWriteError):
                    repository.append_cycle(_cycle("cycle-a", "obs-a"))

            self.assertEqual([path.parent.parent], flushed_directories)
            self.assertFalse(repository.invalid_marker_path.exists())
            self.assertFalse(path.exists())
            cycle_b = _cycle("cycle-b", "obs-b")
            repository.append_cycle(cycle_b)
            self.assertEqual((cycle_b,), JsonlObservationRepository(path).read_cycles())

    def test_retry_repairs_unsynced_directory_entries_and_concurrent_writer_verifies_ancestors(self):
        # Case 1: Failed ancestor dir fsync leaves durable .dirsync -> fresh repository repairs all entries on retry.
        with TemporaryDirectory() as tmp:
            existing_root = Path(tmp)
            path = existing_root / "data" / "observations" / "stage0.jsonl"

            def fail_once(directory):
                if directory == path.parent.parent:
                    raise OSError("dir fsync failed")

            with patch("mu_strategy.observations._fsync_directory", side_effect=fail_once):
                with self.assertRaises(ObservationWriteError):
                    JsonlObservationRepository(path).append_cycle(_cycle("cycle-a", "obs-a"))

            self.assertTrue(path.parent.exists())
            with patch("mu_strategy.observations._fsync_directory") as retry_fsync:
                cycle_b = _cycle("cycle-b", "obs-b")
                JsonlObservationRepository(path).append_cycle(cycle_b)

            self.assertEqual(
                [path.parent.parent, existing_root, path.parent],
                [call.args[0] for call in retry_fsync.call_args_list],
            )
            self.assertEqual((cycle_b,), JsonlObservationRepository(path).read_cycles())

        # Case 2: Writer A pauses right after staging directories visible; Writer B MUST still sync ancestor entries.
        with TemporaryDirectory() as tmp:
            existing_root = Path(tmp)
            path = existing_root / "data" / "observations" / "stage0.jsonl"
            writer_a = JsonlObservationRepository(path)
            writer_a._ensure_parent_directories_staged()
            self.assertTrue(path.parent.exists())
            self.assertTrue(writer_a.dirsync_path.exists())

            with patch("mu_strategy.observations._fsync_directory") as writer_b_fsync:
                cycle_b = _cycle("cycle-b", "obs-b")
                JsonlObservationRepository(path).append_cycle(cycle_b)

            self.assertEqual(
                [path.parent.parent, existing_root, path.parent],
                [call.args[0] for call in writer_b_fsync.call_args_list],
            )
            self.assertFalse(writer_a.dirsync_path.exists())
            self.assertEqual((cycle_b,), JsonlObservationRepository(path).read_cycles())

        # Case 3: Initial log creation parent-directory fsync fails -> zero records written and retry syncs parent.
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            with patch("mu_strategy.observations._fsync_directory", side_effect=OSError("parent sync failed")):
                with self.assertRaises(ObservationWriteError):
                    repository.append_cycle(_cycle("cycle-a", "obs-a"))
            self.assertFalse(repository.invalid_marker_path.exists())
            self.assertEqual((), repository.read_cycles())
            with patch("mu_strategy.observations._fsync_directory") as retry_fsync:
                cycle_b = _cycle("cycle-b", "obs-b")
                repository.append_cycle(cycle_b)
            self.assertEqual([path.parent], [call.args[0] for call in retry_fsync.call_args_list])
            self.assertEqual((cycle_b,), repository.read_cycles())

    def test_readers_excluded_during_uncommitted_append_and_rollback_while_concurrent_readers_share_lock(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            first = _cycle("cycle-a", "obs-a")
            second = _cycle("cycle-b", "obs-b")
            repository.append_cycle(first)
            _, cursor_after_first = repository.read_batch()

            # Concurrent readers must not block each other.
            with repository.read_fence(wait=False) as locked_1:
                self.assertTrue(locked_1)
                with repository.read_fence(wait=False) as locked_2:
                    self.assertTrue(locked_2)
                with self.assertRaises(FileLockBusyError):
                    with repository.publication_fence(wait=False):
                        self.fail("exclusive writer fence must block while shared reader holds lock")

            real_fsync = __import__("os").fsync
            in_fsync = threading.Event()
            release_fsync = threading.Event()
            fsync_calls = 0
            writer_errors: list[Exception] = []
            reader_results: dict[str, object] = {}

            def paused_failing_fsync(fd):
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 1:
                    in_fsync.set()
                    release_fsync.wait()
                    raise OSError("fsync failed after write")
                return real_fsync(fd)

            def run_writer():
                try:
                    with patch("mu_strategy.observations.os.fsync", side_effect=paused_failing_fsync):
                        repository.append_cycle(second)
                except Exception as exc:  # noqa: BLE001
                    writer_errors.append(exc)

            # Record when each public reader actually reaches read_fence() before releasing writer.
            contending_count = 0
            contending_lock = threading.Lock()
            all_contending = threading.Event()
            original_lock_file = __import__("mu_strategy.file_locks", fromlist=["lock_file"]).lock_file

            def observed_lock_file(stream, **kwargs):
                nonlocal contending_count
                if kwargs.get("shared") and kwargs.get("wait", False):
                    with contending_lock:
                        contending_count += 1
                        if contending_count == 3:
                            all_contending.set()
                return original_lock_file(stream, **kwargs)

            writer = threading.Thread(target=run_writer)
            writer.start()
            try:
                self.assertTrue(in_fsync.wait(timeout=5), "writer did not reach fsync")
                with self.assertRaises(FileLockBusyError):
                    with repository.read_fence(wait=False):
                        self.fail("reader acquired lock while writer was mid-append")

                with patch("mu_strategy.file_locks.lock_file", side_effect=observed_lock_file):
                    t_batch = threading.Thread(
                        target=lambda: reader_results.__setitem__(
                            "batch",
                            repository.read_batch(
                                offset=cursor_after_first[0],
                                anchor_start=cursor_after_first[1],
                                anchor_sha256=cursor_after_first[2],
                            ),
                        )
                    )
                    t_all = threading.Thread(target=lambda: reader_results.__setitem__("all", repository.read_cycles()))
                    t_tail = threading.Thread(target=lambda: reader_results.__setitem__("tail", repository.read_tail(limit=5)))
                    for reader in (t_batch, t_all, t_tail):
                        reader.start()
                    self.assertTrue(all_contending.wait(timeout=5), "all three public readers must contend on read_fence")
                    self.assertTrue(all(reader.is_alive() for reader in (t_batch, t_all, t_tail)))
                    release_fsync.set()
                    for reader in (t_batch, t_all, t_tail):
                        reader.join(timeout=5)
            finally:
                release_fsync.set()
                writer.join(timeout=5)

            self.assertEqual(1, len(writer_errors))
            self.assertIsInstance(writer_errors[0], ObservationWriteError)
            self.assertEqual(((), cursor_after_first), reader_results["batch"])
            self.assertEqual((first,), reader_results["all"])
            self.assertEqual((first,), reader_results["tail"])

    def test_rollback_truncate_failure_quarantines_log_and_blocks_appends_and_reads(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            first = _cycle("cycle-a", "obs-a")
            repository.append_cycle(first)
            original_open = Path.open

            class FailingTruncateHandle:
                def __init__(self, inner):
                    self._inner = inner

                def __enter__(self):
                    self._inner.__enter__()
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return self._inner.__exit__(exc_type, exc, tb)

                def fileno(self):
                    return self._inner.fileno()

                def write(self, data):
                    # Write only half the record then fail to simulate partial write.
                    raw = bytes(data)
                    self._inner.write(raw[: max(1, len(raw) // 2)])
                    raise OSError("partial write failed")

                def truncate(self, size):
                    raise OSError("truncate rollback failed")

            def intercept_open(target, mode="r", *args, **kwargs):
                handle = original_open(target, mode, *args, **kwargs)
                if target == path and "a" in mode:
                    return FailingTruncateHandle(handle)
                return handle

            with patch.object(Path, "open", new=intercept_open):
                with self.assertRaises(ObservationWriteError):
                    repository.append_cycle(_cycle("cycle-b", "obs-b"))

            self.assertTrue(repository.invalid_marker_path.exists())
            with self.assertRaisesRegex(ObservationWriteError, "invalid marker"):
                repository.append_cycle(_cycle("cycle-c", "obs-c"))
            with self.assertRaisesRegex(ObservationCorruptionError, "failed-write marker"):
                repository.read_cycles()
            with self.assertRaisesRegex(ObservationCorruptionError, "failed-write marker"):
                repository.read_batch()
            with self.assertRaisesRegex(ObservationCorruptionError, "failed-write marker"):
                repository.read_tail(limit=5)

    def test_read_tail_with_min_created_at_ms_skips_out_of_order_older_cycles(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            c2000 = Stage0ObservationCycle(
                cycle_id="cycle-2000",
                created_at_ms=2_000,
                observations=(_observation(cycle_id="cycle-2000", observation_id="obs-2000", created_at_ms=2_000),),
            )
            c1000 = Stage0ObservationCycle(
                cycle_id="cycle-1000",
                created_at_ms=1_000,
                observations=(_observation(cycle_id="cycle-1000", observation_id="obs-1000", created_at_ms=1_000),),
            )
            c1800 = Stage0ObservationCycle(
                cycle_id="cycle-1800",
                created_at_ms=1_800,
                observations=(_observation(cycle_id="cycle-1800", observation_id="obs-1800", created_at_ms=1_800),),
            )
            repository.append_cycle(c2000)
            repository.append_cycle(c1000)
            self.assertEqual((c2000,), repository.read_tail(limit=10, min_created_at_ms=1_500))
            repository.append_cycle(c1800)
            self.assertEqual((c2000, c1800), repository.read_tail(limit=10, min_created_at_ms=1_500))

    def test_read_tail_and_iter_tail_cycles_reverse_scan_across_chunk_boundaries(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            self.assertEqual((), repository.read_tail(limit=5))
            cycles = tuple(
                Stage0ObservationCycle(
                    cycle_id=f"cycle-{idx}",
                    created_at_ms=1_000 + idx * 100,
                    observations=(_observation(cycle_id=f"cycle-{idx}", observation_id=f"obs-{idx}", created_at_ms=1_000 + idx * 100),),
                )
                for idx in range(6)
            )
            for item in cycles:
                repository.append_cycle(item)

            for chunk_size in (17, 64, 512, 65_536):
                with self.subTest(chunk_size=chunk_size):
                    self.assertEqual(cycles[-3:], repository.read_tail(limit=3, chunk_size=chunk_size))
                    self.assertEqual(cycles, repository.read_tail(limit=10, chunk_size=chunk_size))
                    self.assertEqual(
                        cycles[3:],
                        repository.read_tail(limit=10, min_created_at_ms=1_300, chunk_size=chunk_size),
                    )
                    self.assertEqual(
                        tuple(reversed(cycles[-2:])),
                        tuple(repository.iter_tail_cycles(max_cycles=2, chunk_size=chunk_size)),
                    )

    def test_read_tail_handles_empty_file_and_fails_closed_on_corruption_or_legacy_marker(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            repository = JsonlObservationRepository(path)
            path.write_bytes(b"")
            self.assertEqual((), repository.read_tail(limit=5))

            repository.append_cycle(_cycle("cycle-a", "obs-a"))
            repository.invalid_marker_path.write_text("legacy", encoding="utf-8")
            with self.assertRaisesRegex(ObservationCorruptionError, "failed-write marker"):
                repository.read_tail(limit=5)
            repository.invalid_marker_path.unlink()

            original = path.read_bytes()
            path.write_bytes(original + b'{"incomplete":true')
            with self.assertRaises(ObservationCorruptionError):
                repository.read_tail(limit=5)

            path.write_bytes(original.replace(b'"schema_version":2', b'"schema_version":2,"schema_version":2', 1))
            with self.assertRaisesRegex(ObservationCorruptionError, "duplicate"):
                repository.read_tail(limit=5)

    def test_reader_rejects_decision_metadata_that_disagrees_with_catalog(self):
        payload = _observation(result=_scan(EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY)).to_dict()
        payload["disposition"] = "wait"
        payload["outcome"] = "normal_no_action"

        with self.assertRaisesRegex(ObservationSchemaError, "decision metadata"):
            Stage0Observation.from_dict(payload)

    def test_successful_scan_evidence_requires_allowed_trusted_data(self):
        ready = _observation(result=_scan(EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY))

        with self.assertRaisesRegex(ValueError, "allowed trusted data"):
            replace(
                ready,
                trust_allowed=False,
                trust_reason=HealthReason.MANIFEST_INVALID,
                outcome=ObservationOutcome.DATA_GATE_BLOCKED,
            )

    def test_data_failure_requires_canonical_input_block_metadata(self):
        blocked = _observation(
            trusted=_trusted(allowed=False, reason=HealthReason.MANIFEST_INVALID, run_id=None, hashes=()),
            result=None,
            failure_code=ObservationFailureCode.TRUSTED_DATA_BLOCKED,
        )

        with self.assertRaisesRegex(ValueError, "data-gate failure decision metadata"):
            replace(
                blocked,
                decision_code=EntryDecisionCode.SECOND_PULLBACK_LIMIT_READY,
                disposition=EntryDisposition.READY,
                decision_stage=EntryDecisionStage.PENDING_ENTRY,
            )

    def test_cycle_rejects_duplicate_observation_ids(self):
        observation = _observation(observation_id="same")

        with self.assertRaisesRegex(ValueError, "observation_id"):
            Stage0ObservationCycle(
                cycle_id="cycle-1",
                created_at_ms=123,
                observations=(observation, observation),
            )


def _cycle(cycle_id: str, observation_id: str) -> Stage0ObservationCycle:
    return Stage0ObservationCycle(
        cycle_id=cycle_id,
        created_at_ms=123,
        observations=(_observation(cycle_id=cycle_id, observation_id=observation_id),),
    )


def _trusted(
    *,
    allowed: bool = True,
    reason: HealthReason = HealthReason.OK,
    run_id: str | None = "trusted-run",
    hashes=(
        ("5m", "a" * 64),
        ("15m", "b" * 64),
        ("1h", "c" * 64),
    ),
) -> TrustedObservationReference:
    return TrustedObservationReference(
        run_id=run_id,
        requested_intervals=("15m", "1h"),
        effective_intervals=("5m", "15m", "1h"),
        content_sha256_by_interval=tuple(hashes),
        policy_name="trading_strict",
        policy_version=1,
        allowed=allowed,
        reason=reason,
    )


def _scan(
    decision_code: EntryDecisionCode = EntryDecisionCode.NO_CONFIRMED_FIB_RETEST,
    *,
    action: str = "wait",
    reason: str = "no current setup",
) -> EntryScanResult:
    return EntryScanResult(
        symbol="BTC-USDT-SWAP",
        action=action,
        reason=reason,
        last_close=100.0,
        regime_1h="green",
        rsi14=55.0,
        macd_hist=0.2,
        macd_hist_prev=0.1,
        fib_level=99.0,
        fib_distance_pct=0.01,
        trigger_price=99.0,
        initial_stop=97.0,
        signal_time_ms=42,
        decision_code=decision_code,
    )


def _observation(
    *,
    observation_id: str = "obs-1",
    cycle_id: str = "cycle-1",
    created_at_ms: int = 123,
    observed_at_ms: int = 120,
    trusted: TrustedObservationReference | None = None,
    result: EntryScanResult | None = None,
    failure_code: ObservationFailureCode | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
):
    return build_stage0_observation(
        observation_id=observation_id,
        cycle_id=cycle_id,
        symbol="BTC-USDT-SWAP",
        created_at_ms=created_at_ms,
        observed_at_ms=observed_at_ms,
        trusted=trusted or _trusted(),
        strategy_name="baseline",
        strategy_config_fingerprint="d" * 64,
        result=result or (_scan() if failure_code is None else None),
        compatibility_source="watchlist",
        provenance="canonical_trusted_generation",
        failure_code=failure_code,
        error_type=error_type,
        error_message=error_message,
    )


if __name__ == "__main__":
    unittest.main()
