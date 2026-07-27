# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest
import rfc8785

from itself import JsonObject, JsonValue, ProtocolValidationError, ProtocolValidator
from itself._json import IJsonError, strict_json_loads

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "conformance" / "canonicalization" / "rfc8785-jsonl-v1.json"


def _fixture() -> JsonObject:
    value = strict_json_loads(VECTORS.read_bytes())
    if not isinstance(value, dict):
        raise TypeError("canonicalization fixture must contain a JSON object")
    return value


def test_rfc8785_vectors_fix_cross_runtime_json_ambiguity() -> None:
    fixture = _fixture()
    vectors = fixture["vectors"]
    if not isinstance(vectors, list):
        raise TypeError("canonicalization vectors must be an array")

    for raw_vector in vectors:
        if not isinstance(raw_vector, dict):
            raise TypeError("each canonicalization vector must be an object")
        expected = raw_vector["canonical"]
        assert isinstance(expected, str)
        assert rfc8785.dumps(raw_vector["value"]) == expected.encode("utf-8")


def test_rfc8785_ledger_digest_vector_is_stable() -> None:
    fixture = _fixture()
    raw_records = fixture["ledger_records"]
    expected_digest = fixture["ledger_sha256"]
    if not isinstance(raw_records, list) or not isinstance(expected_digest, str):
        raise TypeError("canonicalization ledger vector has invalid shape")

    content = b"".join(rfc8785.dumps(record) + b"\n" for record in raw_records)

    assert hashlib.sha256(content).hexdigest() == expected_digest


def test_rfc8785_normalizes_numbers_that_generic_json_encoders_do_not() -> None:
    value: JsonObject = {"negative_zero": -0.0, "whole_float": 1.0}

    assert rfc8785.dumps(value) == b'{"negative_zero":0,"whole_float":1}'
    assert json.dumps(value, sort_keys=True, separators=(",", ":")).encode() != (
        rfc8785.dumps(value)
    )


@pytest.mark.parametrize(
    ("source", "detail"),
    [
        ('{"value":9007199254740992}', "safe range"),
        ('{"value":-0}', "negative zero"),
        ('{"value":1e400}', "not a finite"),
        ('{"value":1e-400}', "underflows"),
        ('{"value":"\\ud800"}', "Unicode surrogate"),
    ],
)
def test_strict_json_rejects_non_interoperable_values(
    source: str,
    detail: str,
) -> None:
    with pytest.raises(IJsonError, match=detail):
        strict_json_loads(source)


def test_programmatic_protocol_records_reject_negative_zero() -> None:
    value = cast(
        JsonValue,
        {
            "protocol_version": "0.1.0-alpha.3",
            "kind": "claim",
            "id": "claim-negative-zero",
            "created_at": "2026-07-25T00:00:00Z",
            "created_by": {
                "id": "test",
                "actor_type": "software",
                "role": "proposer",
            },
            "text": "A value is represented without numeric ambiguity.",
            "status": "proposed",
            "scope": {
                "description": "Canonicalization test",
                "dimensions": {"value": -0.0},
            },
        },
    )

    with pytest.raises(ProtocolValidationError, match="negative zero"):
        ProtocolValidator().validate(value)
