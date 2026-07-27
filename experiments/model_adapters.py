# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Diagnosis-specific binding over the installable structured-inference adapter."""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, cast

from experiments.diagnostician import (
    CONTRACT_VERSION,
    DiagnosisContractError,
    DiagnosisRequest,
    DiagnosisResult,
    DiagnosisUsage,
    DiagnosticianContractValidator,
    DiagnosticianIdentity,
    RawOutputArtifact,
)
from itself import ActorType, JsonObject, JsonValue
from itself.inference import (
    ArtifactReference,
    ArtifactSink,
    EnvironmentCredential,
    InferenceError,
    InferenceFailure,
    OpenAICompatibleEndpoint,
    StructuredInferenceClient,
    build_chat_completions_payload,
)
from itself.inference import (
    DirectoryArtifactSink as DirectoryArtifactSink,
)
from itself.inference import (
    HttpRequest as HttpRequest,
)
from itself.inference import (
    HttpResponse as HttpResponse,
)
from itself.inference import (
    HttpTransport as HttpTransport,
)
from itself.inference import (
    StructuredOutputProfile as StructuredOutputProfile,
)
from itself.inference import (
    UrllibHttpTransport as UrllibHttpTransport,
)

ADAPTER_ID: Final = "openai-chat-completions"
ADAPTER_VERSION: Final = "0.2.0"
PROTOCOL_ID: Final = "openai-chat-completions"
SCHEMA_NAME: Final = "itself_diagnosis_assertion_v0_2_0"
CONSTRAINED_SCHEMA_NAME: Final = "itself_diagnosis_assertion_v0_3_0"

_CONTRACT_ROOT: Final = Path(__file__).resolve().parent / "contracts" / "v1"
_ASSERTION_SCHEMA_PATH: Final = _CONTRACT_ROOT / "diagnosis-assertion.schema.json"
_IDENTIFIER_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_ENVIRONMENT_NAME_PATTERN: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_HEADERS: Final = frozenset(
    {"accept", "authorization", "content-type", "user-agent"}
)
_RESERVED_BODY_FIELDS: Final = frozenset(
    {"max_tokens", "messages", "model", "response_format", "stream"}
)
_SYSTEM_PROMPT: Final = """You are a causal diagnostician operating under an evidence-bound protocol.

Return only the requested non-authoritative JSON assertion. Use only hypothesis and test identifiers supplied in the request. Retain at least one hypothesis, select exactly one available test, and provide exactly one falsifiable prediction for that selected test for every retained hypothesis.

Do not claim that a test ran. Do not emit evidence, a verdict, a state transition, execution metadata, or private chain-of-thought. The public explanation should state the concise basis for the assertion and why the selected test is useful."""


class ModelAdapterFailure(StrEnum):
    """Stable diagnosis-adapter failure stages retained by trial contracts."""

    CONFIGURATION = "configuration"
    TRANSPORT = "transport"
    RESPONSE_TOO_LARGE = "response_too_large"
    HTTP_AUTHENTICATION = "http_authentication"
    HTTP_AUTHORIZATION = "http_authorization"
    HTTP_RATE_LIMIT = "http_rate_limit"
    HTTP_SERVER = "http_server"
    HTTP_STATUS = "http_status"
    REFUSAL = "refusal"
    INCOMPLETE = "incomplete"
    RESPONSE_ENVELOPE = "response_envelope"
    RESPONSE_JSON = "response_json"
    RESPONSE_SCHEMA = "response_schema"
    RESPONSE_USAGE = "response_usage"
    ASSERTION_CONTRACT = "assertion_contract"
    ARTIFACT = "artifact"


_RETRYABLE_FAILURES: Final = frozenset(
    {
        ModelAdapterFailure.TRANSPORT,
        ModelAdapterFailure.HTTP_RATE_LIMIT,
        ModelAdapterFailure.HTTP_SERVER,
    }
)

_INFERENCE_FAILURE_MAP: Final = {
    InferenceFailure.CONFIGURATION: ModelAdapterFailure.CONFIGURATION,
    InferenceFailure.TRANSPORT: ModelAdapterFailure.TRANSPORT,
    InferenceFailure.RESPONSE_TOO_LARGE: ModelAdapterFailure.RESPONSE_TOO_LARGE,
    InferenceFailure.HTTP_AUTHENTICATION: (ModelAdapterFailure.HTTP_AUTHENTICATION),
    InferenceFailure.HTTP_AUTHORIZATION: ModelAdapterFailure.HTTP_AUTHORIZATION,
    InferenceFailure.HTTP_RATE_LIMIT: ModelAdapterFailure.HTTP_RATE_LIMIT,
    InferenceFailure.HTTP_SERVER: ModelAdapterFailure.HTTP_SERVER,
    InferenceFailure.HTTP_STATUS: ModelAdapterFailure.HTTP_STATUS,
    InferenceFailure.REFUSAL: ModelAdapterFailure.REFUSAL,
    InferenceFailure.INCOMPLETE: ModelAdapterFailure.INCOMPLETE,
    InferenceFailure.RESPONSE_ENVELOPE: ModelAdapterFailure.RESPONSE_ENVELOPE,
    InferenceFailure.RESPONSE_JSON: ModelAdapterFailure.RESPONSE_JSON,
    InferenceFailure.RESPONSE_SCHEMA: ModelAdapterFailure.RESPONSE_SCHEMA,
    InferenceFailure.RESPONSE_USAGE: ModelAdapterFailure.RESPONSE_USAGE,
    InferenceFailure.ARTIFACT: ModelAdapterFailure.ARTIFACT,
}


def _empty_headers() -> Mapping[str, str]:
    return {}


def _empty_body() -> Mapping[str, JsonValue]:
    return {}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _raw_output_artifact(reference: ArtifactReference) -> RawOutputArtifact:
    return RawOutputArtifact(
        uri=reference.uri,
        media_type=reference.media_type,
        sha256=reference.sha256,
        captured_at=reference.captured_at,
    )


class ModelAdapterError(RuntimeError):
    """Sanitized failure for one diagnosis-model invocation attempt."""

    def __init__(
        self,
        failure: ModelAdapterFailure,
        detail: str,
        *,
        status_code: int | None = None,
        artifact: RawOutputArtifact | None = None,
    ) -> None:
        self.failure: ModelAdapterFailure = failure
        self.detail: str = detail
        self.status_code: int | None = status_code
        self.artifact: RawOutputArtifact | None = artifact
        super().__init__(f"{failure.value}: {detail}")

    @property
    def retryable(self) -> bool:
        """Whether a later, separately recorded attempt may be reasonable."""

        return self.failure in _RETRYABLE_FAILURES


def _adapter_error(error: InferenceError) -> ModelAdapterError:
    return ModelAdapterError(
        _INFERENCE_FAILURE_MAP[error.failure],
        error.detail,
        status_code=error.status_code,
        artifact=(
            None if error.artifact is None else _raw_output_artifact(error.artifact)
        ),
    )


@dataclass(frozen=True, slots=True)
class OpenAIChatEndpoint:
    """Legacy trial configuration bound to the generic compatible endpoint."""

    actor_id: str
    base_url: str
    model: str
    api_key_env: str
    timeout_seconds: float = 90.0
    max_output_tokens: int = 2_048
    stream: bool = False
    extra_headers: Mapping[str, str] = field(
        default_factory=_empty_headers,
        repr=False,
    )
    extra_body: Mapping[str, JsonValue] = field(
        default_factory=_empty_body,
        repr=False,
    )

    def __post_init__(self) -> None:
        if _IDENTIFIER_PATTERN.fullmatch(self.actor_id) is None:
            raise ValueError("actor_id must be a valid diagnostician identifier")
        if _ENVIRONMENT_NAME_PATTERN.fullmatch(self.api_key_env) is None:
            raise ValueError("api_key_env must be a valid environment variable name")
        if (
            isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and greater than zero")
        if isinstance(self.max_output_tokens, bool) or self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")

        headers = dict(self.extra_headers)
        for name, value in headers.items():
            if name.lower() in _RESERVED_HEADERS:
                raise ValueError(f"extra_headers cannot override {name!r}")
            if not name or "\r" in name or "\n" in name:
                raise ValueError("extra header names must be non-empty single lines")
            if "\r" in value or "\n" in value:
                raise ValueError("extra header values must be single lines")

        body = deepcopy(dict(self.extra_body))
        reserved_body_fields = sorted(_RESERVED_BODY_FIELDS.intersection(body))
        if reserved_body_fields:
            joined = ", ".join(reserved_body_fields)
            raise ValueError(f"extra_body cannot override reserved fields: {joined}")
        try:
            json.dumps(body, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("extra_body must contain finite JSON values") from error

        compatible = OpenAICompatibleEndpoint(
            actor_id=self.actor_id,
            base_url=self.base_url,
            model=self.model,
            credential=EnvironmentCredential(self.api_key_env),
            timeout_seconds=self.timeout_seconds,
            max_output_tokens=self.max_output_tokens,
            stream=self.stream,
            extra_headers=headers,
            extra_body=body,
        )
        object.__setattr__(self, "base_url", compatible.base_url)
        object.__setattr__(
            self,
            "extra_headers",
            MappingProxyType(dict(compatible.extra_headers)),
        )
        object.__setattr__(
            self,
            "extra_body",
            compatible.extra_body,
        )

    @property
    def chat_completions_url(self) -> str:
        """Return the conventional Chat Completions resource URL."""

        return self.as_compatible_endpoint().url

    def as_compatible_endpoint(self) -> OpenAICompatibleEndpoint:
        """Translate the registered trial configuration to the public adapter."""

        return OpenAICompatibleEndpoint(
            actor_id=self.actor_id,
            base_url=self.base_url,
            model=self.model,
            credential=EnvironmentCredential(self.api_key_env),
            timeout_seconds=self.timeout_seconds,
            max_output_tokens=self.max_output_tokens,
            stream=self.stream,
            extra_headers=self.extra_headers,
            extra_body=self.extra_body,
        )


def _load_model_schema() -> JsonObject:
    value = cast(JsonValue, json.loads(_ASSERTION_SCHEMA_PATH.read_text("utf-8")))
    if not isinstance(value, dict):
        raise TypeError("diagnosis assertion schema must contain an object")
    schema = deepcopy(value)
    for annotation in ("$schema", "$id", "title"):
        schema.pop(annotation, None)
    return schema


def _expected_observation_values(values: Sequence[str]) -> tuple[str, ...]:
    raw_values: Sequence[object] = values
    normalized: list[str] = []
    for value in raw_values:
        if type(value) is not str or not value or value != value.strip():
            raise ValueError(
                "expected observation values must be non-empty strings "
                "without outer space"
            )
        normalized.append(value)
    if len(normalized) != len(set(normalized)):
        raise ValueError("expected observation values must not contain duplicates")
    return tuple(normalized)


def model_assertion_schema(
    expected_observation_values: Sequence[str] = (),
) -> JsonObject:
    """Return a defensive provider-facing copy of the assertion schema."""

    schema = _load_model_schema()
    values = _expected_observation_values(expected_observation_values)
    if not values:
        return schema
    definitions = cast(JsonObject, schema["$defs"])
    prediction = cast(JsonObject, definitions["prediction"])
    properties = cast(JsonObject, prediction["properties"])
    expected_observation = cast(JsonObject, properties["expected_observation"])
    enum_values: list[JsonValue] = [value for value in values]
    expected_observation["enum"] = enum_values
    return schema


def diagnosis_output_profile(
    expected_observation_values: Sequence[str] = (),
) -> StructuredOutputProfile:
    """Return the strict structured-output profile for diagnosis assertions."""

    values = _expected_observation_values(expected_observation_values)
    if values:
        rendered_values = json.dumps(
            values,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        system_prompt = (
            _SYSTEM_PROMPT
            + "\n\nThe expected_observation field is a registered categorical "
            f"code. Emit exactly one of {rendered_values} in that field, with no "
            "additional words. Put explanatory prose in falsified_when or "
            "public_explanation."
        )
        profile_id = "diagnosis-assertion-v0.3.0"
        schema_name = CONSTRAINED_SCHEMA_NAME
    else:
        system_prompt = _SYSTEM_PROMPT
        profile_id = "diagnosis-assertion-v0.2.0"
        schema_name = SCHEMA_NAME
    return StructuredOutputProfile(
        profile_id=profile_id,
        schema_name=schema_name,
        system_prompt=system_prompt,
        user_instruction=(
            "Produce a causal diagnosis assertion for the following request. "
            "Respond only with JSON matching the supplied schema."
        ),
        input_label="DIAGNOSIS REQUEST",
        schema=model_assertion_schema(values),
    )


def build_structured_output_payload(
    endpoint: OpenAIChatEndpoint,
    input_value: JsonValue,
    profile: StructuredOutputProfile,
) -> JsonObject:
    """Render one structured-output request without credentials."""

    return build_chat_completions_payload(
        endpoint.as_compatible_endpoint(),
        input_value,
        profile,
    )


def build_chat_completion_payload(
    endpoint: OpenAIChatEndpoint,
    request: DiagnosisRequest,
) -> JsonObject:
    """Render one deterministic diagnosis request without credentials."""

    DiagnosticianContractValidator().validate_request(request)
    return build_structured_output_payload(
        endpoint,
        request.to_json_object(),
        diagnosis_output_profile(),
    )


@dataclass(frozen=True, slots=True, init=False)
class StructuredModelOutput:
    """One parsed JSON output plus diagnosis-trial provenance and cost."""

    _value: JsonValue = field(repr=False)
    diagnostician: DiagnosticianIdentity
    raw_output_artifact: RawOutputArtifact
    usage: DiagnosisUsage

    def __init__(
        self,
        value: JsonValue,
        diagnostician: DiagnosticianIdentity,
        raw_output_artifact: RawOutputArtifact,
        usage: DiagnosisUsage,
    ) -> None:
        object.__setattr__(self, "_value", deepcopy(value))
        object.__setattr__(self, "diagnostician", diagnostician)
        object.__setattr__(self, "raw_output_artifact", raw_output_artifact)
        object.__setattr__(self, "usage", usage)

    @property
    def value(self) -> JsonValue:
        """Return a defensive copy of the parsed model value."""

        return deepcopy(self._value)


@dataclass(frozen=True, slots=True)
class OpenAIChatStructuredInvoker:
    """Bind the public compatible client to registered diagnosis trial types."""

    endpoint: OpenAIChatEndpoint
    artifact_sink: ArtifactSink
    transport: HttpTransport = field(default_factory=UrllibHttpTransport)
    environment: Mapping[str, str] = field(
        default_factory=lambda: os.environ,
        repr=False,
        compare=False,
    )
    now: Callable[[], datetime] = field(
        default=_utc_now,
        repr=False,
        compare=False,
    )
    monotonic_ns: Callable[[], int] = field(
        default=time.monotonic_ns,
        repr=False,
        compare=False,
    )

    def invoke(
        self,
        input_value: JsonValue,
        profile: StructuredOutputProfile,
    ) -> StructuredModelOutput:
        """Execute one attempt and translate it to trial contract objects."""

        try:
            output = StructuredInferenceClient(
                endpoint=self.endpoint.as_compatible_endpoint(),
                artifact_sink=self.artifact_sink,
                transport=self.transport,
                environment=self.environment,
                now=self.now,
                monotonic_ns=self.monotonic_ns,
            ).invoke(input_value, profile)
        except InferenceError as error:
            raise _adapter_error(error) from None

        artifact = _raw_output_artifact(output.raw_response)
        input_tokens = output.usage.input_tokens
        output_tokens = output.usage.output_tokens
        if input_tokens is None or output_tokens is None:
            raise ModelAdapterError(
                ModelAdapterFailure.RESPONSE_USAGE,
                "provider response omitted valid token usage",
                artifact=artifact,
            )
        return StructuredModelOutput(
            value=output.value,
            diagnostician=DiagnosticianIdentity(
                actor_id=self.endpoint.actor_id,
                actor_type=ActorType.MODEL,
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
            ),
            raw_output_artifact=artifact,
            usage=DiagnosisUsage(
                model_input_tokens=input_tokens,
                model_output_tokens=output_tokens,
                wall_time_ms=output.usage.wall_time_ms,
            ),
        )


@dataclass(frozen=True, slots=True)
class OpenAIChatDiagnostician:
    """Decode one compatible structured completion as a diagnosis assertion."""

    endpoint: OpenAIChatEndpoint
    artifact_sink: ArtifactSink
    profile: StructuredOutputProfile = field(default_factory=diagnosis_output_profile)
    transport: HttpTransport = field(default_factory=UrllibHttpTransport)
    environment: Mapping[str, str] = field(
        default_factory=lambda: os.environ,
        repr=False,
        compare=False,
    )
    now: Callable[[], datetime] = field(
        default=_utc_now,
        repr=False,
        compare=False,
    )
    monotonic_ns: Callable[[], int] = field(
        default=time.monotonic_ns,
        repr=False,
        compare=False,
    )

    def diagnose(self, request: DiagnosisRequest, /) -> DiagnosisResult:
        """Perform one explicit model attempt and return a validated result."""

        validator = DiagnosticianContractValidator()
        validator.validate_request(request)
        output = OpenAIChatStructuredInvoker(
            endpoint=self.endpoint,
            artifact_sink=self.artifact_sink,
            transport=self.transport,
            environment=self.environment,
            now=self.now,
            monotonic_ns=self.monotonic_ns,
        ).invoke(request.to_json_object(), self.profile)

        try:
            assertion = validator.decode_assertion(request, output.value)
        except DiagnosisContractError:
            raise ModelAdapterError(
                ModelAdapterFailure.ASSERTION_CONTRACT,
                "model assertion violated the diagnosis contract",
                artifact=output.raw_output_artifact,
            ) from None

        result = DiagnosisResult(
            contract_version=CONTRACT_VERSION,
            diagnostician=output.diagnostician,
            assertion=assertion,
            raw_output_artifact=output.raw_output_artifact,
            usage=output.usage,
        )
        validator.validate_result(request, result)
        return result
