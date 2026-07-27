# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest

import itself.schema_export as schema_export_module
from itself import (
    SCHEMA_LINE,
    SCHEMA_LINES,
    SCHEMA_NAMES,
    SCHEMA_ORIGIN,
    SCHEMA_PUBLIC_ROOT,
    JsonObject,
    JsonValue,
    SchemaExportError,
    export_schemas,
)
from itself._filesystem import publish_path_no_replace
from itself.cli import main

ROOT = Path(__file__).resolve().parents[1]
PACKAGED_SCHEMA_ROOT = ROOT / "src" / "itself" / "schemas"


def _load_object(path: Path) -> JsonObject:
    value = cast(JsonValue, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def test_schema_export_contains_exact_schemas_catalog_and_checksums(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "export"

    snapshot = export_schemas(destination)

    assert snapshot.destination == destination
    assert tuple(schema.name for schema in snapshot.schemas) == tuple(
        name for _line in SCHEMA_LINES for name in SCHEMA_NAMES
    )
    catalog = _load_object(destination / "itself" / "schemas" / "index.json")
    assert catalog == _load_object(
        destination / "itself" / "schemas" / SCHEMA_LINE / "index.json"
    )
    entries = cast(list[JsonValue], catalog["schemas"])
    assert len(entries) == len(SCHEMA_NAMES)

    for line in SCHEMA_LINES:
        checksum_lines: list[str] = []
        for name in SCHEMA_NAMES:
            source = PACKAGED_SCHEMA_ROOT / line / name
            exported = destination / "itself" / "schemas" / line / name
            assert exported.read_bytes() == source.read_bytes()
            assert _load_object(exported)["$id"] == (
                f"{SCHEMA_ORIGIN}/itself/schemas/{line}/{name}"
            )
            checksum_lines.append(
                f"{hashlib.sha256(source.read_bytes()).hexdigest()}  {name}\n"
            )
        assert (destination / "itself" / "schemas" / line / "SHA256SUMS").read_text(
            encoding="utf-8"
        ) == "".join(checksum_lines)

    for name in SCHEMA_NAMES:
        assert (
            _load_object(destination / "itself" / "schemas" / SCHEMA_LINE / name)["$id"]
            == f"{SCHEMA_PUBLIC_ROOT}/{name}"
        )


def test_schema_export_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    export_schemas(first)
    export_schemas(second)

    first_files = sorted(path.relative_to(first) for path in first.rglob("*"))
    second_files = sorted(path.relative_to(second) for path in second.rglob("*"))
    assert first_files == second_files
    for relative_path in first_files:
        if (first / relative_path).is_file():
            assert (first / relative_path).read_bytes() == (
                second / relative_path
            ).read_bytes()


def test_schema_export_refuses_to_overwrite_existing_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "export"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("owned by caller\n", encoding="utf-8")

    with pytest.raises(SchemaExportError, match="already exists"):
        export_schemas(destination)

    assert sentinel.read_text(encoding="utf-8") == "owned by caller\n"


def test_schema_export_refuses_dangling_symbolic_link_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "export"
    try:
        destination.symlink_to(tmp_path / "missing", target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(SchemaExportError, match="already exists"):
        export_schemas(destination)

    assert destination.is_symlink()


def test_schema_export_preserves_destination_created_during_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "export"

    def publish_after_competitor(source: Path, target: Path) -> None:
        target.mkdir()
        publish_path_no_replace(source, target)

    monkeypatch.setattr(
        schema_export_module,
        "publish_path_no_replace",
        publish_after_competitor,
    )

    with pytest.raises(SchemaExportError, match="appeared while exporting"):
        export_schemas(destination)

    assert destination.is_dir()
    assert tuple(destination.iterdir()) == ()
    assert tuple(tmp_path.glob(".export.*")) == ()


def test_cli_exports_canonical_schemas(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "export"

    result = main(["schema", "export", str(destination)])

    captured = capsys.readouterr()
    assert result == 0
    assert f"PASS {destination}: exported 6 canonical schemas" in captured.out
    assert not captured.err
    assert (destination / "itself" / "schemas" / "index.json").is_file()
