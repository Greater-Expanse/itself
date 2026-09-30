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
from itself._json import IJsonError, ensure_i_json, strict_json_loads

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


def test_strict_json_accepts_the_nesting_limit() -> None:
    source = "[" * 128 + "]" * 128

    assert json.dumps(strict_json_loads(source)) == source


@pytest.mark.parametrize("depth", [129, 10_000, 100_000])
def test_strict_json_rejects_deeper_nesting(depth: int) -> None:
    source = "[" * depth + "]" * depth

    with pytest.raises(IJsonError, match="JSON nesting exceeds") as raised:
        strict_json_loads(source)

    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ or raised.value.__context__ is None


@pytest.mark.parametrize(
    "source",
    [
        b'\xef\xbb\xbf{"value":1}',
        '{"value":1}'.encode("utf-16"),
        '{"value":1}'.encode("utf-16-le"),
        '{"value":1}'.encode("utf-32"),
        bytearray('{"value":1}'.encode("utf-32-be")),
    ],
    ids=["utf-8-bom", "utf-16", "utf-16-le", "utf-32", "utf-32-be-bytearray"],
)
def test_strict_json_bytes_must_be_utf8(source: bytes | bytearray) -> None:
    with pytest.raises(ValueError):
        strict_json_loads(source)


def test_strict_json_decodes_utf8_bytes_and_bytearrays() -> None:
    assert strict_json_loads(b'{"value":"\xc3\xa9"}') == {"value": "\u00e9"}
    assert strict_json_loads(bytearray(b'{"value":1}')) == {"value": 1}


def test_i_json_check_walks_deep_values_without_recursion() -> None:
    value: JsonValue = []
    for _ in range(10_000):
        value = [value]

    ensure_i_json(value)


@pytest.mark.parametrize(
    ("value", "detail"),
    [
        ({"outer": [1, {"inner": float("nan")}]}, r"^\$\.outer\[1\]\.inner: .*finite"),
        ({"first": [float("inf")], "\ud800": 1}, r"^\$\.first\[0\]: .*finite"),
        ({"value": (1, 2)}, r"^\$\.value: tuple is not a JSON value"),
        ({"value": {1, 2}}, r"^\$\.value: set is not a JSON value"),
        ({"value": b"bytes"}, r"^\$\.value: bytes is not a JSON value"),
        ({1: "value"}, r"^\$: object key is not a string"),
    ],
)
def test_i_json_check_reports_the_first_violation_in_document_order(
    value: object,
    detail: str,
) -> None:
    with pytest.raises(IJsonError, match=detail):
        ensure_i_json(cast(JsonValue, value))


def test_protocol_validator_reports_non_json_values_without_crashing() -> None:
    record = cast(JsonValue, {"kind": "claim", "scope": ("tuple", "value")})

    assert ProtocolValidator().errors(record) == [
        "$: $.scope: tuple is not a JSON value"
    ]


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
