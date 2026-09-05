from __future__ import annotations

import os
from contextlib import contextmanager


class FileLockBusyError(BlockingIOError):
    pass


@contextmanager
def locked_file(path, *, wait: bool = True):
    with path.open("a+b") as stream:
        lock_file(stream, wait=wait)
        try:
            yield
        finally:
            unlock_file(stream)


def lock_file(stream, *, shared: bool = False, wait: bool = False) -> None:
    stream.seek(0)
    try:
        if os.name == "nt":
            _windows_lock(stream, shared=shared, wait=wait)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError as exc:
        raise FileLockBusyError("file is locked") from exc


def unlock_file(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        _windows_lock(stream, release=True)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _windows_lock(stream, *, shared: bool = False, wait: bool = False, release: bool = False) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = api.UnlockFileEx if release else api.LockFileEx
    operation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
    if not release:
        operation.argtypes += [wintypes.DWORD]
    operation.argtypes += [ctypes.POINTER(Overlapped)]
    operation.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(stream.fileno())
    overlap = Overlapped()
    args = [handle, 0, 1, 0, ctypes.byref(overlap)] if release else [
        handle, (0 if wait else 1) | (0 if shared else 2), 0, 1, 0, ctypes.byref(overlap),
    ]
    if not operation(*args):
        code = ctypes.get_last_error()
        if not release and code == 33:  # ERROR_LOCK_VIOLATION
            raise FileLockBusyError("file is locked")
        raise ctypes.WinError(code)
