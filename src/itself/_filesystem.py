# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Small filesystem primitives with explicit overwrite and durability semantics."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from contextlib import suppress
from pathlib import Path

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004


class AtomicPublishUnavailableError(OSError):
    """Raised when a platform cannot atomically publish without replacement."""


def _require_private_directory(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OSError(f"private path component is not a real directory: {path}")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & (
        stat.S_IRWXG | stat.S_IRWXO
    ):
        raise PermissionError(
            f"private directory grants group or other permissions: {path}"
        )


def ensure_private_directory(root: Path, path: Path) -> None:
    """Create a private directory below an existing private boundary."""

    root = Path(root)
    path = Path(path)
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ValueError("private directory must remain below its boundary") from error
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("private directory must remain below its boundary")

    _require_private_directory(root)
    current = root
    for part in relative.parts:
        current /= part
        with suppress(FileExistsError):
            current.mkdir(mode=stat.S_IRWXU)
        _require_private_directory(current)


def fsync_directory(path: Path) -> None:
    """Synchronize a directory entry update on platforms that support it."""

    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_file_exclusive(
    path: Path,
    content: bytes,
    *,
    mode: int,
) -> None:
    """Create, synchronize, and close one file without replacing any path."""

    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def write_private_file_exclusive(path: Path, content: bytes) -> None:
    """Create one owner-readable file without replacing an existing path."""

    write_file_exclusive(
        path,
        content,
        mode=stat.S_IRUSR | stat.S_IWUSR,
    )


def _raise_rename_error(error_number: int, destination: Path) -> None:
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(
            error_number,
            os.strerror(error_number),
            destination,
        )
    raise OSError(error_number, os.strerror(error_number), destination)


def _linux_rename_no_replace(source: Path, destination: Path) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = library.renameat2
    except AttributeError as error:
        raise AtomicPublishUnavailableError(
            errno.ENOTSUP,
            "libc does not expose renameat2 with RENAME_NOREPLACE",
        ) from error
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        _raise_rename_error(ctypes.get_errno(), destination)


def _darwin_rename_no_replace(source: Path, destination: Path) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    try:
        renamex_np = library.renamex_np
    except AttributeError as error:
        raise AtomicPublishUnavailableError(
            errno.ENOTSUP,
            "libc does not expose renamex_np with RENAME_EXCL",
        ) from error
    renamex_np.argtypes = (
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renamex_np.restype = ctypes.c_int
    result = renamex_np(
        os.fsencode(source),
        os.fsencode(destination),
        _RENAME_EXCL,
    )
    if result != 0:
        _raise_rename_error(ctypes.get_errno(), destination)


def publish_path_no_replace(source: Path, destination: Path) -> None:
    """Atomically rename a sibling path while refusing every existing target."""

    source = Path(source)
    destination = Path(destination)
    if os.path.abspath(source.parent) != os.path.abspath(destination.parent):
        raise ValueError("atomic publication requires sibling source and destination")
    source_metadata = source.lstat()
    if stat.S_ISLNK(source_metadata.st_mode):
        raise OSError(f"publication source must not be a symbolic link: {source}")

    if sys.platform.startswith("linux"):
        _linux_rename_no_replace(source, destination)
    elif sys.platform == "darwin":
        _darwin_rename_no_replace(source, destination)
    elif os.name == "nt":
        os.rename(source, destination)
    else:
        raise AtomicPublishUnavailableError(
            errno.ENOTSUP,
            f"atomic no-replace publication is unsupported on {sys.platform}",
        )
