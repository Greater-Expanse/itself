# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Protocol, cast
from urllib.request import urlopen

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

import itself.evidence_bundle as evidence_bundle_module
import itself.receipts as receipts_module
from itself import (
    EvidenceBundleValidator,
    JsonValue,
    ProtocolValidator,
    ReasoningReceiptValidator,
    StructuredOutputProfile,
)
from itself._schema_references import (
    SchemaReferenceError,
    offline_validator,
    require_local_references,
    unresolved_reference_errors,
)

SchemaFactory = Callable[[str], dict[str, JsonValue]]


class _SchemaValidator(Protocol):
    def iter_errors(self, instance: JsonValue) -> Iterator[ValidationError]: ...


def _offline(schema: Mapping[str, JsonValue]) -> _SchemaValidator:
    return cast(
        _SchemaValidator, offline_validator(schema, format_checker=FormatChecker())
    )


@contextmanager
def _schema_server() -> Generator[tuple[str, list[str]]]:
    """Serve a permissive schema at every path and record each request."""

    requested: list[str] = []

    class SchemaHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requested.append(self.path)
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/schema+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), SchemaHandler)
    thread = Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        # The server answers, so a request any check made would be recorded.
        with urlopen(f"{origin}/reachable.json", timeout=5) as response:
            assert response.read() == b"{}"
        requested.clear()
        yield origin, requested
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


REMOTE_SCHEMAS: Mapping[str, SchemaFactory] = {
    "absolute-ref": lambda origin: {
        "type": "object",
        "properties": {"answer": {"$ref": f"{origin}/answer.json"}},
    },
    "relative-ref-against-remote-id": lambda origin: {
        "$id": f"{origin}/root.json",
        "type": "object",
        "properties": {"answer": {"$ref": "answer.json"}},
    },
    "dynamic-ref": lambda origin: {
        "type": "object",
        "properties": {"answer": {"$dynamicRef": f"{origin}/answer.json#node"}},
    },
    "ref-in-definitions": lambda origin: {
        "$defs": {"answer": {"$ref": f"{origin}/answer.json"}},
        "type": "object",
        "properties": {"answer": {"$ref": "#/$defs/answer"}},
    },
}


def _profile(schema: Mapping[str, JsonValue]) -> StructuredOutputProfile:
    return StructuredOutputProfile(
        profile_id="answer-v1",
        schema_name="answer_v1",
        system_prompt="Return only the requested JSON object.",
        user_instruction="Answer the supplied question.",
        input_label="QUESTION",
        schema=schema,
    )


@pytest.mark.parametrize("factory", REMOTE_SCHEMAS.values(), ids=REMOTE_SCHEMAS)
def test_profile_refuses_a_remote_reference_without_retrieving_it(
    factory: SchemaFactory,
) -> None:
    with (
        _schema_server() as (origin, requested),
        pytest.raises(ValueError, match="resolve every reference within"),
    ):
        _profile(factory(origin))

    assert requested == []


@pytest.mark.parametrize("factory", REMOTE_SCHEMAS.values(), ids=REMOTE_SCHEMAS)
def test_protocol_validator_refuses_a_remote_reference_without_retrieving_it(
    factory: SchemaFactory,
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"
    with _schema_server() as (origin, requested):
        schema_path.write_text(json.dumps(factory(origin)), encoding="utf-8")
        with pytest.raises(ValueError, match="never retrieved"):
            ProtocolValidator(schema_path)

    assert requested == []


@pytest.mark.parametrize("factory", REMOTE_SCHEMAS.values(), ids=REMOTE_SCHEMAS)
def test_offline_validator_never_retrieves_a_reference(
    factory: SchemaFactory,
) -> None:
    with _schema_server() as (origin, requested):
        validator = _offline(factory(origin))
        with pytest.raises(unresolved_reference_errors()):
            list(validator.iter_errors({"answer": "four"}))

    assert requested == []


def test_a_meta_schema_reference_is_refused() -> None:
    schema: dict[str, JsonValue] = {
        "$ref": "https://json-schema.org/draft/2020-12/schema"
    }

    with pytest.raises(SchemaReferenceError, match="never retrieved"):
        require_local_references(schema)


def test_references_within_the_schema_still_resolve() -> None:
    schema: dict[str, JsonValue] = {
        "$id": "urn:example:answer",
        "$defs": {
            "count": {"type": "integer", "minimum": 0},
            "embedded": {
                "$id": "urn:example:embedded",
                "$defs": {"word": {"type": "string"}},
                "type": "string",
            },
        },
        "type": "object",
        "properties": {
            "count": {"$ref": "#/$defs/count"},
            "word": {"$ref": "urn:example:embedded"},
            "nested": {"$ref": "urn:example:embedded#/$defs/word"},
        },
    }
    Draft202012Validator.check_schema(schema)

    require_local_references(schema)
    validator = _offline(schema)

    assert not list(
        validator.iter_errors({"count": 4, "word": "four", "nested": "four"})
    )
    assert len(list(validator.iter_errors({"count": -1, "word": 4}))) == 2


def test_packaged_schemas_compile_once_per_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled: list[JsonValue] = []

    def counting_offline_validator(
        schema: Mapping[str, JsonValue],
        *,
        format_checker: FormatChecker,
    ) -> Draft202012Validator:
        compiled.append(schema.get("$id"))
        return offline_validator(schema, format_checker=format_checker)

    for module in (receipts_module, evidence_bundle_module):
        monkeypatch.setattr(module, "offline_validator", counting_offline_validator)
    receipts_module._receipt_schema_validator.cache_clear()
    evidence_bundle_module._manifest_schema_validator.cache_clear()

    for _ in range(3):
        assert ReasoningReceiptValidator().errors({})
        assert EvidenceBundleValidator().errors({})

    assert len(compiled) == 2
    assert len(set(compiled)) == 2
