# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

ROOT: Final = Path(__file__).resolve().parents[1]
PYTHON_SOURCE_ROOTS: Final[tuple[Path, ...]] = tuple(
    ROOT / directory
    for directory in (
        "evaluations",
        "examples",
        "experiments",
        "src/itself",
        "tests",
        "typing_tests",
    )
)
JAVASCRIPT_SOURCE_ROOT: Final = ROOT / "conformance/javascript"
HEADER_BY_SUFFIX: Final[dict[str, tuple[str, str, str]]] = {
    ".mjs": (
        "// SPDX-License-Identifier: MPL-2.0",
        "// Copyright (C) 2026 Greater Expanse LLC",
        "",
    ),
    ".py": (
        "# SPDX-License-Identifier: MPL-2.0",
        "# Copyright (C) 2026 Greater Expanse LLC",
        "",
    ),
}


def _source_files() -> tuple[Path, ...]:
    paths = {
        path
        for root in PYTHON_SOURCE_ROOTS
        for path in root.rglob("*.py")
        if path.is_file()
    }
    paths.update(
        path
        for path in JAVASCRIPT_SOURCE_ROOT.rglob("*.mjs")
        if path.is_file() and "node_modules" not in path.parts
    )
    return tuple(sorted(paths))


def _case_id(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


@pytest.mark.parametrize("source_path", _source_files(), ids=_case_id)
def test_authored_source_has_per_file_license_notice(source_path: Path) -> None:
    lines = source_path.read_text(encoding="utf-8").splitlines()
    assert lines, f"{_case_id(source_path)} is empty"

    header_start = 1 if lines[0].startswith("#!") else 0
    expected = HEADER_BY_SUFFIX[source_path.suffix]
    actual = tuple(lines[header_start : header_start + len(expected)])

    assert actual == expected
