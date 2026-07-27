# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Protocol, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from itself import JsonObject, JsonValue

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = ROOT / "experiments" / "contracts" / "v1"
SCHEMA_PATHS = (
    CONTRACT_ROOT / "diagnosis-request.schema.json",
    CONTRACT_ROOT / "diagnosis-assertion.schema.json",
    CONTRACT_ROOT / "diagnosis-result.schema.json",
    CONTRACT_ROOT / "narrative-assertion.schema.json",
    CONTRACT_ROOT / "narrative-result.schema.json",
)


class _ContractValidator(Protocol):
    """Typed surface used from the partially typed jsonschema dependency."""

    def validate(self, instance: JsonValue) -> None: ...


def _load_object(path: Path) -> JsonObject:
    value = cast(JsonValue, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise TypeError(f"fixture {path} must contain a JSON object")
    return value


def _validator(schema_name: str) -> _ContractValidator:
    schema = _load_object(CONTRACT_ROOT / schema_name)
    Draft202012Validator.check_schema(schema)
    return cast(
        _ContractValidator,
        Draft202012Validator(schema, format_checker=FormatChecker()),
    )


@pytest.mark.parametrize("schema_path", SCHEMA_PATHS, ids=lambda path: path.name)
def test_diagnostician_contract_schemas_are_valid(schema_path: Path) -> None:
    Draft202012Validator.check_schema(_load_object(schema_path))


@pytest.mark.parametrize(
    ("schema_name", "example_name"),
    [
        ("diagnosis-request.schema.json", "request.json"),
        ("diagnosis-assertion.schema.json", "assertion.json"),
        ("diagnosis-result.schema.json", "result.json"),
        ("narrative-assertion.schema.json", "narrative-assertion.json"),
        ("narrative-result.schema.json", "narrative-result.json"),
    ],
)
def test_language_neutral_examples_conform(
    schema_name: str,
    example_name: str,
) -> None:
    _validator(schema_name).validate(
        _load_object(CONTRACT_ROOT / "examples" / example_name)
    )


def test_request_rejects_evaluator_only_top_level_ground_truth() -> None:
    request = deepcopy(_load_object(CONTRACT_ROOT / "examples" / "request.json"))
    request["ground_truth"] = "cache_key_omits_source_revision"

    with pytest.raises(ValidationError):
        _validator("diagnosis-request.schema.json").validate(request)


def test_result_cannot_smuggle_an_authoritative_verdict() -> None:
    result = deepcopy(_load_object(CONTRACT_ROOT / "examples" / "result.json"))
    assertion = cast(JsonObject, result["assertion"])
    assertion["verdict"] = "supported"

    with pytest.raises(ValidationError):
        _validator("diagnosis-result.schema.json").validate(result)


@pytest.mark.parametrize(
    "forbidden_field",
    ["verdict", "evidence", "diagnostician", "usage", "raw_output_artifact"],
)
def test_model_assertion_cannot_smuggle_authority_or_provenance(
    forbidden_field: str,
) -> None:
    assertion = deepcopy(_load_object(CONTRACT_ROOT / "examples" / "assertion.json"))
    assertion[forbidden_field] = {}

    with pytest.raises(ValidationError):
        _validator("diagnosis-assertion.schema.json").validate(assertion)


def test_model_assertion_requires_nullable_confidence_field() -> None:
    assertion = deepcopy(_load_object(CONTRACT_ROOT / "examples" / "assertion.json"))
    assertion["expressed_confidence"] = None
    _validator("diagnosis-assertion.schema.json").validate(assertion)

    del assertion["expressed_confidence"]
    with pytest.raises(ValidationError):
        _validator("diagnosis-assertion.schema.json").validate(assertion)


def test_result_carries_an_artifact_reference_not_inline_raw_output() -> None:
    result = deepcopy(_load_object(CONTRACT_ROOT / "examples" / "result.json"))
    artifact = cast(JsonObject, result["raw_output_artifact"])
    artifact["content"] = "private reasoning must not be required here"

    with pytest.raises(ValidationError):
        _validator("diagnosis-result.schema.json").validate(result)
