from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any


_PUBLICATION_RECORD_SCHEMA_VERSION = 1
_PUBLICATION_RECORD_FIELDS = {
    "schema_version",
    "content_sha256",
    "created_parent_count",
}


class StrategyArtifactPublicationError(RuntimeError):
    """A strategy artifact could not reach a durable committed state."""


class StrategyArtifactConflictError(StrategyArtifactPublicationError):
    """An immutable artifact path is already bound to different bytes."""


class StrategyArtifactRecoveryRequiredError(StrategyArtifactPublicationError):
    """A pending publication makes the artifact state ambiguous."""


def strategy_artifact_pending_marker_path(path: Path) -> Path:
    artifact_path = Path(path)
    return artifact_path.with_name(f".{artifact_path.name}.publication-pending")


def strategy_artifact_commit_witness_path(path: Path) -> Path:
    artifact_path = Path(path)
    return artifact_path.with_name(f".{artifact_path.name}.publication-committed")


def publish_strategy_artifact(
    path: Path,
    text: str,
    *,
    durability_anchor: Path,
) -> None:
    """Publish immutable bytes, creating parents only beneath a durable anchor."""

    artifact_path = Path(path)
    resolved_anchor = _resolve_durability_anchor(artifact_path, durability_anchor)
    encoded = text.encode("utf-8")
    marker_path = strategy_artifact_pending_marker_path(artifact_path)
    witness_path = strategy_artifact_commit_witness_path(artifact_path)
    temporary_path: Path | None = None

    try:
        created_directories = _create_parent_directories(artifact_path.parent)
        if _publication_record_exists(witness_path, label="commit witness"):
            _require_committed_artifact(
                artifact_path,
                expected=encoded,
                marker_path=marker_path,
                witness_path=witness_path,
            )
            if _publication_record_exists(marker_path, label="pending marker"):
                raise StrategyArtifactRecoveryRequiredError(
                    "strategy artifact has an unresolved pending publication"
                )
            _fsync_publication_record(witness_path, label="commit witness")
            _require_identical_artifact(artifact_path, encoded, fsync=True)
            _fsync_directory(artifact_path.parent)
            _fsync_directory_entries_to_anchor(artifact_path.parent, resolved_anchor)
            _require_committed_artifact(
                artifact_path,
                expected=encoded,
                marker_path=marker_path,
                witness_path=witness_path,
            )
            if _publication_record_exists(marker_path, label="pending marker"):
                raise StrategyArtifactRecoveryRequiredError(
                    "strategy artifact has an unresolved pending publication"
                )
            return
        if _publication_record_exists(marker_path, label="pending marker"):
            raise StrategyArtifactRecoveryRequiredError(
                "strategy artifact has an unresolved pending publication"
            )
        if _path_entry_exists(artifact_path):
            _require_identical_artifact(artifact_path, encoded, fsync=True)
            _fsync_directory(artifact_path.parent)
            _fsync_directory_entries_to_anchor(artifact_path.parent, resolved_anchor)
            return

        temporary_path = artifact_path.with_name(
            f".{artifact_path.name}.{uuid.uuid4().hex}.tmp"
        )
        _write_bytes_durably(temporary_path, encoded, exclusive=True)

        marker_bytes = _publication_record_bytes(
            encoded,
            created_parent_count=len(created_directories),
        )
        try:
            _write_bytes_durably(marker_path, marker_bytes, exclusive=True)
        except FileExistsError as exc:
            raise StrategyArtifactRecoveryRequiredError(
                "strategy artifact publication is already pending"
            ) from exc
        except OSError:
            _fsync_directory(artifact_path.parent)
            raise
        _fsync_directory(artifact_path.parent)
        _fsync_directory_entries_to_anchor(artifact_path.parent, resolved_anchor)
        if _publication_record_exists(witness_path, label="commit witness"):
            _finish_publication_against_concurrent_commit(
                artifact_path,
                expected=encoded,
                marker_path=marker_path,
                marker_bytes=marker_bytes,
                witness_path=witness_path,
            )
            return
        if _path_entry_exists(artifact_path):
            _require_identical_artifact(artifact_path, encoded, fsync=True)
        else:
            _install_final_without_overwrite(
                temporary_path,
                artifact_path,
                expected=encoded,
            )
        _fsync_directory(artifact_path.parent)
        _require_identical_artifact(artifact_path, encoded, fsync=True)
        _establish_commit_witness(
            marker_path,
            witness_path,
            marker_bytes=marker_bytes,
            parent=artifact_path.parent,
        )
        _remove_pending_marker_durably(
            marker_path,
            witness_path=witness_path,
            parent=artifact_path.parent,
        )
    except StrategyArtifactPublicationError:
        raise
    except OSError as exc:
        raise StrategyArtifactPublicationError(
            f"strategy artifact publication failed: {type(exc).__name__}"
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def recover_strategy_artifact(
    path: Path,
    text: str,
    *,
    durability_anchor: Path,
) -> None:
    """Explicitly complete a failed publication after verifying its exact bytes."""

    artifact_path = Path(path)
    resolved_anchor = _resolve_durability_anchor(artifact_path, durability_anchor)
    encoded = text.encode("utf-8")
    marker_path = strategy_artifact_pending_marker_path(artifact_path)
    witness_path = strategy_artifact_commit_witness_path(artifact_path)
    temporary_path: Path | None = None
    try:
        if _publication_record_exists(witness_path, label="commit witness"):
            _require_recoverable_committed_artifact(
                artifact_path,
                expected=encoded,
                marker_path=marker_path,
                witness_path=witness_path,
            )
            _fsync_publication_record(witness_path, label="commit witness")
            _require_identical_artifact(artifact_path, encoded, fsync=True)
            _fsync_directory(artifact_path.parent)
            _fsync_directory_entries_to_anchor(artifact_path.parent, resolved_anchor)
            _require_recoverable_committed_artifact(
                artifact_path,
                expected=encoded,
                marker_path=marker_path,
                witness_path=witness_path,
            )
            _remove_pending_marker_durably(
                marker_path,
                witness_path=witness_path,
                parent=artifact_path.parent,
            )
            return
        if not _publication_record_exists(marker_path, label="pending marker"):
            publish_strategy_artifact(
                artifact_path,
                text,
                durability_anchor=resolved_anchor,
            )
            return

        marker = _read_publication_record(marker_path, label="pending marker")
        expected_digest = _content_sha256(encoded)
        if marker["content_sha256"] != expected_digest:
            raise StrategyArtifactConflictError(
                "pending strategy artifact publication belongs to different content"
            )
        created_directories = _created_directories_from_count(
            artifact_path.parent,
            marker["created_parent_count"],
        )
        _require_anchor_covers_created_directories(
            created_directories,
            anchor=resolved_anchor,
        )
        marker_bytes = _publication_record_bytes(
            encoded,
            created_parent_count=marker["created_parent_count"],
        )
        _read_regular_publication_record_bytes(
            marker_path,
            label="pending marker",
            expected=marker_bytes,
            fsync=True,
        )
        _fsync_directory(artifact_path.parent)
        _fsync_directory_entries_to_anchor(artifact_path.parent, resolved_anchor)
        if _path_entry_exists(artifact_path):
            _require_identical_artifact(artifact_path, encoded, fsync=True)
        else:
            temporary_path = artifact_path.with_name(
                f".{artifact_path.name}.{uuid.uuid4().hex}.tmp"
            )
            _write_bytes_durably(temporary_path, encoded, exclusive=True)

        if temporary_path is not None:
            _install_final_without_overwrite(
                temporary_path,
                artifact_path,
                expected=encoded,
            )
        _fsync_directory(artifact_path.parent)
        _require_identical_artifact(artifact_path, encoded, fsync=True)
        _establish_commit_witness(
            marker_path,
            witness_path,
            marker_bytes=marker_bytes,
            parent=artifact_path.parent,
        )
        _remove_pending_marker_durably(
            marker_path,
            witness_path=witness_path,
            parent=artifact_path.parent,
        )
    except StrategyArtifactPublicationError:
        raise
    except OSError as exc:
        raise StrategyArtifactPublicationError(
            f"strategy artifact recovery failed: {type(exc).__name__}"
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def read_strategy_artifact_text(path: Path) -> str:
    """Read only a legacy artifact or one bound by an exact commit witness."""

    artifact_path = Path(path)
    marker_path = strategy_artifact_pending_marker_path(artifact_path)
    witness_path = strategy_artifact_commit_witness_path(artifact_path)
    _reject_pending_marker(marker_path)
    if _publication_record_exists(witness_path, label="commit witness"):
        witness = _read_publication_record(witness_path, label="commit witness")
        text = _read_regular_artifact_text(artifact_path)
        _require_record_content(witness, text.encode("utf-8"), label="commit witness")
        if not _publication_record_exists(witness_path, label="commit witness"):
            raise StrategyArtifactRecoveryRequiredError(
                "strategy artifact commit witness changed during read"
            )
        if _read_publication_record(witness_path, label="commit witness") != witness:
            raise StrategyArtifactRecoveryRequiredError(
                "strategy artifact commit witness changed during read"
            )
        _reject_pending_marker(marker_path)
        return text

    text = _read_regular_artifact_text(artifact_path)
    if _publication_record_exists(witness_path, label="commit witness"):
        raise StrategyArtifactRecoveryRequiredError(
            "strategy artifact publication state changed during read"
        )
    _reject_pending_marker(marker_path)
    return text


def _reject_pending_marker(marker_path: Path) -> None:
    if _publication_record_exists(marker_path, label="pending marker"):
        raise StrategyArtifactRecoveryRequiredError(
            "strategy artifact has an unresolved pending publication"
        )


def _require_committed_artifact(
    artifact_path: Path,
    *,
    expected: bytes,
    marker_path: Path,
    witness_path: Path,
) -> None:
    witness, committed_bytes = _read_committed_artifact(
        artifact_path,
        witness_path=witness_path,
    )
    if committed_bytes != expected:
        raise StrategyArtifactConflictError(
            "immutable strategy artifact path already contains different content"
        )
    _require_matching_pending_record(marker_path, witness)


def _require_recoverable_committed_artifact(
    artifact_path: Path,
    *,
    expected: bytes,
    marker_path: Path,
    witness_path: Path,
) -> None:
    witness, committed_bytes = _read_committed_artifact(
        artifact_path,
        witness_path=witness_path,
    )
    if committed_bytes != expected:
        raise StrategyArtifactConflictError(
            "immutable strategy artifact path already contains different content"
        )
    _require_matching_pending_content(marker_path, witness)


def _read_committed_artifact(
    artifact_path: Path,
    *,
    witness_path: Path,
) -> tuple[dict[str, Any], bytes]:
    witness = _read_publication_record(witness_path, label="commit witness")
    committed_bytes = _read_regular_artifact_bytes(artifact_path)
    _require_record_content(witness, committed_bytes, label="commit witness")
    return witness, committed_bytes


def _finish_publication_against_concurrent_commit(
    artifact_path: Path,
    *,
    expected: bytes,
    marker_path: Path,
    marker_bytes: bytes,
    witness_path: Path,
) -> None:
    _read_regular_publication_record_bytes(
        marker_path,
        label="pending marker",
        expected=marker_bytes,
        fsync=True,
    )
    witness, committed_bytes = _read_committed_artifact(
        artifact_path,
        witness_path=witness_path,
    )
    _fsync_publication_record(witness_path, label="commit witness")
    _read_regular_artifact_bytes(
        artifact_path,
        expected=committed_bytes,
        fsync=True,
    )
    _fsync_directory(artifact_path.parent)
    current_witness, current_bytes = _read_committed_artifact(
        artifact_path,
        witness_path=witness_path,
    )
    if current_witness != witness or current_bytes != committed_bytes:
        raise StrategyArtifactRecoveryRequiredError(
            "strategy artifact concurrent commit changed during verification"
        )
    _read_regular_publication_record_bytes(
        marker_path,
        label="pending marker",
        expected=marker_bytes,
    )
    _remove_pending_marker_durably(
        marker_path,
        witness_path=witness_path,
        parent=artifact_path.parent,
    )
    if committed_bytes != expected:
        raise StrategyArtifactConflictError(
            "immutable strategy artifact path already contains different content"
        )


def _require_matching_pending_record(
    marker_path: Path,
    witness: dict[str, Any],
) -> None:
    if not _publication_record_exists(marker_path, label="pending marker"):
        return
    pending = _read_publication_record(marker_path, label="pending marker")
    if pending != witness:
        raise StrategyArtifactRecoveryRequiredError(
            "strategy artifact pending marker does not match the commit witness"
        )


def _require_matching_pending_content(
    marker_path: Path,
    witness: dict[str, Any],
) -> None:
    if not _publication_record_exists(marker_path, label="pending marker"):
        return
    pending = _read_publication_record(marker_path, label="pending marker")
    if pending["content_sha256"] != witness["content_sha256"]:
        raise StrategyArtifactRecoveryRequiredError(
            "strategy artifact pending marker content does not match the commit witness"
        )


def _require_record_content(
    record: dict[str, Any],
    content: bytes,
    *,
    label: str,
) -> None:
    if record["content_sha256"] != _content_sha256(content):
        raise StrategyArtifactConflictError(
            f"strategy artifact {label} does not match the requested content"
        )


def _require_identical_artifact(path: Path, expected: bytes, *, fsync: bool = False) -> None:
    _read_regular_artifact_bytes(path, expected=expected, fsync=fsync)


def _install_final_without_overwrite(
    temporary_path: Path,
    artifact_path: Path,
    *,
    expected: bytes,
) -> None:
    # A hard-link install is atomic but, unlike os.replace, cannot overwrite a
    # conflicting immutable identity that appears after the preflight check.
    # The linked temporary name must be retired before the final inode can be
    # accepted as immutable; the caller's following directory fsync commits
    # both the final install and temporary-name removal.
    try:
        os.link(temporary_path, artifact_path)
    except FileExistsError:
        _require_identical_artifact(artifact_path, expected, fsync=True)
    else:
        temporary_path.unlink()


def _publication_record_bytes(content: bytes, *, created_parent_count: int) -> bytes:
    return json.dumps(
        {
            "schema_version": _PUBLICATION_RECORD_SCHEMA_VERSION,
            "content_sha256": _content_sha256(content),
            "created_parent_count": created_parent_count,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _publication_record_exists(record_path: Path, *, label: str) -> bool:
    try:
        record_status = record_path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise StrategyArtifactRecoveryRequiredError(
            f"strategy artifact {label} cannot be inspected"
        ) from exc
    _require_regular_file(record_status, label=label)
    return True


def _read_regular_publication_record_bytes(
    record_path: Path,
    *,
    label: str,
    expected: bytes | None = None,
    fsync: bool = False,
) -> bytes:
    try:
        return _read_identity_checked_regular_bytes(
            record_path,
            label=label,
            expected=expected,
            fsync=fsync,
            require_single_link=False,
        )
    except FileNotFoundError as exc:
        raise StrategyArtifactRecoveryRequiredError(
            f"strategy artifact {label} cannot be read safely"
        ) from exc


def _read_regular_artifact_bytes(
    artifact_path: Path,
    *,
    expected: bytes | None = None,
    fsync: bool = False,
) -> bytes:
    return _read_identity_checked_regular_bytes(
        artifact_path,
        label="final artifact",
        expected=expected,
        fsync=fsync,
        require_single_link=True,
    )


def _read_regular_artifact_text(artifact_path: Path) -> str:
    try:
        return _read_regular_artifact_bytes(artifact_path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StrategyArtifactRecoveryRequiredError(
            "strategy artifact final artifact is not valid UTF-8"
        ) from exc


def _read_identity_checked_regular_bytes(
    path: Path,
    *,
    label: str,
    expected: bytes | None,
    fsync: bool,
    require_single_link: bool,
) -> bytes:
    try:
        path_status = path.lstat()
        _require_regular_file(path_status, label=label)
        if require_single_link:
            _require_single_link(path_status, label=label)
        with path.open("r+b" if fsync else "rb") as handle:
            opened_status = os.fstat(handle.fileno())
            _require_regular_file(opened_status, label=label)
            if require_single_link:
                _require_single_link(opened_status, label=label)
            if not os.path.samestat(path_status, opened_status):
                raise StrategyArtifactRecoveryRequiredError(
                    f"strategy artifact {label} changed before read"
                )
            content = handle.read()
            if expected is not None and content != expected:
                raise StrategyArtifactConflictError(
                    f"strategy artifact {label} does not match the expected bytes"
                )
            if fsync:
                os.fsync(handle.fileno())
        current_status = path.lstat()
        _require_regular_file(current_status, label=label)
        if require_single_link:
            _require_single_link(current_status, label=label)
        if not os.path.samestat(opened_status, current_status):
            raise StrategyArtifactRecoveryRequiredError(
                f"strategy artifact {label} changed during read"
            )
        return content
    except StrategyArtifactPublicationError:
        raise
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise StrategyArtifactRecoveryRequiredError(
            f"strategy artifact {label} cannot be read safely"
        ) from exc


def _fsync_publication_record(record_path: Path, *, label: str) -> None:
    _read_regular_publication_record_bytes(record_path, label=label, fsync=True)


def _require_regular_file(file_status: os.stat_result, *, label: str) -> None:
    if not stat.S_ISREG(file_status.st_mode):
        raise StrategyArtifactRecoveryRequiredError(
            f"strategy artifact {label} is not a regular file"
        )


def _require_single_link(file_status: os.stat_result, *, label: str) -> None:
    if file_status.st_nlink != 1:
        raise StrategyArtifactRecoveryRequiredError(
            f"strategy artifact {label} has multiple hard links"
        )


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _read_publication_record(record_path: Path, *, label: str) -> dict[str, Any]:
    record_bytes = _read_regular_publication_record_bytes(record_path, label=label)
    try:
        payload = json.loads(record_bytes.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != _PUBLICATION_RECORD_FIELDS:
            raise ValueError(f"{label} fields do not match the strategy publication schema")
        if payload.get("schema_version") != _PUBLICATION_RECORD_SCHEMA_VERSION:
            raise ValueError("unsupported strategy publication marker schema")
        digest = payload.get("content_sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("pending marker content digest is invalid")
        count = payload.get("created_parent_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("pending marker created_parent_count is invalid")
        return payload
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise StrategyArtifactRecoveryRequiredError(
            f"strategy artifact {label} is invalid"
        ) from exc


def _content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_bytes_durably(path: Path, content: bytes, *, exclusive: bool) -> None:
    mode = "xb" if exclusive else "wb"
    created_status: os.stat_result | None = None
    try:
        with path.open(mode) as handle:
            created_status = os.fstat(handle.fileno())
            written = handle.write(content)
            if written != len(content):
                raise OSError(f"short strategy artifact write: {written}/{len(content)} bytes")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        if exclusive and created_status is not None:
            _unlink_owned_file_best_effort(path, expected_status=created_status)
        raise


def _unlink_owned_file_best_effort(path: Path, *, expected_status: os.stat_result) -> None:
    try:
        current_status = path.lstat()
        if stat.S_ISREG(current_status.st_mode) and os.path.samestat(
            expected_status,
            current_status,
        ):
            path.unlink()
    except OSError:
        pass


def _create_parent_directories(parent: Path) -> tuple[Path, ...]:
    created_directories: list[Path] = []
    directory = parent
    while not directory.exists():
        created_directories.append(directory)
        ancestor = directory.parent
        if ancestor == directory:
            raise OSError("strategy artifact has no existing parent directory")
        directory = ancestor
    parent.mkdir(parents=True, exist_ok=True)
    return tuple(created_directories)


def _created_directories_from_count(parent: Path, count: int) -> tuple[Path, ...]:
    created_directories: list[Path] = []
    directory = parent
    for _ in range(count):
        if not directory.is_dir():
            raise StrategyArtifactRecoveryRequiredError(
                "strategy artifact recovery directory chain is missing"
            )
        created_directories.append(directory)
        ancestor = directory.parent
        if ancestor == directory:
            raise StrategyArtifactRecoveryRequiredError(
                "strategy artifact recovery directory count escapes the path"
            )
        directory = ancestor
    return tuple(created_directories)


def _require_anchor_covers_created_directories(
    created_directories: tuple[Path, ...],
    *,
    anchor: Path,
) -> None:
    if not created_directories:
        return
    try:
        first_preexisting_parent = created_directories[-1].parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise StrategyArtifactRecoveryRequiredError(
            "strategy artifact recorded parent lineage cannot be resolved"
        ) from exc
    if (
        first_preexisting_parent != anchor
        and anchor not in first_preexisting_parent.parents
    ):
        raise StrategyArtifactRecoveryRequiredError(
            "strategy artifact durability anchor is inside the recorded parent lineage"
        )


def _resolve_durability_anchor(artifact_path: Path, durability_anchor: Path) -> Path:
    try:
        resolved_anchor = Path(durability_anchor).resolve(strict=True)
        resolved_parent = artifact_path.parent.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise StrategyArtifactPublicationError(
            "strategy artifact durability anchor cannot be resolved"
        ) from exc
    if not resolved_anchor.is_dir():
        raise StrategyArtifactPublicationError(
            "strategy artifact durability anchor must be an existing directory"
        )
    if resolved_parent != resolved_anchor and resolved_anchor not in resolved_parent.parents:
        raise StrategyArtifactPublicationError(
            "strategy artifact durability anchor must contain the artifact path"
        )
    return resolved_anchor


def _fsync_directory_entries_to_anchor(parent: Path, anchor: Path) -> None:
    try:
        directory = parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise StrategyArtifactRecoveryRequiredError(
            "strategy artifact parent directory chain cannot be resolved"
        ) from exc
    if directory != anchor and anchor not in directory.parents:
        raise StrategyArtifactRecoveryRequiredError(
            "strategy artifact parent directory escaped its durability anchor"
        )
    while directory != anchor:
        _fsync_directory(directory.parent)
        directory = directory.parent


def _establish_commit_witness(
    marker_path: Path,
    witness_path: Path,
    *,
    marker_bytes: bytes,
    parent: Path,
) -> None:
    created_witness = False
    try:
        os.link(marker_path, witness_path)
        created_witness = True
    except FileExistsError:
        _read_regular_publication_record_bytes(
            witness_path,
            label="commit witness",
            expected=marker_bytes,
            fsync=True,
        )
    try:
        _fsync_directory(parent)
    except OSError:
        if created_witness:
            try:
                witness_path.unlink(missing_ok=True)
                _fsync_directory(parent)
            except OSError:
                pass
        raise
    _read_regular_publication_record_bytes(
        witness_path,
        label="commit witness",
        expected=marker_bytes,
        fsync=True,
    )


def _remove_pending_marker_durably(
    marker_path: Path,
    *,
    witness_path: Path,
    parent: Path,
) -> None:
    if not _publication_record_exists(marker_path, label="pending marker"):
        return
    try:
        marker_path.unlink(missing_ok=True)
        _fsync_directory(parent)
    except OSError:
        _restore_pending_marker_best_effort(
            marker_path,
            witness_path=witness_path,
            parent=parent,
        )
        if _publication_record_exists(marker_path, label="pending marker"):
            raise
        try:
            _fsync_directory(parent)
        except OSError:
            # The exact final file and positive witness were already durable at
            # the commit point. If every barrier-restoration path is unavailable
            # but unlink remains visible, raising would expose committed evidence
            # behind an error. A restart may conservatively resurrect the matching
            # pending record, in which case normal recovery removes it.
            if _publication_record_exists(marker_path, label="pending marker"):
                raise


def _restore_pending_marker_best_effort(
    marker_path: Path,
    *,
    witness_path: Path,
    parent: Path,
) -> None:
    try:
        if not _path_entry_exists(marker_path):
            try:
                os.link(witness_path, marker_path)
            except FileExistsError:
                pass
            except OSError:
                try:
                    _write_bytes_durably(
                        marker_path,
                        _read_regular_publication_record_bytes(
                            witness_path,
                            label="commit witness",
                        ),
                        exclusive=True,
                    )
                except FileExistsError:
                    pass
        _fsync_directory(parent)
    except OSError:
        pass


def _fsync_directory(directory: Path) -> None:
    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = (wintypes.HANDLE,)
    flush_file_buffers.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    generic_write = 0x40000000
    share_read_write_delete = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    handle = create_file(
        str(directory),
        generic_write,
        share_read_write_delete,
        None,
        open_existing,
        file_flag_backup_semantics,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error), str(directory))
    try:
        if not flush_file_buffers(handle):
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(directory))
    finally:
        close_handle(handle)
