from __future__ import annotations

import hashlib
import json
import os
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


def publish_strategy_artifact(path: Path, text: str) -> None:
    """Publish immutable strategy artifact bytes and return only after a durable commit."""

    artifact_path = Path(path)
    encoded = text.encode("utf-8")
    marker_path = strategy_artifact_pending_marker_path(artifact_path)
    witness_path = strategy_artifact_commit_witness_path(artifact_path)
    temporary_path: Path | None = None

    try:
        created_directories = _create_parent_directories(artifact_path.parent)
        if witness_path.exists():
            _require_committed_artifact(
                artifact_path,
                expected=encoded,
                marker_path=marker_path,
                witness_path=witness_path,
            )
            if marker_path.exists():
                raise StrategyArtifactRecoveryRequiredError(
                    "strategy artifact has an unresolved pending publication"
                )
            _fsync_file(witness_path)
            _fsync_file(artifact_path)
            _fsync_directory(artifact_path.parent)
            return
        if marker_path.exists():
            raise StrategyArtifactRecoveryRequiredError(
                "strategy artifact has an unresolved pending publication"
            )
        if artifact_path.exists():
            _require_identical_artifact(artifact_path, encoded)
            _fsync_file(artifact_path)
            _fsync_directory(artifact_path.parent)
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
        _fsync_directory(artifact_path.parent)
        _fsync_created_directory_entries(created_directories)

        if artifact_path.exists():
            _require_identical_artifact(artifact_path, encoded)
            _fsync_file(artifact_path)
        else:
            _install_final_without_overwrite(
                temporary_path,
                artifact_path,
                expected=encoded,
            )
        _fsync_directory(artifact_path.parent)
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


def recover_strategy_artifact(path: Path, text: str) -> None:
    """Explicitly complete a failed publication after verifying its exact bytes."""

    artifact_path = Path(path)
    encoded = text.encode("utf-8")
    marker_path = strategy_artifact_pending_marker_path(artifact_path)
    witness_path = strategy_artifact_commit_witness_path(artifact_path)
    temporary_path: Path | None = None
    try:
        if witness_path.exists():
            _require_committed_artifact(
                artifact_path,
                expected=encoded,
                marker_path=marker_path,
                witness_path=witness_path,
            )
            _fsync_file(witness_path)
            _fsync_file(artifact_path)
            _fsync_directory(artifact_path.parent)
            _remove_pending_marker_durably(
                marker_path,
                witness_path=witness_path,
                parent=artifact_path.parent,
            )
            return
        if not marker_path.exists():
            publish_strategy_artifact(artifact_path, text)
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
        marker_bytes = _publication_record_bytes(
            encoded,
            created_parent_count=marker["created_parent_count"],
        )
        _fsync_file(marker_path)
        _fsync_directory(artifact_path.parent)
        if artifact_path.exists():
            _require_identical_artifact(artifact_path, encoded)
            _fsync_file(artifact_path)
        else:
            temporary_path = artifact_path.with_name(
                f".{artifact_path.name}.{uuid.uuid4().hex}.tmp"
            )
            _write_bytes_durably(temporary_path, encoded, exclusive=True)

        _fsync_created_directory_entries(created_directories)
        if temporary_path is not None:
            _install_final_without_overwrite(
                temporary_path,
                artifact_path,
                expected=encoded,
            )
        _fsync_directory(artifact_path.parent)
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
    if witness_path.exists():
        witness = _read_publication_record(witness_path, label="commit witness")
        text = artifact_path.read_text(encoding="utf-8")
        _require_record_content(witness, text.encode("utf-8"), label="commit witness")
        if not witness_path.exists():
            raise StrategyArtifactRecoveryRequiredError(
                "strategy artifact commit witness changed during read"
            )
        if _read_publication_record(witness_path, label="commit witness") != witness:
            raise StrategyArtifactRecoveryRequiredError(
                "strategy artifact commit witness changed during read"
            )
        _reject_pending_marker(marker_path)
        return text

    text = artifact_path.read_text(encoding="utf-8")
    if witness_path.exists():
        raise StrategyArtifactRecoveryRequiredError(
            "strategy artifact publication state changed during read"
        )
    _reject_pending_marker(marker_path)
    return text


def _reject_pending_marker(marker_path: Path) -> None:
    if marker_path.exists():
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
    witness = _read_publication_record(witness_path, label="commit witness")
    _require_record_content(witness, expected, label="commit witness")
    _require_matching_pending_record(marker_path, witness)
    _require_identical_artifact(artifact_path, expected)


def _require_matching_pending_record(
    marker_path: Path,
    witness: dict[str, Any],
) -> None:
    if not marker_path.exists():
        return
    pending = _read_publication_record(marker_path, label="pending marker")
    if pending != witness:
        raise StrategyArtifactRecoveryRequiredError(
            "strategy artifact pending marker does not match the commit witness"
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


def _require_identical_artifact(path: Path, expected: bytes) -> None:
    if path.read_bytes() != expected:
        raise StrategyArtifactConflictError(
            "immutable strategy artifact path already contains different content"
        )


def _install_final_without_overwrite(
    temporary_path: Path,
    artifact_path: Path,
    *,
    expected: bytes,
) -> None:
    # A hard-link install is atomic but, unlike os.replace, cannot overwrite a
    # conflicting immutable identity that appears after the preflight check.
    try:
        os.link(temporary_path, artifact_path)
    except FileExistsError:
        _require_identical_artifact(artifact_path, expected)
        _fsync_file(artifact_path)


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


def _read_publication_record(record_path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
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
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise StrategyArtifactRecoveryRequiredError(
            f"strategy artifact {label} is invalid"
        ) from exc


def _content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_bytes_durably(path: Path, content: bytes, *, exclusive: bool) -> None:
    mode = "xb" if exclusive else "wb"
    with path.open(mode) as handle:
        written = handle.write(content)
        if written != len(content):
            raise OSError(f"short strategy artifact write: {written}/{len(content)} bytes")
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


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


def _fsync_created_directory_entries(created_directories: tuple[Path, ...]) -> None:
    for directory in created_directories:
        _fsync_directory(directory.parent)


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
        _require_identical_artifact(witness_path, marker_bytes)
        _fsync_file(witness_path)
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


def _remove_pending_marker_durably(
    marker_path: Path,
    *,
    witness_path: Path,
    parent: Path,
) -> None:
    if not marker_path.exists():
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
        if marker_path.exists():
            raise
        try:
            _fsync_directory(parent)
        except OSError:
            # The exact final file and positive witness were already durable at
            # the commit point. If every barrier-restoration path is unavailable
            # but unlink remains visible, raising would expose committed evidence
            # behind an error. A restart may conservatively resurrect the matching
            # pending record, in which case normal recovery removes it.
            if marker_path.exists():
                raise


def _restore_pending_marker_best_effort(
    marker_path: Path,
    *,
    witness_path: Path,
    parent: Path,
) -> None:
    try:
        if not marker_path.exists():
            try:
                os.link(witness_path, marker_path)
            except FileExistsError:
                pass
            except OSError:
                try:
                    _write_bytes_durably(
                        marker_path,
                        witness_path.read_bytes(),
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
