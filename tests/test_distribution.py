# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import sys
import tomllib
from importlib.metadata import version
from pathlib import Path
from typing import cast

import pytest

from itself import JsonObject, __version__
from itself.cli import main

ROOT = Path(__file__).resolve().parents[1]


def test_distribution_version_declarations_match() -> None:
    document = cast(
        JsonObject,
        tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8")),
    )
    project = cast(JsonObject, document["project"])

    assert project["version"] == __version__
    assert version("itself-sdk") == __version__


def test_cli_reports_distribution_version(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["itself"])
    with pytest.raises(SystemExit) as captured_exit:
        main(["--version"])

    captured = capsys.readouterr()
    assert captured_exit.value.code == 0
    assert captured.out == f"itself {__version__}\n"
    assert not captured.err
