# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import ctypes
import errno
import os
import re
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest

import itself._filesystem as filesystem_module
from itself._filesystem import (
    AtomicPublishUnavailableError,
    _raise_rename_error,  # pyright: ignore[reportPrivateUsage]
    ensure_private_directory,
    fsync_directory,
    open_observed_regular_file,
    publish_path_no_replace,
    read_and_close,
    sync_directory_after_rename,
    write_file_exclusive,
    write_private_file_exclusive,
)

UNAVAILABLE_SYNC_ERRORS = ["EINVAL", "ENOTSUP", "EOPNOTSUPP", "EACCES", "EPERM"]
posix_only = pytest.mark.skipif(
    os.name != "posix", reason="directories are synchronized only on POSIX"
)


def _fail_with(code: int, calls: list[str], stage: str) -> Callable[..., NoReturn]:
    """Return a stand-in for ``os.open`` or ``os.fsync`` that fails with ``code``."""

    def fail(*args: object) -> NoReturn:
        del args
        calls.append(stage)
        raise OSError(code, os.strerror(code))

    return fail


@posix_only
@pytest.mark.parametrize("stage", ["open", "fsync"])
@pytest.mark.parametrize("name", UNAVAILABLE_SYNC_ERRORS)
def test_directory_sync_after_rename_skips_an_unavailable_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    name: str,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(os, stage, _fail_with(getattr(errno, name), calls, stage))

    sync_directory_after_rename(tmp_path)

    assert calls == [stage]


@posix_only
@pytest.mark.parametrize("stage", ["open", "fsync"])
def test_directory_sync_after_rename_reports_other_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(os, stage, _fail_with(errno.EIO, calls, stage))

    with pytest.raises(OSError) as raised:
        sync_directory_after_rename(tmp_path)

    assert raised.value.errno == errno.EIO
    assert calls == [stage]


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="needs POSIX permissions that bind the current user",
)
def test_directory_sync_after_rename_skips_a_directory_it_cannot_read(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "write-only"
    directory.mkdir()
    directory.chmod(0o300)
    try:
        # The strict primitive still reports what the tolerant sync skips.
        with pytest.raises(PermissionError):
            fsync_directory(directory)
        sync_directory_after_rename(directory)
    finally:
        directory.chmod(0o700)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode semantics")
def test_ensure_private_directory_never_repermissions_ancestors(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "caller-owned"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    root = parent / "private"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    target = root / "nested" / "artifacts"

    ensure_private_directory(root, target)

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "nested").stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_ensure_private_directory_rejects_path_outside_boundary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)

    with pytest.raises(ValueError, match="below its boundary"):
        ensure_private_directory(root, tmp_path / "outside")

    assert not (tmp_path / "outside").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode semantics")
def test_ensure_private_directory_preserves_insecure_existing_component(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    component = root / "caller-owned"
    component.mkdir(mode=0o755)
    component.chmod(0o755)

    with pytest.raises(PermissionError, match="group or other permissions"):
        ensure_private_directory(root, component / "nested")

    assert stat.S_IMODE(component.stat().st_mode) == 0o755
    assert not (component / "nested").exists()


def test_ensure_private_directory_rejects_symlink_component(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    component = root / "link"
    try:
        component.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(OSError, match="not a real directory"):
        ensure_private_directory(root, component / "nested")

    assert not (outside / "nested").exists()


def test_publish_path_no_replace_moves_complete_directory(tmp_path: Path) -> None:
    source = tmp_path / ".staging"
    source.mkdir()
    (source / "complete.txt").write_text("complete\n", encoding="utf-8")
    destination = tmp_path / "published"

    publish_path_no_replace(source, destination)

    assert not source.exists()
    assert (destination / "complete.txt").read_text(encoding="utf-8") == "complete\n"


def test_publish_path_no_replace_preserves_existing_empty_directory(
    tmp_path: Path,
) -> None:
    source = tmp_path / ".staging"
    source.mkdir()
    (source / "complete.txt").write_text("complete\n", encoding="utf-8")
    destination = tmp_path / "published"
    destination.mkdir()

    with pytest.raises(FileExistsError):
        publish_path_no_replace(source, destination)

    assert source.is_dir()
    assert destination.is_dir()
    assert tuple(destination.iterdir()) == ()


def test_publish_path_no_replace_preserves_existing_file(tmp_path: Path) -> None:
    source = tmp_path / ".staging"
    source.mkdir()
    destination = tmp_path / "published"
    destination.write_text("caller-owned\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        publish_path_no_replace(source, destination)

    assert source.is_dir()
    assert destination.read_text(encoding="utf-8") == "caller-owned\n"


def test_publish_path_no_replace_rejects_non_sibling_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "first" / ".staging"
    source.mkdir(parents=True)
    destination = tmp_path / "second" / "published"
    destination.parent.mkdir()

    with pytest.raises(ValueError, match="requires sibling"):
        publish_path_no_replace(source, destination)

    assert source.is_dir()
    assert not destination.exists()


def test_publish_path_no_replace_rejects_symlink_source(tmp_path: Path) -> None:
    real_source = tmp_path / "real"
    real_source.mkdir()
    source = tmp_path / ".staging"
    try:
        source.symlink_to(real_source, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(OSError, match="must not be a symbolic link"):
        publish_path_no_replace(source, tmp_path / "published")

    assert source.is_symlink()


def test_write_file_exclusive_never_replaces_existing_bytes(tmp_path: Path) -> None:
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"caller-owned")

    with pytest.raises(FileExistsError):
        write_file_exclusive(
            path,
            b"replacement",
            mode=stat.S_IRUSR | stat.S_IWUSR,
        )

    assert path.read_bytes() == b"caller-owned"


def _anchored(message: str) -> str:
    return f"^{re.escape(message)}$"


@pytest.fixture
def open_umask() -> Iterator[None]:
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


def _private_root(tmp_path: Path) -> Path:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def _staged(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / ".staging"
    source.mkdir()
    return source, tmp_path / "published"


class _FakeRename:
    """Stand-in for a libc rename function that records its calls."""

    def __init__(self, result: int) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: object) -> int:
        self.calls.append(args)
        return self.result


def _fake_ctypes(library: object, error_number: int = 0) -> SimpleNamespace:

    def load_library(name: object, *, use_errno: bool) -> object:
        del name, use_errno
        return library

    def get_errno() -> int:
        return error_number

    return SimpleNamespace(
        CDLL=load_library,
        get_errno=get_errno,
        c_int=ctypes.c_int,
        c_char_p=ctypes.c_char_p,
        c_uint=ctypes.c_uint,
    )


@posix_only
@pytest.mark.parametrize("mode", [0o750, 0o705, 0o770, 0o777])
def test_ensure_private_directory_rejects_a_boundary_that_grants_group_or_other_bits(
    tmp_path: Path, mode: int
) -> None:
    root = tmp_path / "private"
    root.mkdir()
    root.chmod(mode)

    with pytest.raises(
        PermissionError,
        match=_anchored(f"private directory grants group or other permissions: {root}"),
    ):
        ensure_private_directory(root, root / "child")

    assert not (root / "child").exists()


def test_ensure_private_directory_rejects_a_symlink_boundary(tmp_path: Path) -> None:
    real_root = _private_root(tmp_path)
    root = tmp_path / "link"
    try:
        root.symlink_to(real_root, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(
        OSError,
        match=_anchored(f"private path component is not a real directory: {root}"),
    ):
        ensure_private_directory(root, root / "child")

    assert not (real_root / "child").exists()


def test_ensure_private_directory_rejects_a_file_boundary(tmp_path: Path) -> None:
    root = tmp_path / "file"
    root.write_bytes(b"")

    with pytest.raises(
        OSError,
        match=_anchored(f"private path component is not a real directory: {root}"),
    ):
        ensure_private_directory(root, root / "child")


def test_ensure_private_directory_requires_an_existing_boundary(tmp_path: Path) -> None:
    root = tmp_path / "missing"

    with pytest.raises(FileNotFoundError):
        ensure_private_directory(root, root / "child")

    assert not root.exists()


@pytest.mark.parametrize("nested", ["file", "file/deeper"])
def test_ensure_private_directory_rejects_a_file_component(
    tmp_path: Path, nested: str
) -> None:
    root = _private_root(tmp_path)
    file_component = root / "file"
    file_component.write_bytes(b"caller-owned")

    with pytest.raises(
        OSError,
        match=_anchored(
            f"private path component is not a real directory: {file_component}"
        ),
    ):
        ensure_private_directory(root, root / nested)

    assert file_component.read_bytes() == b"caller-owned"


def test_ensure_private_directory_rejects_a_symlink_as_the_final_component(
    tmp_path: Path,
) -> None:
    root = _private_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(
        OSError,
        match=_anchored(f"private path component is not a real directory: {link}"),
    ):
        ensure_private_directory(root, link)


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param("..", id="parent-only"),
        pytest.param("../sibling", id="escapes-to-sibling"),
        pytest.param("child/../..", id="climbs-out"),
        pytest.param("child/../other", id="stays-inside-but-contains-dotdot"),
    ],
)
def test_ensure_private_directory_rejects_parent_components(
    tmp_path: Path, relative: str
) -> None:
    root = _private_root(tmp_path)

    with pytest.raises(
        ValueError, match=_anchored("private directory must remain below its boundary")
    ):
        ensure_private_directory(root, root / relative)

    assert sorted(path.name for path in tmp_path.iterdir()) == ["private"]
    assert tuple(root.iterdir()) == ()


def test_ensure_private_directory_rejects_a_relative_path_outside_an_absolute_root(
    tmp_path: Path,
) -> None:
    root = _private_root(tmp_path)

    with pytest.raises(
        ValueError, match=_anchored("private directory must remain below its boundary")
    ):
        ensure_private_directory(root, Path("elsewhere"))


@posix_only
def test_ensure_private_directory_creates_nested_directories_as_owner_only(
    tmp_path: Path, open_umask: None
) -> None:
    del open_umask
    root = _private_root(tmp_path)
    target = root / "a" / "b" / "c"

    ensure_private_directory(root, target)

    for directory in (root / "a", root / "a" / "b", target):
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_ensure_private_directory_accepts_the_boundary_itself(tmp_path: Path) -> None:
    root = _private_root(tmp_path)

    ensure_private_directory(root, root)

    assert tuple(root.iterdir()) == ()


@posix_only
def test_ensure_private_directory_accepts_existing_private_directories(
    tmp_path: Path,
) -> None:
    root = _private_root(tmp_path)
    existing = root / "existing"
    existing.mkdir(mode=0o700)
    existing.chmod(0o700)
    marker = existing / "marker.txt"
    marker.write_text("kept\n", encoding="utf-8")

    ensure_private_directory(root, existing / "new")
    ensure_private_directory(root, existing / "new")

    assert (existing / "new").is_dir()
    assert marker.read_text(encoding="utf-8") == "kept\n"


@pytest.mark.parametrize("content", [b"payload", b""], ids=["content", "empty"])
def test_write_file_exclusive_writes_the_content(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "artifact.bin"

    write_file_exclusive(path, content, mode=0o600)

    assert path.read_bytes() == content


@posix_only
@pytest.mark.parametrize("mode", [0o600, 0o640, 0o444])
def test_write_file_exclusive_applies_the_requested_mode(
    tmp_path: Path, open_umask: None, mode: int
) -> None:
    del open_umask
    path = tmp_path / "artifact.bin"

    write_file_exclusive(path, b"x", mode=mode)

    assert stat.S_IMODE(path.stat().st_mode) == mode


@posix_only
def test_write_file_exclusive_refuses_an_existing_path_and_keeps_it_untouched(
    tmp_path: Path,
) -> None:
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"caller-owned")
    path.chmod(0o640)
    before = path.stat()

    with pytest.raises(FileExistsError):
        write_file_exclusive(path, b"replacement", mode=0o600)

    after = path.stat()
    assert path.read_bytes() == b"caller-owned"
    assert stat.S_IMODE(after.st_mode) == 0o640
    assert after.st_mtime_ns == before.st_mtime_ns


def test_write_file_exclusive_refuses_an_existing_directory(tmp_path: Path) -> None:
    path = tmp_path / "artifact"
    path.mkdir()

    with pytest.raises(OSError):
        write_file_exclusive(path, b"x", mode=0o600)

    assert path.is_dir()


@posix_only
def test_write_file_exclusive_refuses_a_dangling_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    path = tmp_path / "artifact.bin"
    path.symlink_to(target)

    with pytest.raises(FileExistsError):
        write_file_exclusive(path, b"x", mode=0o600)

    assert path.is_symlink()
    assert not target.exists()


def test_write_file_exclusive_removes_the_partial_file_when_the_sync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "artifact.bin"
    calls: list[str] = []
    monkeypatch.setattr(os, "fsync", _fail_with(errno.EIO, calls, "fsync"))

    with pytest.raises(OSError) as raised:
        write_file_exclusive(path, b"partial", mode=0o600)

    assert raised.value.errno == errno.EIO
    assert calls == ["fsync"]
    assert not path.exists()


def test_write_file_exclusive_removes_the_partial_file_on_base_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "artifact.bin"

    def interrupt(descriptor: int) -> NoReturn:
        del descriptor
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync", interrupt)

    with pytest.raises(KeyboardInterrupt):
        write_file_exclusive(path, b"partial", mode=0o600)

    assert not path.exists()


def test_write_private_file_exclusive_writes_the_content(tmp_path: Path) -> None:
    path = tmp_path / "secret.bin"

    write_private_file_exclusive(path, b"secret")

    assert path.read_bytes() == b"secret"


@posix_only
def test_write_private_file_exclusive_is_readable_and_writable_by_the_owner_only(
    tmp_path: Path, open_umask: None
) -> None:
    del open_umask
    path = tmp_path / "secret.bin"

    write_private_file_exclusive(path, b"secret")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_private_file_exclusive_refuses_an_existing_path(tmp_path: Path) -> None:
    path = tmp_path / "secret.bin"
    path.write_bytes(b"caller-owned")

    with pytest.raises(FileExistsError):
        write_private_file_exclusive(path, b"replacement")

    assert path.read_bytes() == b"caller-owned"


def test_write_private_file_exclusive_removes_the_partial_file_when_the_sync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secret.bin"
    monkeypatch.setattr(os, "fsync", _fail_with(errno.EIO, [], "fsync"))

    with pytest.raises(OSError):
        write_private_file_exclusive(path, b"partial")

    assert not path.exists()


def test_publish_path_no_replace_moves_a_file(tmp_path: Path) -> None:
    source = tmp_path / ".staging"
    source.write_bytes(b"complete")
    destination = tmp_path / "published"

    publish_path_no_replace(source, destination)

    assert not source.exists()
    assert destination.read_bytes() == b"complete"


def test_publish_path_no_replace_rejects_a_destination_in_a_child_directory(
    tmp_path: Path,
) -> None:
    source, _ = _staged(tmp_path)
    destination = tmp_path / "child" / "published"
    destination.parent.mkdir()

    with pytest.raises(
        ValueError,
        match=_anchored("atomic publication requires sibling source and destination"),
    ):
        publish_path_no_replace(source, destination)

    assert source.is_dir()
    assert not destination.exists()


def test_publish_path_no_replace_requires_the_source_to_exist(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        publish_path_no_replace(tmp_path / ".staging", tmp_path / "published")


def test_publish_path_no_replace_rejects_a_symlink_source_with_its_path(
    tmp_path: Path,
) -> None:
    real_source = tmp_path / "real"
    real_source.mkdir()
    source = tmp_path / ".staging"
    try:
        source.symlink_to(real_source, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(
        OSError,
        match=_anchored(f"publication source must not be a symbolic link: {source}"),
    ):
        publish_path_no_replace(source, tmp_path / "published")

    assert source.is_symlink()
    assert not (tmp_path / "published").exists()


@posix_only
def test_publish_path_no_replace_refuses_a_non_empty_destination_directory(
    tmp_path: Path,
) -> None:
    source, destination = _staged(tmp_path)
    (source / "new.txt").write_text("new\n", encoding="utf-8")
    destination.mkdir()
    (destination / "old.txt").write_text("old\n", encoding="utf-8")

    with pytest.raises(FileExistsError) as raised:
        publish_path_no_replace(source, destination)

    assert raised.value.errno in {errno.EEXIST, errno.ENOTEMPTY}
    assert (source / "new.txt").read_text(encoding="utf-8") == "new\n"
    assert sorted(path.name for path in destination.iterdir()) == ["old.txt"]


@posix_only
@pytest.mark.parametrize("platform", ["sunos5", "freebsd14", "cygwin"])
def test_publish_path_no_replace_reports_unsupported_platforms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    source, destination = _staged(tmp_path)
    monkeypatch.setattr(filesystem_module, "sys", SimpleNamespace(platform=platform))

    with pytest.raises(AtomicPublishUnavailableError) as raised:
        publish_path_no_replace(source, destination)

    assert raised.value.errno == errno.ENOTSUP
    assert raised.value.args == (
        errno.ENOTSUP,
        f"atomic no-replace publication is unsupported on {platform}",
    )
    assert source.is_dir()
    assert not destination.exists()


def test_atomic_publish_unavailable_error_is_an_os_error() -> None:
    assert issubclass(AtomicPublishUnavailableError, OSError)


@pytest.mark.parametrize(
    ("platform", "function", "constant"),
    [
        pytest.param("linux", "renameat2", "RENAME_NOREPLACE", id="linux"),
        pytest.param("darwin", "renamex_np", "RENAME_EXCL", id="darwin"),
    ],
)
def test_publish_path_no_replace_reports_a_libc_without_the_atomic_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    function: str,
    constant: str,
) -> None:
    source, destination = _staged(tmp_path)
    monkeypatch.setattr(filesystem_module, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(filesystem_module, "ctypes", _fake_ctypes(object()))

    with pytest.raises(AtomicPublishUnavailableError) as raised:
        publish_path_no_replace(source, destination)

    assert raised.value.errno == errno.ENOTSUP
    assert raised.value.args == (
        errno.ENOTSUP,
        f"libc does not expose {function} with {constant}",
    )
    assert isinstance(raised.value.__cause__, AttributeError)
    assert source.is_dir()
    assert not destination.exists()


def test_publish_path_no_replace_calls_renameat2_with_no_replace_on_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = _staged(tmp_path)
    rename = _FakeRename(0)
    monkeypatch.setattr(filesystem_module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(
        filesystem_module, "ctypes", _fake_ctypes(SimpleNamespace(renameat2=rename))
    )

    publish_path_no_replace(source, destination)

    assert rename.calls == [
        (-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    ]


def test_publish_path_no_replace_calls_renamex_np_with_rename_excl_on_darwin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = _staged(tmp_path)
    rename = _FakeRename(0)
    monkeypatch.setattr(filesystem_module, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(
        filesystem_module, "ctypes", _fake_ctypes(SimpleNamespace(renamex_np=rename))
    )

    publish_path_no_replace(source, destination)

    assert rename.calls == [(os.fsencode(source), os.fsencode(destination), 4)]


@pytest.mark.parametrize(
    ("platform", "function"),
    [
        pytest.param("linux", "renameat2", id="linux"),
        pytest.param("darwin", "renamex_np", id="darwin"),
    ],
)
@pytest.mark.parametrize(
    ("error_number", "expected_type"),
    [
        pytest.param(errno.EEXIST, FileExistsError, id="EEXIST"),
        pytest.param(errno.ENOTEMPTY, FileExistsError, id="ENOTEMPTY"),
        pytest.param(errno.EXDEV, OSError, id="EXDEV"),
    ],
)
def test_publish_path_no_replace_maps_a_failed_libc_rename_to_the_errno(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    function: str,
    error_number: int,
    expected_type: type[OSError],
) -> None:
    source, destination = _staged(tmp_path)
    library = SimpleNamespace(**{function: _FakeRename(-1)})
    monkeypatch.setattr(filesystem_module, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(
        filesystem_module, "ctypes", _fake_ctypes(library, error_number)
    )

    with pytest.raises(expected_type) as raised:
        publish_path_no_replace(source, destination)

    assert type(raised.value) is expected_type
    assert raised.value.errno == error_number
    assert raised.value.filename == destination


@pytest.mark.parametrize("error_number", [errno.EEXIST, errno.ENOTEMPTY])
def test_raise_rename_error_maps_existing_targets_to_file_exists_error(
    tmp_path: Path, error_number: int
) -> None:
    destination = tmp_path / "published"

    with pytest.raises(FileExistsError) as raised:
        _raise_rename_error(error_number, destination)

    assert raised.value.errno == error_number
    assert raised.value.strerror == os.strerror(error_number)
    assert raised.value.filename == destination


@pytest.mark.parametrize("error_number", [errno.EXDEV, errno.EIO, errno.ENOSPC])
def test_raise_rename_error_maps_other_errnos_to_a_plain_os_error(
    tmp_path: Path, error_number: int
) -> None:
    destination = tmp_path / "published"

    with pytest.raises(OSError) as raised:
        _raise_rename_error(error_number, destination)

    assert type(raised.value) is OSError
    assert raised.value.errno == error_number
    assert raised.value.strerror == os.strerror(error_number)
    assert raised.value.filename == destination


class _Changed(Exception):
    pass


class _OpenFailed(Exception):
    pass


def _recording_close(
    monkeypatch: pytest.MonkeyPatch,
) -> list[int]:
    closed: list[int] = []
    close = os.close

    def record(descriptor: int) -> None:
        closed.append(descriptor)
        close(descriptor)

    monkeypatch.setattr(os, "close", record)
    return closed


def test_open_observed_regular_file_returns_the_observed_file(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(b"{}")
    observed = path.lstat()

    descriptor, opened = open_observed_regular_file(
        path, observed, changed=lambda: _Changed()
    )
    content, final_size = read_and_close(descriptor, limit=None)

    assert (opened.st_dev, opened.st_ino) == (observed.st_dev, observed.st_ino)
    assert opened.st_size == 2
    assert (content, final_size) == (b"{}", 2)


def test_open_observed_regular_file_refuses_a_file_replaced_after_lstat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(b"{}")
    observed = path.lstat()
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(b"{}")
    os.replace(replacement, path)
    closed = _recording_close(monkeypatch)

    with pytest.raises(_Changed):
        open_observed_regular_file(path, observed, changed=lambda: _Changed())

    assert len(closed) == 1


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW")
def test_open_observed_regular_file_does_not_follow_a_symlink_swapped_in(
    tmp_path: Path,
) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(b"{}")
    observed = path.lstat()
    target = tmp_path / "target.json"
    path.rename(target)
    try:
        path.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable: {error}")

    with pytest.raises(OSError) as raised:
        open_observed_regular_file(path, observed, changed=lambda: _Changed())
    assert raised.value.errno == errno.ELOOP

    with pytest.raises(_OpenFailed) as translated:
        open_observed_regular_file(
            path,
            observed,
            changed=lambda: _Changed(),
            open_failed=lambda error: _OpenFailed(str(error.errno)),
        )
    assert isinstance(translated.value.__cause__, OSError)
    assert translated.value.__cause__.errno == errno.ELOOP


def test_read_and_close_reads_one_byte_past_the_limit_and_closes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(b"0123456789")
    descriptor, _ = open_observed_regular_file(
        path, path.lstat(), changed=lambda: _Changed()
    )

    content, final_size = read_and_close(descriptor, limit=4)

    assert (content, final_size) == (b"01234", 10)
    with pytest.raises(OSError) as raised:
        os.fstat(descriptor)
    assert raised.value.errno == errno.EBADF
