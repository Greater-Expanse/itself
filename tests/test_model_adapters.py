# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import NoReturn, cast
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request

import pytest

import itself.inference as inference
from experiments.cases.cache_key_diagnostician import (
    CacheKeyFixtureDiagnostician,
    build_cache_key_request,
)
from experiments.cases.cache_key_omission import CaseEnvironment, Mechanism
from experiments.diagnostician import DiagnosisRequest
from experiments.model_adapters import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    DirectoryArtifactSink,
    HttpRequest,
    HttpResponse,
    ModelAdapterError,
    ModelAdapterFailure,
    OpenAIChatDiagnostician,
    OpenAIChatEndpoint,
    OpenAIChatStructuredInvoker,
    StructuredOutputProfile,
    UrllibHttpTransport,
    build_chat_completion_payload,
    build_structured_output_payload,
    diagnosis_output_profile,
    model_assertion_schema,
)
from itself import ActorType, InferenceError, InferenceFailure, JsonObject, JsonValue

CAPTURED_AT = datetime(2026, 7, 22, 15, 30, tzinfo=UTC)
SECRET = "test-secret-that-must-not-leak"


def _request() -> DiagnosisRequest:
    context = CaseEnvironment(
        Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION
    ).diagnostic_context()
    return build_cache_key_request(context)


def _endpoint(
    *,
    actor_id: str = "test-model-diagnostician",
    base_url: str = "https://models.example.test/v1/",
    model: str = "example/model-1",
    api_key_env: str = "TEST_MODEL_API_KEY",
    timeout_seconds: float = 30.0,
    max_output_tokens: int = 1_024,
    stream: bool = False,
    extra_headers: Mapping[str, str] | None = None,
    extra_body: Mapping[str, JsonValue] | None = None,
) -> OpenAIChatEndpoint:
    return OpenAIChatEndpoint(
        actor_id=actor_id,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
        stream=stream,
        extra_headers={} if extra_headers is None else extra_headers,
        extra_body={} if extra_body is None else extra_body,
    )


def _assertion_value() -> JsonObject:
    return (
        CacheKeyFixtureDiagnostician()
        .diagnose(_request())
        .assertion.to_model_json_object()
    )


def _completion_body(
    assertion: JsonValue | None = None,
    *,
    finish_reason: str = "stop",
    refusal: str | None = None,
    include_usage: bool = True,
) -> bytes:
    message: JsonObject = {
        "role": "assistant",
        "content": json.dumps(
            _assertion_value() if assertion is None else assertion,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    }
    if refusal is not None:
        message["refusal"] = refusal
    envelope: JsonObject = {
        "id": "completion-1",
        "model": "example/model-1",
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


@dataclass(frozen=True, slots=True)
class FailingArtifactSink:
    def capture(
        self,
        content: bytes,
        /,
        *,
        media_type: str,
        captured_at: datetime,
    ) -> NoReturn:
        del content, media_type, captured_at
        raise OSError("sensitive storage failure detail")


def _adapter(
    tmp_path: Path,
    body: bytes | None = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> tuple[OpenAIChatDiagnostician, RecordingTransport]:
    transport = RecordingTransport(
        HttpResponse(
            status_code=status_code,
            headers=headers or {"Content-Type": "application/json; charset=utf-8"},
            body=_completion_body() if body is None else body,
        )
    )
    adapter = OpenAIChatDiagnostician(
        endpoint=_endpoint(),
        artifact_sink=DirectoryArtifactSink(tmp_path / "artifacts"),
        transport=transport,
        environment={"TEST_MODEL_API_KEY": SECRET},
        now=lambda: CAPTURED_AT,
        monotonic_ns=SequenceClock([1_000_000_000, 1_125_000_000]),
    )
    return adapter, transport


def _artifact_path(uri: str) -> Path:
    parsed = urlsplit(uri)
    assert parsed.scheme == "file"
    return Path(parsed.path)


def test_adapter_builds_strict_request_and_adapter_owned_result(tmp_path: Path) -> None:
    raw_response = _completion_body()
    adapter, transport = _adapter(tmp_path, raw_response)

    result = adapter.diagnose(_request())

    assert result.diagnostician.actor_id == "test-model-diagnostician"
    assert result.diagnostician.actor_type is ActorType.MODEL
    assert result.diagnostician.adapter_id == ADAPTER_ID
    assert result.diagnostician.adapter_version == ADAPTER_VERSION
    assert result.usage.model_input_tokens == 12
    assert result.usage.model_output_tokens == 7
    assert result.usage.wall_time_ms == 125
    assert result.raw_output_artifact.sha256 == hashlib.sha256(raw_response).hexdigest()
    assert result.raw_output_artifact.captured_at == "2026-07-22T15:30:00Z"

    artifact_path = _artifact_path(result.raw_output_artifact.uri)
    assert artifact_path.read_bytes() == raw_response
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600

    assert len(transport.requests) == 1
    wire_request = transport.requests[0]
    assert wire_request.url == "https://models.example.test/v1/chat/completions"
    assert wire_request.headers["Authorization"] == f"Bearer {SECRET}"
    assert SECRET not in repr(wire_request)
    assert SECRET.encode() not in wire_request.body

    payload = cast(JsonObject, json.loads(wire_request.body))
    assert payload["model"] == "example/model-1"
    assert payload["stream"] is False
    response_format = cast(JsonObject, payload["response_format"])
    schema_wrapper = cast(JsonObject, response_format["json_schema"])
    assert schema_wrapper["strict"] is True
    schema = cast(JsonObject, schema_wrapper["schema"])
    assert "$id" not in schema
    assert "$schema" not in schema


def test_payload_includes_schema_and_request_without_credentials() -> None:
    payload = build_chat_completion_payload(_endpoint(), _request())
    serialized = json.dumps(payload, sort_keys=True)

    assert "DIAGNOSIS REQUEST" in serialized
    assert "OUTPUT SCHEMA" in serialized
    assert "cache_key_omits_source_revision" in serialized
    assert "TEST_MODEL_API_KEY" not in serialized
    assert SECRET not in serialized


def test_diagnosis_payload_is_the_generic_profile_rendering() -> None:
    request = _request()

    assert build_chat_completion_payload(_endpoint(), request) == (
        build_structured_output_payload(
            _endpoint(),
            request.to_json_object(),
            diagnosis_output_profile(),
        )
    )


def test_categorical_observation_profile_constrains_wire_and_local_schema() -> None:
    profile = diagnosis_output_profile(("A", "B"))
    schema = model_assertion_schema(("A", "B"))
    definitions = cast(JsonObject, schema["$defs"])
    prediction = cast(JsonObject, definitions["prediction"])
    properties = cast(JsonObject, prediction["properties"])
    expected_observation = cast(JsonObject, properties["expected_observation"])

    assert profile.profile_id == "diagnosis-assertion-v0.3.0"
    assert expected_observation["enum"] == ["A", "B"]
    assert 'Emit exactly one of ["A","B"]' in profile.system_prompt


def test_categorical_observation_profile_rejects_explanatory_prediction_text(
    tmp_path: Path,
) -> None:
    assertion = _assertion_value()
    predictions = cast(list[JsonValue], assertion["predictions"])
    first = cast(JsonObject, predictions[0])
    first["expected_observation"] = "The observed revision will be B."
    transport = RecordingTransport(
        HttpResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=_completion_body(assertion),
        )
    )
    adapter = OpenAIChatDiagnostician(
        endpoint=_endpoint(),
        artifact_sink=DirectoryArtifactSink(tmp_path / "artifacts"),
        profile=diagnosis_output_profile(("A", "B")),
        transport=transport,
        environment={"TEST_MODEL_API_KEY": SECRET},
        now=lambda: CAPTURED_AT,
        monotonic_ns=SequenceClock([1_000_000_000, 1_125_000_000]),
    )

    with pytest.raises(ModelAdapterError) as captured:
        adapter.diagnose(_request())

    assert captured.value.failure is ModelAdapterFailure.RESPONSE_SCHEMA


@pytest.mark.parametrize("values", [("A", "A"), ("A", ""), (" A", "B")])
def test_categorical_observation_profile_rejects_ambiguous_vocabularies(
    values: tuple[str, str],
) -> None:
    with pytest.raises(ValueError):
        diagnosis_output_profile(values)


def test_legacy_endpoint_preserves_generic_streaming_capability() -> None:
    endpoint = _endpoint(stream=True)

    assert endpoint.as_compatible_endpoint().stream is True
    assert build_chat_completion_payload(endpoint, _request())["stream"] is True


def test_generic_invoker_returns_defensive_json_and_observed_metadata(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport(
        HttpResponse(200, {"Content-Type": "application/json"}, _completion_body())
    )
    invoker = OpenAIChatStructuredInvoker(
        endpoint=_endpoint(),
        artifact_sink=DirectoryArtifactSink(tmp_path / "artifacts"),
        transport=transport,
        environment={"TEST_MODEL_API_KEY": SECRET},
        now=lambda: CAPTURED_AT,
        monotonic_ns=SequenceClock([1_000_000_000, 1_125_000_000]),
    )

    output = invoker.invoke(
        _request().to_json_object(),
        diagnosis_output_profile(),
    )
    value = cast(JsonObject, output.value)
    value["primary_hypothesis_id"] = "tampered"

    assert cast(JsonObject, output.value)["primary_hypothesis_id"] != "tampered"
    assert output.diagnostician.actor_id == "test-model-diagnostician"
    assert output.usage.wall_time_ms == 125
    assert len(transport.requests) == 1


def test_structured_profile_defensively_copies_schema() -> None:
    source: JsonObject = {"type": "object", "additionalProperties": False}
    profile = StructuredOutputProfile(
        profile_id="test-profile",
        schema_name="test_profile",
        system_prompt="Return a test object.",
        user_instruction="Produce the requested object.",
        input_label="TEST INPUT",
        schema=source,
    )
    source["type"] = "array"
    first = profile.schema_json()
    first["type"] = "string"

    assert profile.schema_json()["type"] == "object"


def test_artifact_sink_can_emit_bundle_relative_uri(tmp_path: Path) -> None:
    sink = DirectoryArtifactSink(
        tmp_path / "bundle" / "artifacts",
        uri_prefix="artifacts",
    )

    artifact = sink.capture(
        b'{"private":true}',
        media_type="application/json",
        captured_at=CAPTURED_AT,
    )

    assert artifact.uri == f"artifacts/sha256-{artifact.sha256}.json"
    assert (tmp_path / "bundle" / artifact.uri).read_bytes() == b'{"private":true}'


@pytest.mark.parametrize(
    "prefix",
    ["", "/artifacts", "artifacts/", "artifacts//raw", "../artifacts", "a\\b"],
)
def test_artifact_sink_rejects_unsafe_uri_prefix(
    tmp_path: Path,
    prefix: str,
) -> None:
    with pytest.raises(ValueError, match="uri_prefix"):
        DirectoryArtifactSink(tmp_path / "artifacts", uri_prefix=prefix)


def test_endpoint_defensively_copies_allowed_extensions() -> None:
    headers = {"X-Trace-Mode": "off"}
    body: JsonObject = {"seed": 7}
    endpoint = _endpoint(extra_headers=headers, extra_body=body)
    headers["X-Trace-Mode"] = "changed"
    body["seed"] = 9

    payload = build_chat_completion_payload(endpoint, _request())

    assert endpoint.extra_headers["X-Trace-Mode"] == "off"
    assert payload["seed"] == 7


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _endpoint(actor_id="invalid actor"),
        lambda: _endpoint(base_url="models.example.test/v1"),
        lambda: _endpoint(base_url="https://user:secret@models.example.test/v1"),
        lambda: _endpoint(base_url="https://models.example.test/v1?secret=value"),
        lambda: _endpoint(model=" "),
        lambda: _endpoint(api_key_env="invalid-name"),
        lambda: _endpoint(timeout_seconds=0),
        lambda: _endpoint(max_output_tokens=0),
        lambda: _endpoint(extra_headers={"Authorization": "secret"}),
        lambda: _endpoint(extra_headers={"X-Test": "line\nbreak"}),
        lambda: _endpoint(extra_body={"messages": []}),
        lambda: _endpoint(extra_body={"temperature": float("nan")}),
    ],
)
def test_endpoint_rejects_ambiguous_or_unsafe_configuration(
    factory: Callable[[], OpenAIChatEndpoint],
) -> None:
    with pytest.raises(ValueError):
        factory()


def test_missing_credential_fails_before_transport(tmp_path: Path) -> None:
    adapter, transport = _adapter(tmp_path)
    adapter = OpenAIChatDiagnostician(
        endpoint=adapter.endpoint,
        artifact_sink=adapter.artifact_sink,
        transport=transport,
        environment={},
    )

    with pytest.raises(ModelAdapterError) as captured:
        adapter.diagnose(_request())

    assert captured.value.failure is ModelAdapterFailure.CONFIGURATION
    assert captured.value.artifact is None
    assert not transport.requests
    assert SECRET not in str(captured.value)


@pytest.mark.parametrize(
    ("status_code", "failure", "retryable"),
    [
        (400, ModelAdapterFailure.HTTP_STATUS, False),
        (401, ModelAdapterFailure.HTTP_AUTHENTICATION, False),
        (403, ModelAdapterFailure.HTTP_AUTHORIZATION, False),
        (429, ModelAdapterFailure.HTTP_RATE_LIMIT, True),
        (503, ModelAdapterFailure.HTTP_SERVER, True),
    ],
)
def test_http_failures_are_classified_without_exposing_body(
    tmp_path: Path,
    status_code: int,
    failure: ModelAdapterFailure,
    retryable: bool,
) -> None:
    private_error = b'{"error":"private provider detail"}'
    adapter, _ = _adapter(tmp_path, private_error, status_code=status_code)

    with pytest.raises(ModelAdapterError) as captured:
        adapter.diagnose(_request())

    error = captured.value
    assert error.failure is failure
    assert error.status_code == status_code
    assert error.retryable is retryable
    assert error.artifact is not None
    assert _artifact_path(error.artifact.uri).read_bytes() == private_error
    assert "private provider detail" not in str(error)


@pytest.mark.parametrize(
    ("body", "failure"),
    [
        (_completion_body(finish_reason="length"), ModelAdapterFailure.INCOMPLETE),
        (
            _completion_body(finish_reason="content_filter"),
            ModelAdapterFailure.REFUSAL,
        ),
        (_completion_body(refusal="cannot comply"), ModelAdapterFailure.REFUSAL),
        (b'{"choices":[]}', ModelAdapterFailure.RESPONSE_ENVELOPE),
        (
            _completion_body(include_usage=False),
            ModelAdapterFailure.RESPONSE_USAGE,
        ),
    ],
)
def test_provider_completion_failures_remain_distinct(
    tmp_path: Path,
    body: bytes,
    failure: ModelAdapterFailure,
) -> None:
    adapter, _ = _adapter(tmp_path, body)

    with pytest.raises(ModelAdapterError) as captured:
        adapter.diagnose(_request())

    assert captured.value.failure is failure
    assert captured.value.artifact is not None


@pytest.mark.parametrize(
    "content",
    ["not JSON", '{"duplicate":1,"duplicate":2}'],
)
def test_model_content_requires_one_strict_json_document(
    tmp_path: Path,
    content: str,
) -> None:
    envelope = cast(JsonObject, json.loads(_completion_body()))
    choices = cast(list[JsonValue], envelope["choices"])
    choice = cast(JsonObject, choices[0])
    message = cast(JsonObject, choice["message"])
    message["content"] = content
    adapter, _ = _adapter(tmp_path, json.dumps(envelope).encode())

    with pytest.raises(ModelAdapterError) as captured:
        adapter.diagnose(_request())

    assert captured.value.failure is ModelAdapterFailure.RESPONSE_JSON


@pytest.mark.parametrize(
    ("invalid_kind", "expected_failure"),
    [
        ("authority", ModelAdapterFailure.RESPONSE_SCHEMA),
        ("reference", ModelAdapterFailure.ASSERTION_CONTRACT),
    ],
)
def test_schema_and_semantic_assertion_failures_remain_distinguishable(
    tmp_path: Path,
    invalid_kind: str,
    expected_failure: ModelAdapterFailure,
) -> None:
    assertion = _assertion_value()
    if invalid_kind == "authority":
        assertion["verdict"] = "supported"
    else:
        assertion["selected_test_id"] = "unknown_test"
        predictions = cast(list[JsonValue], assertion["predictions"])
        for item in predictions:
            cast(JsonObject, item)["test_id"] = "unknown_test"
    adapter, _ = _adapter(tmp_path, _completion_body(assertion))

    with pytest.raises(ModelAdapterError) as captured:
        adapter.diagnose(_request())

    assert captured.value.failure is expected_failure


def test_artifact_failure_is_sanitized() -> None:
    transport = RecordingTransport(
        HttpResponse(200, {"Content-Type": "application/json"}, _completion_body())
    )
    adapter = OpenAIChatDiagnostician(
        endpoint=_endpoint(),
        artifact_sink=FailingArtifactSink(),
        transport=transport,
        environment={"TEST_MODEL_API_KEY": SECRET},
    )

    with pytest.raises(ModelAdapterError) as captured:
        adapter.diagnose(_request())

    assert captured.value.failure is ModelAdapterFailure.ARTIFACT
    assert "sensitive storage failure detail" not in str(captured.value)


@dataclass(slots=True)
class FakeUrlResponse:
    status: int
    body: bytes
    headers: Message = field(default_factory=Message)

    def read(self, amount: int = -1) -> bytes:
        return self.body if amount < 0 else self.body[:amount]

    def __enter__(self) -> FakeUrlResponse:
        return self

    def __exit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> None:
        del exception_type, exception, traceback


@dataclass(frozen=True, slots=True)
class FakeUrlOpener:
    callback: Callable[[Request, float], FakeUrlResponse]

    def open(self, request: Request, timeout: float) -> FakeUrlResponse:
        return self.callback(request, timeout)


def _install_fake_url_opener(
    monkeypatch: pytest.MonkeyPatch,
    callback: Callable[[Request, float], FakeUrlResponse],
) -> None:
    def fake_build_opener(*handlers: object) -> FakeUrlOpener:
        assert handlers
        return FakeUrlOpener(callback)

    monkeypatch.setattr(inference, "build_opener", fake_build_opener)


def test_urllib_transport_sends_one_bounded_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[Request, float]] = []
    response = FakeUrlResponse(200, b'{"ok":true}')
    response.headers["Content-Type"] = "application/json"

    def fake_urlopen(request: Request, timeout: float) -> FakeUrlResponse:
        captured.append((request, timeout))
        return response

    _install_fake_url_opener(monkeypatch, fake_urlopen)
    request = HttpRequest(
        "https://models.example.test/v1/chat/completions",
        {"Authorization": "Bearer hidden"},
        b"{}",
        12.5,
    )

    result = UrllibHttpTransport(max_response_bytes=100).send(request)

    assert result.status_code == 200
    assert result.body == b'{"ok":true}'
    assert result.headers["Content-Type"] == "application/json"
    assert len(captured) == 1
    sent, timeout = captured[0]
    assert sent.method == "POST"
    assert sent.full_url == request.url
    assert timeout == 12.5


def test_urllib_transport_bounds_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: Request, timeout: float) -> FakeUrlResponse:
        del request, timeout
        return FakeUrlResponse(200, b"123456")

    _install_fake_url_opener(monkeypatch, fake_urlopen)
    request = HttpRequest(
        "https://models.example.test/v1/chat/completions", {}, b"{}", 1
    )

    with pytest.raises(InferenceError) as captured:
        UrllibHttpTransport(max_response_bytes=5).send(request)

    assert captured.value.failure is InferenceFailure.RESPONSE_TOO_LARGE


def test_urllib_transport_sanitizes_network_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: Request, timeout: float) -> NoReturn:
        del request, timeout
        raise URLError("private network detail")

    _install_fake_url_opener(monkeypatch, fake_urlopen)
    request = HttpRequest(
        "https://models.example.test/v1/chat/completions", {}, b"{}", 1
    )

    with pytest.raises(InferenceError) as captured:
        UrllibHttpTransport().send(request)

    assert captured.value.failure is InferenceFailure.TRANSPORT
    assert captured.value.retryable is True
    assert "private network detail" not in str(captured.value)
