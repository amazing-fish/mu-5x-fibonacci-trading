from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _ERROR_LOCK_VIOLATION = 33

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _LOCK_FILE_EX = _KERNEL32.LockFileEx
    _LOCK_FILE_EX.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    )
    _LOCK_FILE_EX.restype = wintypes.BOOL

    _UNLOCK_FILE_EX = _KERNEL32.UnlockFileEx
    _UNLOCK_FILE_EX.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    )
    _UNLOCK_FILE_EX.restype = wintypes.BOOL
else:
    import fcntl


class FileLockBusyError(BlockingIOError):
    pass


@contextmanager
def locked_file(
    path: Path | str,
    *,
    wait: bool = True,
    shared: bool = False,
    timeout_seconds: float | None = None,
) -> Iterator[None]:
    target = path if isinstance(path, Path) else Path(path)
    with target.open("a+b") as stream:
        kwargs: dict[str, object] = {"wait": wait}
        if shared:
            kwargs["shared"] = True
        if timeout_seconds is not None:
            kwargs["timeout_seconds"] = timeout_seconds
        lock_file(stream, **kwargs)
        try:
            yield
        finally:
            unlock_file(stream)


def lock_file(
    stream,
    *,
    shared: bool = False,
    wait: bool = False,
    timeout_seconds: float | None = None,
) -> None:
    if timeout_seconds is not None:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds < 0:
            raise ValueError("timeout_seconds must be a non-negative number or None")
        deadline = time.monotonic() + float(timeout_seconds)
        poll_interval = 0.005
        while True:
            try:
                _lock_once(stream, shared=shared, wait=False)
                return
            except FileLockBusyError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FileLockBusyError(f"file lock timed out after {timeout_seconds}s") from None
                time.sleep(min(poll_interval, remaining))
                poll_interval = min(poll_interval * 2.0, 0.05)
    _lock_once(stream, shared=shared, wait=wait)


def _lock_once(stream, *, shared: bool = False, wait: bool = False) -> None:
    stream.seek(0)
    try:
        if os.name == "nt":
            _windows_lock(stream, shared=shared, wait=wait)
        else:
            flags = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if wait else fcntl.LOCK_NB)
            fcntl.flock(stream.fileno(), flags)
    except BlockingIOError as exc:
        raise FileLockBusyError("file is locked") from exc


def unlock_file(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        _windows_lock(stream, release=True)
    else:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _windows_lock(stream, *, shared: bool = False, wait: bool = False, release: bool = False) -> None:
    handle = msvcrt.get_osfhandle(stream.fileno())
    overlap = _Overlapped()
    if release:
        ok = _UNLOCK_FILE_EX(handle, 0, 1, 0, ctypes.byref(overlap))
    else:
        flags = (0 if wait else _LOCKFILE_FAIL_IMMEDIATELY) | (0 if shared else _LOCKFILE_EXCLUSIVE_LOCK)
        ok = _LOCK_FILE_EX(handle, flags, 0, 1, 0, ctypes.byref(overlap))
    if not ok:
        code = ctypes.get_last_error()
        if not release and code == _ERROR_LOCK_VIOLATION:
            raise FileLockBusyError("file is locked")
        raise ctypes.WinError(code)
