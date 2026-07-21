from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class _WindowsDirectoryApi:
    create_file: Callable[..., Any]
    flush_file_buffers: Callable[[Any], Any]
    close_handle: Callable[[Any], Any]
    invalid_handle_value: Any
    get_last_error: Callable[[], int]
    format_error: Callable[[int], str]


def fsync_directory(directory: Path) -> None:
    """Make prior directory-entry changes durable or raise ``OSError``."""

    platform = _platform_name()
    if platform == "posix":
        _fsync_posix_directory(directory)
        return
    if platform == "nt":
        _fsync_windows_directory(directory)
        return
    raise OSError(
        errno.ENOTSUP,
        f"directory durability is unsupported on platform {platform!r}",
        str(directory),
    )


def _platform_name() -> str:
    return os.name


def _fsync_posix_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)

    flush_error: BaseException | None = None
    try:
        os.fsync(descriptor)
    except BaseException as exc:
        flush_error = exc

    close_error: BaseException | None = None
    try:
        os.close(descriptor)
    except BaseException as exc:
        close_error = exc

    if flush_error is not None:
        raise flush_error
    if close_error is not None:
        raise close_error


def _fsync_windows_directory(directory: Path) -> None:
    api = _load_windows_directory_api()
    generic_write = 0x40000000
    share_read_write_delete = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    handle = api.create_file(
        str(directory),
        generic_write,
        share_read_write_delete,
        None,
        open_existing,
        file_flag_backup_semantics,
        None,
    )
    if handle == api.invalid_handle_value:
        raise _windows_os_error(api, directory)

    flush_error: BaseException | None = None
    try:
        if not api.flush_file_buffers(handle):
            flush_error = _windows_os_error(api, directory)
    except BaseException as exc:
        flush_error = exc

    close_error: BaseException | None = None
    try:
        if not api.close_handle(handle):
            close_error = _windows_os_error(api, directory)
    except BaseException as exc:
        close_error = exc

    if flush_error is not None:
        raise flush_error
    if close_error is not None:
        raise close_error


def _windows_os_error(api: _WindowsDirectoryApi, directory: Path) -> OSError:
    error = api.get_last_error()
    return OSError(error, api.format_error(error), str(directory))


def _load_windows_directory_api() -> _WindowsDirectoryApi:
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
    return _WindowsDirectoryApi(
        create_file=create_file,
        flush_file_buffers=flush_file_buffers,
        close_handle=close_handle,
        invalid_handle_value=ctypes.c_void_p(-1).value,
        get_last_error=ctypes.get_last_error,
        format_error=ctypes.FormatError,
    )


__all__ = ["fsync_directory"]
