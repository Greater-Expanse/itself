# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from itself import JsonValue, ProtocolValidationError, ProtocolValidator

ROOT = Path(__file__).resolve().parents[1]
VALID_FIXTURES = sorted((ROOT / "conformance" / "valid").glob("*.json"))
INVALID_FIXTURES = sorted((ROOT / "conformance" / "invalid").glob("*.json"))


def _load(path: Path) -> JsonValue:
    return cast(JsonValue, json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("fixture", VALID_FIXTURES, ids=lambda path: path.name)
def test_valid_conformance_fixtures(fixture: Path) -> None:
    ProtocolValidator().validate(_load(fixture))


@pytest.mark.parametrize("fixture", INVALID_FIXTURES, ids=lambda path: path.name)
def test_invalid_conformance_fixtures(fixture: Path) -> None:
    with pytest.raises(ProtocolValidationError):
        ProtocolValidator().validate(_load(fixture))


def test_conformance_sets_are_not_empty() -> None:
    assert VALID_FIXTURES
    assert INVALID_FIXTURES
