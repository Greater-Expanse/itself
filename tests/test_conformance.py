# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, cast

import pytest
import rfc3987  # type: ignore[import-untyped]
from jsonschema import Draft202012Validator

from itself import JsonObject, JsonValue, ProtocolValidationError, ProtocolValidator
from itself._formats import schema_format_checker
from itself._json import strict_json_loads

ROOT = Path(__file__).resolve().parents[1]
VALID_FIXTURES = sorted((ROOT / "conformance" / "valid").glob("*.json"))
INVALID_FIXTURES = sorted((ROOT / "conformance" / "invalid").glob("*.json"))
ACCEPTED_DOCUMENTS = sorted((ROOT / "conformance" / "json" / "accept").glob("*.json"))
REJECTED_DOCUMENTS = sorted((ROOT / "conformance" / "json" / "reject").glob("*.json"))
URI_REFERENCE_MODULE = ROOT / "conformance" / "javascript" / "uri-reference.mjs"
PROTOCOL_SCHEMA = (
    ROOT / "src" / "itself" / "schemas" / "v0alpha2" / "protocol.schema.json"
)
RECORD_KINDS = (
    "artifact_reference",
    "claim",
    "hypothesis",
    "prediction",
    "test",
    "evidence",
    "verdict",
    "decision",
    "status_transition",
)


class _RootSchemaValidator(Protocol):
    """Typed surface used from the partially typed jsonschema dependency."""

    def is_valid(self, instance: JsonValue) -> bool: ...


def _load(path: Path) -> JsonValue:
    # The JavaScript runner reads fixtures with its strict parser, so Python
    # does too: a fixture either runner would refuse fails here first.
    return strict_json_loads(path.read_bytes())


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


def _fixture_records() -> list[JsonObject]:
    values = [_load(path) for path in VALID_FIXTURES + INVALID_FIXTURES]
    for path in sorted((ROOT / "conformance" / "bundles").rglob("*.json")):
        values.extend(cast(list[JsonValue], _load(path)))
    canonicalization = cast(
        JsonObject,
        _load(ROOT / "conformance" / "canonicalization" / "rfc8785-jsonl-v1.json"),
    )
    values.extend(cast(list[JsonValue], canonicalization["ledger_records"]))
    records: list[JsonObject] = []
    for value in values:
        assert isinstance(value, dict)
        records.append(value)
    return records


def _mutants(record: JsonObject) -> Iterator[JsonObject]:
    for field in record:
        yield {key: value for key, value in record.items() if key != field}
    for kind in RECORD_KINDS:
        yield {**record, "kind": kind}
    yield {**record, "kind": 7}
    yield {**record, "unexpected": True}
    yield {**record, "status": "completed"}
    yield {**record, "evidence_refs": []}


def test_kind_dispatch_accepts_exactly_what_the_root_schema_accepts() -> None:
    root = cast(
        _RootSchemaValidator,
        Draft202012Validator(
            cast(JsonObject, _load(PROTOCOL_SCHEMA)),
            format_checker=schema_format_checker(),
        ),
    )
    validator = ProtocolValidator()
    records = _fixture_records()
    candidates = records + [mutant for record in records for mutant in _mutants(record)]

    outcomes = [
        (not validator.errors(candidate), root.is_valid(candidate))
        for candidate in candidates
    ]

    assert [
        candidate
        for candidate, (dispatched, whole) in zip(candidates, outcomes, strict=True)
        if dispatched != whole
    ] == []
    assert {whole for _, whole in outcomes} == {True, False}


def test_json_document_sets_are_not_empty() -> None:
    assert ACCEPTED_DOCUMENTS
    assert REJECTED_DOCUMENTS


@pytest.mark.parametrize("document", ACCEPTED_DOCUMENTS, ids=lambda path: path.name)
def test_reader_accepts_conformance_document(document: Path) -> None:
    strict_json_loads(document.read_bytes())


@pytest.mark.parametrize("document", REJECTED_DOCUMENTS, ids=lambda path: path.name)
def test_reader_rejects_conformance_document(document: Path) -> None:
    with pytest.raises(ValueError):
        strict_json_loads(document.read_bytes())


def test_javascript_uri_reference_grammar_matches_the_python_checker() -> None:
    source = URI_REFERENCE_MODULE.read_text(encoding="utf-8")
    literals = re.findall(r'^  (".*")(?: \+|;)$', source, flags=re.MULTILINE)
    vendored = "".join(json.loads(literal) for literal in literals)
    installed = cast(
        re.Pattern[str],
        rfc3987.get_compiled_pattern("^%(URI_reference)s$"),  # pyright: ignore[reportUnknownMemberType]
    )

    assert vendored == installed.pattern


def _deployment_policy() -> JsonObject:
    # A rule that a deployment adds on top of the packaged schema, and that
    # some fixture records break.
    return {
        "properties": {
            "created_by": {"properties": {"id": {"not": {"const": "trace-recorder-1"}}}}
        }
    }


@pytest.mark.parametrize("variant", ["root-ref", "branch-sibling", "root-all-of"])
def test_custom_schemas_accept_exactly_what_their_root_accepts(
    tmp_path: Path,
    variant: str,
) -> None:
    schema = cast(JsonObject, _load(PROTOCOL_SCHEMA))
    definitions = cast(JsonObject, schema["$defs"])
    branches = cast(list[JsonValue], schema["oneOf"])
    if variant == "root-ref":
        definitions["deploymentPolicy"] = _deployment_policy()
        schema["$ref"] = "#/$defs/deploymentPolicy"
    elif variant == "branch-sibling":
        branches[0] = {**cast(JsonObject, branches[0]), **_deployment_policy()}
    else:
        definitions["deploymentPolicy"] = _deployment_policy()
        schema["allOf"] = [{"$ref": "#/$defs/deploymentPolicy"}]
    path = tmp_path / "custom.schema.json"
    path.write_text(json.dumps(schema), encoding="utf-8")
    root = cast(
        _RootSchemaValidator,
        Draft202012Validator(schema, format_checker=schema_format_checker()),
    )
    validator = ProtocolValidator(path)
    records = _fixture_records()

    outcomes = [
        (not validator.errors(record), root.is_valid(record)) for record in records
    ]

    assert [
        record["id"]
        for record, (dispatched, whole) in zip(records, outcomes, strict=True)
        if dispatched != whole
    ] == []
    assert {whole for _, whole in outcomes} == {True, False}
