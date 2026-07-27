# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, cast

import pytest

import itself.inference as inference_module
from itself import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    DirectoryArtifactSink,
    EnvironmentCredential,
    HttpRequest,
    HttpResponse,
    InferenceError,
    InferenceFailure,
    JsonObject,
    JsonValue,
    OpenAICompatibleEndpoint,
    StructuredInferenceAdapter,
    StructuredInferenceClient,
    StructuredOutputMode,
    StructuredOutputProfile,
    build_chat_completions_payload,
)
from itself._filesystem import publish_path_no_replace

CAPTURED_AT = datetime(2026, 7, 23, 12, 30, tzinfo=UTC)
SECRET = "secret-that-must-not-leak"
DEFAULT_CREDENTIAL = EnvironmentCredential("TEST_INFERENCE_KEY")


def _schema() -> JsonObject:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["answer", "supported"],
        "properties": {
            "answer": {"type": "string"},
            "supported": {"type": "boolean"},
        },
    }


def _profile(
    mode: StructuredOutputMode = StructuredOutputMode.JSON_SCHEMA,
) -> StructuredOutputProfile:
    return StructuredOutputProfile(
        profile_id="answer-v1",
        schema_name="answer_v1",
        system_prompt="Return only the requested JSON object.",
        user_instruction="Answer the supplied question.",
        input_label="QUESTION",
        schema=_schema(),
        mode=mode,
    )


def _endpoint(
    *,
    credential: EnvironmentCredential | None = DEFAULT_CREDENTIAL,
    base_url: str = "https://models.example.test/v1/",
    resource_path: str = "chat/completions",
    query_parameters: Mapping[str, str] | None = None,
    max_output_tokens: int | None = 512,
    max_output_tokens_field: str = "max_tokens",
    accepted_finish_reasons: tuple[str, ...] = ("stop",),
    stream: bool = False,
    allow_insecure_http: bool = False,
    extra_headers: Mapping[str, str] | None = None,
    extra_body: Mapping[str, JsonValue] | None = None,
) -> OpenAICompatibleEndpoint:
    return OpenAICompatibleEndpoint(
        actor_id="model-under-test",
        base_url=base_url,
        model="org/model-1",
        credential=credential,
        resource_path=resource_path,
        query_parameters={} if query_parameters is None else query_parameters,
        timeout_seconds=20.0,
        max_output_tokens=max_output_tokens,
        max_output_tokens_field=max_output_tokens_field,
        accepted_finish_reasons=accepted_finish_reasons,
        stream=stream,
        allow_insecure_http=allow_insecure_http,
        extra_headers={} if extra_headers is None else extra_headers,
        extra_body={} if extra_body is None else extra_body,
    )


def _completion_body(
    content: JsonValue | None = None,
    *,
    finish_reason: str = "stop",
    refusal: str | None = None,
    include_usage: bool = True,
) -> bytes:
    model_content: JsonValue = (
        '{"answer":"four","supported":true}' if content is None else content
    )
    message: JsonObject = {
        "role": "assistant",
        "content": model_content,
    }
    if refusal is not None:
        message["refusal"] = refusal
    envelope: JsonObject = {
        "id": "completion-1",
        "model": "resolved/model-1",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": message,
            }
        ],
    }
    if include_usage:
        envelope["usage"] = {
            "prompt_tokens": 12,
            "completion_tokens": 7,
            "total_tokens": 19,
        }
    return json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()


def _sse_body(
    events: list[JsonObject],
    *,
    include_done: bool = True,
) -> bytes:
    body = b"".join(
        b"data: "
        + json.dumps(event, separators=(",", ":"), sort_keys=True).encode()
        + b"\n\n"
        for event in events
    )
    return body + (b"data: [DONE]\n\n" if include_done else b"")


def _streaming_completion_body(
    *,
    finish_reason: str = "stop",
    refusal: str | None = None,
) -> bytes:
    first_delta: JsonObject = {"role": "assistant"}
    if refusal is not None:
        first_delta["refusal"] = refusal
    return _sse_body(
        [
            {
                "id": "completion-1",
                "model": "resolved/model-1",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": None,
                        "delta": first_delta,
                    }
                ],
            },
            {
                "id": "completion-1",
                "model": "resolved/model-1",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": None,
                        "delta": {"content": '{"answer":"four",'},
                    }
                ],
            },
            {
                "id": "completion-1",
                "model": "resolved/model-1",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": None,
                        "delta": {"content": '"supported":true}'},
                    }
                ],
            },
            {
                "id": "completion-1",
                "model": "resolved/model-1",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": finish_reason,
                        "delta": {},
                    }
                ],
            },
            {
                "id": "completion-1",
                "model": "resolved/model-1",
                "choices": [],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 7,
                    "total_tokens": 19,
                },
            },
        ]
    )


def _request_list() -> list[HttpRequest]:
    return []


@dataclass(slots=True)
class RecordingTransport:
    response: HttpResponse
    requests: list[HttpRequest] = field(default_factory=_request_list)

    def send(self, request: HttpRequest, /) -> HttpResponse:
        self.requests.append(request)
        return self.response


@dataclass(slots=True)
class SequenceClock:
    values: list[int]

    def __call__(self) -> int:
        if not self.values:
            raise AssertionError("clock exhausted")
        return self.values.pop(0)


def _client(
    tmp_path: Path,
    *,
    endpoint: OpenAICompatibleEndpoint | None = None,
    body: bytes | None = None,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> tuple[StructuredInferenceClient, RecordingTransport]:
    transport = RecordingTransport(
        HttpResponse(
            status_code=status_code,
            headers=(
                {"Content-Type": "application/json; charset=utf-8"}
                if headers is None
                else headers
            ),
            body=_completion_body() if body is None else body,
        )
    )
    client = StructuredInferenceClient(
        endpoint=_endpoint() if endpoint is None else endpoint,
        artifact_sink=DirectoryArtifactSink(tmp_path / "artifacts"),
        transport=transport,
        environment=(
            {"TEST_INFERENCE_KEY": SECRET} if environment is None else environment
        ),
        now=lambda: CAPTURED_AT,
        monotonic_ns=SequenceClock([1_000_000_000, 1_125_000_000]),
    )
    return client, transport


def test_client_is_assignable_to_provider_neutral_adapter_protocol(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    adapter: StructuredInferenceAdapter = client

    result = adapter.invoke({"question": "What is two plus two?"}, _profile())

    assert result.value == {"answer": "four", "supported": True}


def test_client_builds_one_captured_schema_validated_request(tmp_path: Path) -> None:
    raw_response = _completion_body()
    client, transport = _client(tmp_path, body=raw_response)

    result = client.invoke({"question": "What is two plus two?"}, _profile())

    assert result.identity.actor_id == "model-under-test"
    assert result.identity.adapter_id == ADAPTER_ID
    assert result.identity.adapter_version == ADAPTER_VERSION
    assert result.identity.requested_model == "org/model-1"
    assert result.identity.resolved_model == "resolved/model-1"
    assert result.profile_id == "answer-v1"
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 7
    assert result.usage.total_tokens == 19
    assert result.usage.wall_time_ms == 125
    assert result.raw_response.sha256 == hashlib.sha256(raw_response).hexdigest()
    assert result.raw_response.captured_at == "2026-07-23T12:30:00Z"

    artifact_path = Path(result.raw_response.uri.removeprefix("file://"))
    assert artifact_path.read_bytes() == raw_response
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(artifact_path.parent.stat().st_mode) == 0o700

    assert len(transport.requests) == 1
    wire_request = transport.requests[0]
    assert wire_request.url == "https://models.example.test/v1/chat/completions"
    assert wire_request.headers["Authorization"] == f"Bearer {SECRET}"
    assert SECRET not in repr(wire_request)
    assert SECRET.encode() not in wire_request.body
    payload = cast(JsonObject, json.loads(wire_request.body))
    assert payload["model"] == "org/model-1"
    assert payload["stream"] is False
    assert payload["max_tokens"] == 512
    response_format = cast(JsonObject, payload["response_format"])
    assert response_format["type"] == "json_schema"


def test_client_assembles_one_bounded_captured_sse_response(
    tmp_path: Path,
) -> None:
    raw_response = _streaming_completion_body()
    client, transport = _client(
        tmp_path,
        endpoint=_endpoint(stream=True),
        body=raw_response,
        headers={"Content-Type": "text/event-stream; charset=utf-8"},
    )

    result = client.invoke({"question": "What is two plus two?"}, _profile())

    assert result.value == {"answer": "four", "supported": True}
    assert result.identity.resolved_model == "resolved/model-1"
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 7
    assert result.usage.total_tokens == 19
    assert result.raw_response.sha256 == hashlib.sha256(raw_response).hexdigest()
    artifact_path = Path(result.raw_response.uri.removeprefix("file://"))
    assert artifact_path.read_bytes() == raw_response

    wire_request = transport.requests[0]
    assert wire_request.headers["Accept"] == "text/event-stream"
    payload = cast(JsonObject, json.loads(wire_request.body))
    assert payload["stream"] is True
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "answer_v1",
            "strict": True,
            "schema": _schema(),
        },
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission policy")
def test_artifact_sink_rejects_permissive_existing_root(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o755)
    root.chmod(0o755)

    with pytest.raises(PermissionError, match="group or other permissions"):
        DirectoryArtifactSink(root).capture(
            b"{}",
            media_type="application/json",
            captured_at=CAPTURED_AT,
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission policy")
def test_artifact_sink_rejects_permissive_existing_artifact(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    content = b'{"private":true}'
    artifact = root / f"sha256-{hashlib.sha256(content).hexdigest()}.json"
    artifact.write_bytes(content)
    artifact.chmod(0o644)

    with pytest.raises(PermissionError, match="group or other permissions"):
        DirectoryArtifactSink(root).capture(
            content,
            media_type="application/json",
            captured_at=CAPTURED_AT,
        )


def test_artifact_sink_rejects_symbolic_link_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "artifacts"
    try:
        root.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(OSError, match="symbolic link"):
        DirectoryArtifactSink(root).capture(
            b"{}",
            media_type="application/json",
            captured_at=CAPTURED_AT,
        )


def test_artifact_sink_rejects_symbolic_link_artifact(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    content = b'{"private":true}'
    artifact = root / f"sha256-{hashlib.sha256(content).hexdigest()}.json"
    target = tmp_path / "target.json"
    target.write_bytes(content)
    try:
        artifact.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(OSError, match="symlink"):
        DirectoryArtifactSink(root).capture(
            content,
            media_type="application/json",
            captured_at=CAPTURED_AT,
        )


def test_artifact_sink_accepts_identical_concurrent_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    content = b'{"private":true}'

    def publish_after_competitor(source: Path, destination: Path) -> None:
        destination.write_bytes(content)
        destination.chmod(0o600)
        publish_path_no_replace(source, destination)

    monkeypatch.setattr(
        inference_module,
        "publish_path_no_replace",
        publish_after_competitor,
    )

    reference = DirectoryArtifactSink(root).capture(
        content,
        media_type="application/json",
        captured_at=CAPTURED_AT,
    )

    artifact = Path(reference.uri.removeprefix("file://"))
    assert artifact.read_bytes() == content
    assert tuple(root.glob(".artifact-*.tmp")) == ()


@pytest.mark.parametrize(
    ("mode", "expected_format"),
    [
        (
            StructuredOutputMode.JSON_SCHEMA,
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "answer_v1",
                    "strict": True,
                    "schema": _schema(),
                },
            },
        ),
        (StructuredOutputMode.JSON_OBJECT, {"type": "json_object"}),
        (StructuredOutputMode.PROMPTED_JSON, None),
    ],
)
def test_output_modes_are_explicit_and_always_include_local_schema_prompt(
    mode: StructuredOutputMode,
    expected_format: JsonObject | None,
) -> None:
    payload = build_chat_completions_payload(
        _endpoint(),
        {"question": "test"},
        _profile(mode),
    )
    messages = cast(list[JsonValue], payload["messages"])
    user_message = cast(JsonObject, messages[1])

    assert "OUTPUT SCHEMA" in cast(str, user_message["content"])
    if expected_format is None:
        assert "response_format" not in payload
    else:
        assert payload["response_format"] == expected_format


def test_endpoint_supports_direct_urls_query_parameters_and_token_field() -> None:
    endpoint = _endpoint(
        base_url="https://gateway.example.test/custom/completions",
        resource_path="",
        query_parameters={"api-version": "2026-07-01"},
        max_output_tokens_field="max_completion_tokens",
        extra_headers={"X-Tenant": "tenant-1"},
        extra_body={"seed": 7},
    )

    payload = build_chat_completions_payload(endpoint, {"input": True}, _profile())

    assert endpoint.url == (
        "https://gateway.example.test/custom/completions?api-version=2026-07-01"
    )
    assert endpoint.extra_headers["X-Tenant"] == "tenant-1"
    assert payload["seed"] == 7
    assert payload["max_completion_tokens"] == 512
    assert "max_tokens" not in payload


def test_credential_free_loopback_http_requires_explicit_opt_in() -> None:
    endpoint = _endpoint(
        credential=None,
        base_url="http://127.0.0.1:8080/v1",
        allow_insecure_http=True,
    )

    assert endpoint.url == "http://127.0.0.1:8080/v1/chat/completions"


def test_nested_extra_body_values_cannot_mutate_frozen_endpoint() -> None:
    source: JsonObject = {"metadata": {"tags": ["original"]}}
    endpoint = _endpoint(extra_body=source)

    source_metadata = cast(JsonObject, source["metadata"])
    source_tags = cast(list[JsonValue], source_metadata["tags"])
    source_tags.append("source-mutation")
    exposed_metadata = cast(JsonObject, endpoint.extra_body["metadata"])
    exposed_tags = cast(list[JsonValue], exposed_metadata["tags"])
    exposed_tags.append("returned-value-mutation")

    payload = build_chat_completions_payload(endpoint, {"input": True}, _profile())

    assert payload["metadata"] == {"tags": ["original"]}


@pytest.mark.parametrize(
    ("credential", "expected_name", "expected_value"),
    [
        (
            EnvironmentCredential("TEST_INFERENCE_KEY"),
            "Authorization",
            f"Bearer {SECRET}",
        ),
        (
            EnvironmentCredential(
                "TEST_INFERENCE_KEY",
                header="X-Api-Key",
                prefix="",
            ),
            "X-Api-Key",
            SECRET,
        ),
        (None, None, None),
    ],
)
def test_authentication_is_runtime_configuration_not_provider_code(
    tmp_path: Path,
    credential: EnvironmentCredential | None,
    expected_name: str | None,
    expected_value: str | None,
) -> None:
    client, transport = _client(
        tmp_path,
        endpoint=_endpoint(credential=credential),
    )

    client.invoke({"question": "test"}, _profile())

    headers = transport.requests[0].headers
    if expected_name is None:
        assert "Authorization" not in headers
        assert "X-Api-Key" not in headers
    else:
        assert headers[expected_name] == expected_value


def test_missing_runtime_credential_fails_before_transport(tmp_path: Path) -> None:
    client, transport = _client(tmp_path, environment={})

    with pytest.raises(InferenceError) as captured:
        client.invoke({"question": "test"}, _profile())

    assert captured.value.failure is InferenceFailure.CONFIGURATION
    assert captured.value.artifact is None
    assert not transport.requests
    assert SECRET not in str(captured.value)


def test_missing_usage_and_compatible_text_parts_are_supported(tmp_path: Path) -> None:
    content_parts: JsonValue = [
        {"type": "text", "text": '{"answer":"four",'},
        {"type": "output_text", "text": '"supported":true}'},
    ]
    endpoint = _endpoint(accepted_finish_reasons=("stop", "eos_token"))
    client, _ = _client(
        tmp_path,
        endpoint=endpoint,
        body=_completion_body(
            content_parts,
            finish_reason="eos_token",
            include_usage=False,
        ),
    )

    result = client.invoke({"question": "test"}, _profile())

    assert result.value == {"answer": "four", "supported": True}
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None
    assert result.usage.total_tokens is None


def test_every_output_mode_is_locally_schema_validated(tmp_path: Path) -> None:
    client, _ = _client(
        tmp_path,
        body=_completion_body('{"answer":4,"supported":true}'),
    )

    with pytest.raises(InferenceError) as captured:
        client.invoke(
            {"question": "test"},
            _profile(StructuredOutputMode.PROMPTED_JSON),
        )

    assert captured.value.failure is InferenceFailure.RESPONSE_SCHEMA
    assert captured.value.artifact is not None


@pytest.mark.parametrize(
    "content",
    ["not JSON", '{"answer":"four","answer":"duplicate","supported":true}'],
)
def test_content_must_be_one_strict_json_document(
    tmp_path: Path,
    content: str,
) -> None:
    client, _ = _client(tmp_path, body=_completion_body(content))

    with pytest.raises(InferenceError) as captured:
        client.invoke({"question": "test"}, _profile())

    assert captured.value.failure is InferenceFailure.RESPONSE_JSON


@pytest.mark.parametrize(
    ("status_code", "failure", "retryable"),
    [
        (400, InferenceFailure.HTTP_STATUS, False),
        (401, InferenceFailure.HTTP_AUTHENTICATION, False),
        (403, InferenceFailure.HTTP_AUTHORIZATION, False),
        (429, InferenceFailure.HTTP_RATE_LIMIT, True),
        (503, InferenceFailure.HTTP_SERVER, True),
    ],
)
def test_http_failures_are_classified_without_exposing_response(
    tmp_path: Path,
    status_code: int,
    failure: InferenceFailure,
    retryable: bool,
) -> None:
    private_body = b'{"error":"private provider detail"}'
    client, _ = _client(
        tmp_path,
        body=private_body,
        status_code=status_code,
    )

    with pytest.raises(InferenceError) as captured:
        client.invoke({"question": "test"}, _profile())

    assert captured.value.failure is failure
    assert captured.value.status_code == status_code
    assert captured.value.retryable is retryable
    assert captured.value.artifact is not None
    assert "private provider detail" not in str(captured.value)


@pytest.mark.parametrize(
    ("body", "failure"),
    [
        (_completion_body(finish_reason="length"), InferenceFailure.INCOMPLETE),
        (
            _completion_body(finish_reason="content_filter"),
            InferenceFailure.REFUSAL,
        ),
        (
            _completion_body(content=None, refusal="cannot comply"),
            InferenceFailure.REFUSAL,
        ),
        (b'{"choices":[]}', InferenceFailure.RESPONSE_ENVELOPE),
        (
            _completion_body().replace(
                b'"prompt_tokens":12', b'"prompt_tokens":"twelve"'
            ),
            InferenceFailure.RESPONSE_USAGE,
        ),
    ],
)
def test_completion_failures_remain_distinct(
    tmp_path: Path,
    body: bytes,
    failure: InferenceFailure,
) -> None:
    client, _ = _client(tmp_path, body=body)

    with pytest.raises(InferenceError) as captured:
        client.invoke({"question": "test"}, _profile())

    assert captured.value.failure is failure
    assert captured.value.artifact is not None


@pytest.mark.parametrize(
    ("body", "failure"),
    [
        (
            _streaming_completion_body().removesuffix(b"data: [DONE]\n\n"),
            InferenceFailure.RESPONSE_ENVELOPE,
        ),
        (
            b"data: {not-json}\n\ndata: [DONE]\n\n",
            InferenceFailure.RESPONSE_ENVELOPE,
        ),
        (
            _streaming_completion_body(finish_reason="length"),
            InferenceFailure.INCOMPLETE,
        ),
        (
            _streaming_completion_body(finish_reason="content_filter"),
            InferenceFailure.REFUSAL,
        ),
        (
            _streaming_completion_body(refusal="cannot comply"),
            InferenceFailure.REFUSAL,
        ),
    ],
)
def test_streaming_completion_failures_remain_distinct(
    tmp_path: Path,
    body: bytes,
    failure: InferenceFailure,
) -> None:
    client, _ = _client(
        tmp_path,
        endpoint=_endpoint(stream=True),
        body=body,
        headers={"Content-Type": "text/event-stream"},
    )

    with pytest.raises(InferenceError) as captured:
        client.invoke({"question": "test"}, _profile())

    assert captured.value.failure is failure
    assert captured.value.artifact is not None


def test_streaming_completion_rejects_cross_chunk_model_drift(
    tmp_path: Path,
) -> None:
    body = _streaming_completion_body().replace(
        b'"model":"resolved/model-1"',
        b'"model":"resolved/model-2"',
        1,
    )
    client, _ = _client(
        tmp_path,
        endpoint=_endpoint(stream=True),
        body=body,
        headers={"Content-Type": "text/event-stream"},
    )

    with pytest.raises(InferenceError) as captured:
        client.invoke({"question": "test"}, _profile())

    assert captured.value.failure is InferenceFailure.RESPONSE_ENVELOPE


def test_streaming_completion_rejects_conflicting_usage(tmp_path: Path) -> None:
    events = _streaming_completion_body().removesuffix(b"data: [DONE]\n\n")
    conflicting_usage = _sse_body(
        [
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 8,
                    "total_tokens": 20,
                },
            }
        ]
    )
    body = events + conflicting_usage
    client, _ = _client(
        tmp_path,
        endpoint=_endpoint(stream=True),
        body=body,
        headers={"Content-Type": "text/event-stream"},
    )

    with pytest.raises(InferenceError) as captured:
        client.invoke({"question": "test"}, _profile())

    assert captured.value.failure is InferenceFailure.RESPONSE_USAGE


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _endpoint(base_url="models.example.test/v1"),
        lambda: _endpoint(
            credential=None,
            base_url="http://127.0.0.1:8080/v1",
        ),
        lambda: _endpoint(
            credential=None,
            base_url="http://models.example.test/v1",
            allow_insecure_http=True,
        ),
        lambda: _endpoint(
            base_url="http://127.0.0.1:8080/v1",
            allow_insecure_http=True,
        ),
        lambda: _endpoint(base_url="https://user:secret@models.example.test/v1"),
        lambda: _endpoint(base_url="https://models.example.test/v1?secret=value"),
        lambda: _endpoint(resource_path="../chat/completions"),
        lambda: _endpoint(query_parameters={"api-version": "line\nbreak"}),
        lambda: _endpoint(max_output_tokens=0),
        lambda: _endpoint(accepted_finish_reasons=()),
        lambda: _endpoint(extra_headers={"Authorization": "secret"}),
        lambda: _endpoint(extra_headers={"X-Test": "line\nbreak"}),
        lambda: _endpoint(extra_body={"messages": []}),
        lambda: _endpoint(extra_body={"temperature": float("nan")}),
    ],
)
def test_endpoint_rejects_ambiguous_or_unsafe_configuration(
    factory: Callable[[], OpenAICompatibleEndpoint],
) -> None:
    with pytest.raises(ValueError):
        factory()


@dataclass(frozen=True, slots=True)
class ExplodingTransport:
    def send(self, request: HttpRequest, /) -> NoReturn:
        del request
        raise RuntimeError("private transport failure")


def test_custom_transport_failures_are_sanitized(tmp_path: Path) -> None:
    client = StructuredInferenceClient(
        endpoint=_endpoint(),
        artifact_sink=DirectoryArtifactSink(tmp_path / "artifacts"),
        transport=ExplodingTransport(),
        environment={"TEST_INFERENCE_KEY": SECRET},
    )

    with pytest.raises(InferenceError) as captured:
        client.invoke({"question": "test"}, _profile())

    assert captured.value.failure is InferenceFailure.TRANSPORT
    assert captured.value.retryable is True
    assert "private transport failure" not in str(captured.value)
