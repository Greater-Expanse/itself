# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from jsonschema import Draft202012Validator

from itself import SCHEMA_LINES, SCHEMA_NAMES, SCHEMA_ORIGIN, JsonObject, JsonValue

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "src" / "itself" / "schemas"


def _load_schema(line: str, name: str) -> JsonObject:
    return cast(
        JsonObject,
        json.loads((SCHEMA_ROOT / line / name).read_text(encoding="utf-8")),
    )


def _references(value: JsonValue) -> list[str]:
    if isinstance(value, dict):
        references = [
            child
            for key, child in value.items()
            if key in {"$ref", "$dynamicRef"} and isinstance(child, str)
        ]
        return references + [
            reference for child in value.values() for reference in _references(child)
        ]
    if isinstance(value, list):
        return [reference for child in value for reference in _references(child)]
    return []


def test_published_schema_ids_match_their_public_urls() -> None:
    for line in SCHEMA_LINES:
        for name in SCHEMA_NAMES:
            schema = _load_schema(line, name)
            assert schema["$id"] == (f"{SCHEMA_ORIGIN}/itself/schemas/{line}/{name}")


def test_published_schemas_are_valid_and_self_contained() -> None:
    for line in SCHEMA_LINES:
        for name in SCHEMA_NAMES:
            schema = _load_schema(line, name)
            Draft202012Validator.check_schema(schema)
            assert all(reference.startswith("#") for reference in _references(schema))
