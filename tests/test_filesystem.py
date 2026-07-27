# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from itself._filesystem import (
    ensure_private_directory,
    publish_path_no_replace,
    write_file_exclusive,
)


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
