# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
import random
import re
from collections.abc import Callable, Iterator
from itertools import product
from pathlib import Path
from typing import Protocol, cast

import pytest
from jsonschema import Draft202012Validator
from rfc3986_validator import (  # type: ignore[import-untyped]
    validate_rfc3986,  # pyright: ignore[reportUnknownVariableType]
)

from itself import JsonObject, JsonValue, ProtocolValidationError, ProtocolValidator
from itself._formats import URI_REFERENCE_PATTERN, schema_format_checker
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


def test_javascript_uri_reference_pattern_is_the_reference_pattern() -> None:
    source = URI_REFERENCE_MODULE.read_text(encoding="utf-8")
    literals = re.findall(r'^  (".*")(?: \+|;)$', source, flags=re.MULTILINE)

    assert "".join(json.loads(literal) for literal in literals) == URI_REFERENCE_PATTERN


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://[2001:db8::1]/runs/184/trace.jsonl", True),
        ("//[::ffff:192.0.2.1]/trace.jsonl", True),
        ("//[1:2:3:4:5:6:7::]/trace.jsonl", True),
        ("//[v1.custom-host]/trace.jsonl", True),
        ("//[V1.custom-host]/trace.jsonl", True),
        ("runs/run-184/trace.jsonl", True),
        ("urn:example:trace", True),
        ("", True),
        ("//[1:{,2}1::1:1:1:1]/trace.jsonl", False),
        ("//[::ffff:192.0.02.1]/trace.jsonl", False),
        ("//[1:2:3:4:5:6:7:8:9]/trace.jsonl", False),
        ("//[1::2::3]/trace.jsonl", False),
        ("//host:port/", False),
        ("1a:b", False),
        ("runs/%zz", False),
        ("runs/a b", False),
        ("runs/\u00e9", False),
        ("runs/trace.jsonl\n", False),
    ],
)
def test_uri_reference_checks_rfc_3986(value: str, expected: bool) -> None:
    assert schema_format_checker().conforms(value, "uri-reference") is expected


def _uri_candidates() -> list[str]:
    ipv6 = [
        "::",
        "::1",
        "1:2:3:4:5:6:7:8",
        "1:2:3:4:5:6:7::",
        "::2:3:4:5:6:7:8",
        "1::8",
        "1:2:3:4:5:6:1.2.3.4",
        "::ffff:192.0.2.1",
        "::ffff:192.0.02.1",
        "::ffff:256.0.2.1",
        "1:2:3:4:5:6:7:8:9",
        "1::2::3",
        ":::",
        "12345::",
        "g::",
        "1:{,2}1::1:1:1:1",
        "fe80::1%25en0",
    ]
    hosts = [
        "",
        "example.com",
        "ex%20ample",
        "ex%2",
        "ex ample",
        "192.0.2.1",
        "h_st~!$&'()*+,;=",
        "\u00e9x",
        *(f"[{value}]" for value in ipv6),
        "[v1.x]",
        "[V1.x]",
        "[v.x]",
        "[vG.x]",
        "[]",
    ]
    authorities = [
        "",
        *(
            f"//{user}{host}{port}"
            for user, host, port in product(
                ["", "user:pw@", "u%40@", "u@v@"], hosts, ["", ":", ":80", ":8a"]
            )
        ),
    ]
    tails = list(
        product(
            ["http:", "a+b.c-d:", "1a:", ""],
            ["", "/", "/a/b", "a:b", "%41", "%4", "a b", "a|b"],
            ["", "?", "?a=b&c", "?a/b?c", "?%zz"],
            ["", "#", "#x", "#x#y"],
        )
    )
    generator = random.Random(3986)
    candidates = {f"//{host}/p" for host in hosts}
    for _ in range(8_000):
        scheme, path, query, fragment = generator.choice(tails)
        authority = generator.choice(authorities)
        if authority and path and not path.startswith("/"):
            path = f"/{path}"
        candidates.add(scheme + authority + path + query + fragment)
    return sorted(candidates)


def _rfc_difference(candidate: str, accepted: bool) -> str:
    """Name the rule that RFC 3986 decides unlike rfc3986-validator."""

    if not accepted and re.search(r"\[[^\]]*[:.]0[0-9][^\]]*\]", candidate):
        return "leading zero in an IPv4 octet inside an IPv6 literal"
    if accepted and "[V" in candidate:
        return "uppercase IPvFuture version prefix"
    return f"unexplained: {candidate!r}"


def test_uri_reference_agrees_with_an_independent_checker_except_where_it_errs() -> (
    None
):
    independent = cast(Callable[..., object], validate_rfc3986)
    checker = schema_format_checker()
    differences: set[str] = set()

    for candidate in _uri_candidates():
        accepted = checker.conforms(candidate, "uri-reference")
        # Its anchored pattern's $ also matches before a final newline.
        expected = bool(independent(candidate, rule="URI_reference"))
        expected = expected and not candidate.endswith("\n")
        if accepted != expected:
            differences.add(_rfc_difference(candidate, accepted))

    # RFC 3986 allows no leading zero in a dec-octet, and ABNF strings such as
    # IPvFuture's "v" are case-insensitive; the independent checker errs on both.
    assert differences == {
        "leading zero in an IPv4 octet inside an IPv6 literal",
        "uppercase IPvFuture version prefix",
    }


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
