# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

from jsonschema import Draft202012Validator

from itself import SCHEMA_LINES, SCHEMA_NAMES, SCHEMA_ORIGIN, JsonObject, JsonValue

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "src" / "itself" / "schemas"

# Digests of the packaged schemas as published at b6057fe. Published lines are
# immutable: a schema change needs a new line with its own pins, not an edit.
PUBLISHED_SCHEMA_SHA256 = {
    "v0alpha/evidence-bundle.schema.json": (
        "c3a6fbda40cf81d3694d2cdbc28931e2fe0df3e09e0deea753f48961c53a319e"
    ),
    "v0alpha/protocol.schema.json": (
        "19a9e4055df8a53406336c9af9c39c13c5e80694a3d2c7a401772b1a0ba020ba"
    ),
    "v0alpha/reasoning-receipt.schema.json": (
        "642850e72e078c818921c35d57ad2571e486e050d1984dc8c2c44eafa316674c"
    ),
    "v0alpha2/evidence-bundle.schema.json": (
        "1874bd965b916687b12bfafaa4aef4ebd17f82cf4f757a9c867e9d39c2ce11f6"
    ),
    "v0alpha2/protocol.schema.json": (
        "78d66763b958e3c8e4067a3a8aa1146373886d76d5cf28a245ea32ab524f5fb8"
    ),
    "v0alpha2/reasoning-receipt.schema.json": (
        "e30c4d46e9c6ccb10fed29c4ae5e89f317b318f6ff1677e1f415a1e16aa8c15f"
    ),
}


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


def test_packaged_schema_bytes_match_their_published_digests() -> None:
    packaged = {
        path.relative_to(SCHEMA_ROOT).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(SCHEMA_ROOT.rglob("*.json"))
    }

    assert packaged == PUBLISHED_SCHEMA_SHA256
