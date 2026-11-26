# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Provider-neutral structured inference over OpenAI-compatible endpoints.

The concrete client in this module speaks the widely implemented Chat
Completions wire format.  Its HTTP transport, credential source, artifact sink,
and structured-output behavior are explicit and replaceable.  Model output is
always parsed locally as strict JSON and validated against the caller's schema.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

# Path is unused here, but DirectoryArtifactSink keeps this module as its
# __module__, so typing.get_type_hints resolves its fields in this namespace.
from pathlib import Path  # noqa: F401  # pyright: ignore[reportUnusedImport]
from typing import Final, Protocol, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from ._artifact_sinks import ArtifactReference as ArtifactReference
from ._artifact_sinks import ArtifactSink as ArtifactSink
from ._artifact_sinks import DirectoryArtifactSink as DirectoryArtifactSink
from ._endpoints import (
    HEADER_NAME_PATTERN,
    IDENTIFIER_PATTERN,
    content_type,
    defensive_extra_body,
    endpoint_url,
    frozen_extra_headers,
    frozen_query_parameters,
    normalized_base_url,
    normalized_resource_path,
    token_counts,
    validate_deadline,
    validate_timeout,
)
from ._endpoints import (
    utc_now as _utc_now,
)
from ._http_transport import HttpRequest as HttpRequest
from ._http_transport import HttpResponse as HttpResponse
from ._http_transport import HttpTransport as HttpTransport
from ._http_transport import UrllibHttpTransport as UrllibHttpTransport
from ._inference_errors import InferenceError as InferenceError
from ._inference_errors import InferenceFailure as InferenceFailure
from ._inference_errors import http_failure as _http_failure
from ._json import strict_json_loads
from ._schema_references import (
    SchemaReferenceError,
    offline_validator,
    require_local_references,
    unresolved_reference_errors,
)
from .types import JsonObject, JsonValue

ADAPTER_ID: Final = "openai-compatible-chat-completions"
ADAPTER_VERSION: Final = "0.4.0"

_ENVIRONMENT_NAME_PATTERN: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CREDENTIAL_VALUE_PATTERN: Final = re.compile(
    r"[\x21-\x7e](?:[\x20-\x7e]*[\x21-\x7e])?"
)
_RESERVED_BODY_FIELDS: Final = frozenset(
    {"messages", "model", "response_format", "stream"}
)


class StructuredOutputMode(StrEnum):
    """How a compatible endpoint is asked to produce structured output."""

    JSON_SCHEMA = "json_schema"
    JSON_OBJECT = "json_object"
    PROMPTED_JSON = "prompted_json"


def _validated_output_mode(value: object) -> StructuredOutputMode:
    if not isinstance(value, StructuredOutputMode):
        raise ValueError("mode must be a StructuredOutputMode")
    return value


def _empty_headers() -> Mapping[str, str]:
    return {}


def _empty_body() -> Mapping[str, JsonValue]:
    return {}


def _empty_query() -> Mapping[str, str]:
    return {}


def _default_finish_reasons() -> Sequence[str]:
    return ("stop",)


class _SchemaValidator(Protocol):
    """Typed surface used from the partially typed jsonschema dependency."""

    def iter_errors(self, instance: JsonValue) -> Iterator[ValidationError]: ...


@dataclass(frozen=True, slots=True)
class EnvironmentCredential:
    """Load one HTTP credential from an environment variable at call time."""

    variable: str
    header: str = "Authorization"
    prefix: str = "Bearer "

    def __post_init__(self) -> None:
        if _ENVIRONMENT_NAME_PATTERN.fullmatch(self.variable) is None:
            raise ValueError("credential variable must be an environment variable name")
        if HEADER_NAME_PATTERN.fullmatch(self.header) is None:
            raise ValueError("credential header must be a valid HTTP field name")
        if "\r" in self.prefix or "\n" in self.prefix:
            raise ValueError("credential prefix must be a single line")
        if any(not " " <= character <= "~" for character in self.prefix):
            raise ValueError("credential prefix must be printable ASCII")

    def render(self, environment: Mapping[str, str]) -> tuple[str, str]:
        """Resolve the secret only while rendering a request header."""

        value = environment.get(self.variable)
        # A traceback keeps each frame's local variables, which tools that
        # record them would otherwise capture, so the secret and the whole
        # environment are dropped before an error leaves this frame.
        if value is None or not value.strip():
            del value, environment
            raise InferenceError(
                InferenceFailure.CONFIGURATION,
                f"credential environment variable {self.variable!r} "
                "is missing or empty",
            )
        rendered = f"{self.prefix}{value}"
        # http.client would either raise with the secret in its message or send
        # a folded header line, so the value is refused here without echoing it.
        if _CREDENTIAL_VALUE_PATTERN.fullmatch(rendered) is None:
            del value, rendered, environment
            raise InferenceError(
                InferenceFailure.CONFIGURATION,
                f"credential environment variable {self.variable!r} must be "
                "printable ASCII without line breaks or trailing whitespace",
            )
        return self.header, rendered


@dataclass(frozen=True, slots=True)
class OpenAICompatibleEndpoint:
    """One declared Chat Completions endpoint and model configuration."""

    actor_id: str
    base_url: str
    model: str
    credential: EnvironmentCredential | None = None
    resource_path: str = "chat/completions"
    query_parameters: Mapping[str, str] = field(
        default_factory=_empty_query,
        repr=False,
    )
    timeout_seconds: float = 90.0
    max_output_tokens: int | None = 2_048
    max_output_tokens_field: str = "max_tokens"
    accepted_finish_reasons: Sequence[str] = field(
        default_factory=_default_finish_reasons,
    )
    stream: bool = False
    allow_insecure_http: bool = False
    extra_headers: Mapping[str, str] = field(
        default_factory=_empty_headers,
        repr=False,
    )
    extra_body: Mapping[str, JsonValue] = field(
        default_factory=_empty_body,
        repr=False,
    )
    deadline_seconds: float | None = 600.0

    def __post_init__(self) -> None:
        if IDENTIFIER_PATTERN.fullmatch(self.actor_id) is None:
            raise ValueError("actor_id must be a valid identifier")

        object.__setattr__(
            self,
            "base_url",
            normalized_base_url(
                self.base_url,
                credential_bearing=self.credential is not None,
                allow_insecure_http=self.allow_insecure_http,
            ),
        )
        object.__setattr__(
            self,
            "resource_path",
            normalized_resource_path(self.resource_path),
        )

        if not self.model or self.model != self.model.strip():
            raise ValueError("model must be non-empty without outer space")
        validate_timeout(self.timeout_seconds)
        validate_deadline(self.deadline_seconds)
        if self.max_output_tokens is not None and (
            isinstance(self.max_output_tokens, bool) or self.max_output_tokens <= 0
        ):
            raise ValueError("max_output_tokens must be positive or None")
        if IDENTIFIER_PATTERN.fullmatch(self.max_output_tokens_field) is None:
            raise ValueError("max_output_tokens_field must be a valid field name")

        finish_reasons = tuple(self.accepted_finish_reasons)
        if not finish_reasons or any(
            not reason or reason != reason.strip() for reason in finish_reasons
        ):
            raise ValueError("accepted_finish_reasons must contain non-empty values")
        if len(finish_reasons) != len(set(finish_reasons)):
            raise ValueError("accepted_finish_reasons must not contain duplicates")
        object.__setattr__(self, "accepted_finish_reasons", finish_reasons)
        if type(self.stream) is not bool:
            raise ValueError("stream must be a boolean")

        object.__setattr__(
            self,
            "query_parameters",
            frozen_query_parameters(self.query_parameters),
        )
        object.__setattr__(
            self,
            "extra_headers",
            frozen_extra_headers(
                self.extra_headers,
                credential_header=(
                    None if self.credential is None else self.credential.header
                ),
            ),
        )
        reserved_body_fields = set(_RESERVED_BODY_FIELDS)
        if self.max_output_tokens is not None:
            reserved_body_fields.add(self.max_output_tokens_field)
        object.__setattr__(
            self,
            "extra_body",
            defensive_extra_body(self.extra_body, reserved=reserved_body_fields),
        )

    @property
    def url(self) -> str:
        """Return the fully rendered endpoint URL without credentials."""

        return endpoint_url(self.base_url, self.resource_path, self.query_parameters)


@dataclass(frozen=True, slots=True, init=False)
class StructuredOutputProfile:
    """Provider-neutral instructions and schema for one JSON output.

    The schema must resolve every reference within itself: referenced schemas
    are never retrieved, and a schema that needs one raises ``ValueError``.
    """

    profile_id: str
    schema_name: str
    system_prompt: str
    user_instruction: str
    input_label: str
    mode: StructuredOutputMode
    _schema: JsonObject = field(repr=False)

    def __init__(
        self,
        profile_id: str,
        schema_name: str,
        system_prompt: str,
        user_instruction: str,
        input_label: str,
        schema: Mapping[str, JsonValue],
        mode: StructuredOutputMode = StructuredOutputMode.JSON_SCHEMA,
    ) -> None:
        for field_name, value in (
            ("profile_id", profile_id),
            ("schema_name", schema_name),
        ):
            if IDENTIFIER_PATTERN.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a valid identifier")
        for field_name, value in (
            ("system_prompt", system_prompt),
            ("user_instruction", user_instruction),
            ("input_label", input_label),
        ):
            if not value or value != value.strip():
                raise ValueError(f"{field_name} must be non-empty without outer space")
        schema_value = deepcopy(dict(schema))
        try:
            json.dumps(schema_value, allow_nan=False)
            Draft202012Validator.check_schema(schema_value)
        except (TypeError, ValueError, SchemaError) as error:
            raise ValueError(
                "profile schema must be valid finite JSON Schema"
            ) from error
        # Output validation never retrieves a referenced schema.
        try:
            require_local_references(schema_value)
        except SchemaReferenceError as error:
            raise ValueError(
                "profile schema must resolve every reference within itself"
            ) from error
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "schema_name", schema_name)
        object.__setattr__(self, "system_prompt", system_prompt)
        object.__setattr__(self, "user_instruction", user_instruction)
        object.__setattr__(self, "input_label", input_label)
        object.__setattr__(self, "mode", _validated_output_mode(mode))
        object.__setattr__(self, "_schema", schema_value)

    def schema_json(self) -> JsonObject:
        """Return a defensive copy of the output schema."""

        return deepcopy(self._schema)


def build_chat_completions_payload(
    endpoint: OpenAICompatibleEndpoint,
    input_value: JsonValue,
    profile: StructuredOutputProfile,
) -> JsonObject:
    """Render one structured-output request without credentials."""

    schema = profile.schema_json()
    input_json = json.dumps(
        input_value,
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    schema_json = json.dumps(
        schema,
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    user_prompt = (
        f"{profile.user_instruction}\n\n"
        f"{profile.input_label}\n{input_json}\n\n"
        f"OUTPUT SCHEMA\n{schema_json}"
    )
    payload: JsonObject = deepcopy(dict(endpoint.extra_body))
    payload.update(
        {
            "model": endpoint.model,
            "messages": [
                {"role": "system", "content": profile.system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": endpoint.stream,
        }
    )
    if endpoint.max_output_tokens is not None:
        payload[endpoint.max_output_tokens_field] = endpoint.max_output_tokens
    if profile.mode is StructuredOutputMode.JSON_SCHEMA:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": profile.schema_name,
                "strict": True,
                "schema": schema,
            },
        }
    elif profile.mode is StructuredOutputMode.JSON_OBJECT:
        payload["response_format"] = {"type": "json_object"}
    return payload


def _object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        raise TypeError("expected a JSON object")
    return value


def _array(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise TypeError("expected a JSON array")
    return value


def _string(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise TypeError("expected a JSON string")
    return value


@dataclass(frozen=True, slots=True)
class InferenceIdentity:
    """Requested and provider-reported identity for one model invocation."""

    actor_id: str
    adapter_id: str
    adapter_version: str
    requested_model: str
    resolved_model: str | None = None


@dataclass(frozen=True, slots=True)
class InferenceUsage:
    """Provider-reported token counts plus locally measured elapsed time."""

    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    wall_time_ms: int

    def __post_init__(self) -> None:
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("total_tokens", self.total_tokens),
            ("wall_time_ms", self.wall_time_ms),
        ):
            if value is not None and (isinstance(value, bool) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer or None")


@dataclass(frozen=True, slots=True, init=False)
class StructuredInferenceResult:
    """One schema-validated JSON output with observed provenance and cost."""

    _value: JsonValue = field(repr=False)
    identity: InferenceIdentity
    raw_response: ArtifactReference
    usage: InferenceUsage
    profile_id: str

    def __init__(
        self,
        value: JsonValue,
        identity: InferenceIdentity,
        raw_response: ArtifactReference,
        usage: InferenceUsage,
        profile_id: str,
    ) -> None:
        object.__setattr__(self, "_value", deepcopy(value))
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "raw_response", raw_response)
        object.__setattr__(self, "usage", usage)
        object.__setattr__(self, "profile_id", profile_id)

    @property
    def value(self) -> JsonValue:
        """Return a defensive copy of the parsed model value."""

        return deepcopy(self._value)


class StructuredInferenceAdapter(Protocol):
    """Provider-neutral boundary implemented by structured inference adapters."""

    def invoke(
        self,
        input_value: JsonValue,
        profile: StructuredOutputProfile,
    ) -> StructuredInferenceResult:
        """Return one captured and schema-validated structured output."""

        ...


@dataclass(frozen=True, slots=True)
class _Completion:
    content: str
    resolved_model: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


def _message_content(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    parts = _array(value)
    texts: list[str] = []
    for item in parts:
        part = _object(item)
        part_type = part.get("type")
        if part_type not in {"text", "output_text"}:
            raise TypeError("message content contained a non-text part")
        texts.append(_string(part.get("text")))
    if not texts:
        raise TypeError("message content did not contain text")
    return "".join(texts)


def _sse_data_events(body: bytes) -> tuple[str, ...]:
    """Decode one complete bounded SSE response into ordered data fields."""

    text = body.decode("utf-8")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    events: list[str] = []
    for block in normalized.split("\n\n"):
        if not block:
            continue
        data_lines: list[str] = []
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            field, separator, raw_value = line.partition(":")
            value = (
                raw_value[1:] if separator and raw_value.startswith(" ") else raw_value
            )
            if field == "data":
                data_lines.append(value)
        if data_lines:
            events.append("\n".join(data_lines))
    if not events or events[-1] != "[DONE]" or "[DONE]" in events[:-1]:
        raise ValueError("SSE response did not end with one terminal marker")
    return tuple(events[:-1])


@dataclass(frozen=True, slots=True)
class StructuredInferenceClient:
    """Perform one observable JSON call through a compatible endpoint."""

    endpoint: OpenAICompatibleEndpoint
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

    def _request(
        self,
        input_value: JsonValue,
        profile: StructuredOutputProfile,
    ) -> HttpRequest:
        try:
            payload = build_chat_completions_payload(
                self.endpoint,
                input_value,
                profile,
            )
            body = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise InferenceError(
                InferenceFailure.CONFIGURATION,
                "inference input could not be serialized as finite JSON",
            ) from error
        headers = {
            "Accept": (
                "text/event-stream" if self.endpoint.stream else "application/json"
            ),
            "Content-Type": "application/json",
            "User-Agent": f"itself-inference/{ADAPTER_VERSION}",
            **self.endpoint.extra_headers,
        }
        if self.endpoint.credential is not None:
            name, value = self.endpoint.credential.render(self.environment)
            headers[name] = value
        return HttpRequest(
            url=self.endpoint.url,
            headers=headers,
            body=body,
            timeout_seconds=self.endpoint.timeout_seconds,
            deadline_seconds=self.endpoint.deadline_seconds,
        )

    def _capture(
        self,
        response: HttpResponse,
        captured_at: datetime,
    ) -> ArtifactReference:
        try:
            return self.artifact_sink.capture(
                response.body,
                media_type=content_type(response.headers),
                captured_at=captured_at,
            )
        except (OSError, ValueError) as error:
            raise InferenceError(
                InferenceFailure.ARTIFACT,
                "provider response artifact capture failed",
            ) from error

    def _completion(
        self,
        response: HttpResponse,
        artifact: ArtifactReference,
    ) -> _Completion:
        if self.endpoint.stream:
            return self._streaming_completion(response, artifact)
        try:
            envelope = _object(strict_json_loads(response.body))
            choices = _array(envelope["choices"])
            if len(choices) != 1:
                raise TypeError("expected exactly one completion choice")
            choice = _object(choices[0])
            message = _object(choice["message"])
            finish_reason = _string(choice["finish_reason"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError):
            raise InferenceError(
                InferenceFailure.RESPONSE_ENVELOPE,
                "provider returned a malformed Chat Completions envelope",
                artifact=artifact,
            ) from None

        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal:
            raise InferenceError(
                InferenceFailure.REFUSAL,
                "model refused the structured inference request",
                artifact=artifact,
            )
        if finish_reason == "content_filter":
            raise InferenceError(
                InferenceFailure.REFUSAL,
                "provider content filtering stopped the model response",
                artifact=artifact,
            )
        if finish_reason not in self.endpoint.accepted_finish_reasons:
            detail = (
                "model output was truncated"
                if finish_reason == "length"
                else "model completion did not finish in an accepted state"
            )
            raise InferenceError(
                InferenceFailure.INCOMPLETE,
                detail,
                artifact=artifact,
            )

        try:
            content = _message_content(message["content"])
        except (KeyError, TypeError):
            raise InferenceError(
                InferenceFailure.RESPONSE_ENVELOPE,
                "provider completion did not contain one text response",
                artifact=artifact,
            ) from None

        input_tokens: int | None = None
        output_tokens: int | None = None
        total_tokens: int | None = None
        usage_value = envelope.get("usage")
        if usage_value is not None:
            try:
                input_tokens, output_tokens, total_tokens = token_counts(usage_value)
            except TypeError:
                raise InferenceError(
                    InferenceFailure.RESPONSE_USAGE,
                    "provider returned malformed token usage",
                    artifact=artifact,
                ) from None

        resolved_model_value = envelope.get("model")
        resolved_model = (
            resolved_model_value if isinstance(resolved_model_value, str) else None
        )
        return _Completion(
            content=content,
            resolved_model=resolved_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    def _streaming_completion(
        self,
        response: HttpResponse,
        artifact: ArtifactReference,
    ) -> _Completion:
        """Assemble one fully captured OpenAI-compatible SSE completion."""

        try:
            events = _sse_data_events(response.body)
        except (UnicodeError, ValueError):
            raise InferenceError(
                InferenceFailure.RESPONSE_ENVELOPE,
                "provider returned a malformed Chat Completions event stream",
                artifact=artifact,
            ) from None

        content_parts: list[str] = []
        finish_reason: str | None = None
        resolved_model: str | None = None
        usage_counts: tuple[int | None, int | None, int | None] | None = None
        saw_choice = False

        for event in events:
            try:
                chunk = _object(strict_json_loads(event))
                choices = _array(chunk["choices"])
            except (
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
                UnicodeError,
            ):
                raise InferenceError(
                    InferenceFailure.RESPONSE_ENVELOPE,
                    "provider returned a malformed Chat Completions event",
                    artifact=artifact,
                ) from None

            model_value = chunk.get("model")
            if isinstance(model_value, str):
                if resolved_model is not None and model_value != resolved_model:
                    raise InferenceError(
                        InferenceFailure.RESPONSE_ENVELOPE,
                        "provider changed model identity within one event stream",
                        artifact=artifact,
                    )
                resolved_model = model_value

            usage_value = chunk.get("usage")
            if usage_value is not None:
                try:
                    observed_usage = token_counts(usage_value)
                except TypeError:
                    raise InferenceError(
                        InferenceFailure.RESPONSE_USAGE,
                        "provider returned malformed streaming token usage",
                        artifact=artifact,
                    ) from None
                if usage_counts is not None and observed_usage != usage_counts:
                    raise InferenceError(
                        InferenceFailure.RESPONSE_USAGE,
                        "provider changed token usage within one event stream",
                        artifact=artifact,
                    )
                usage_counts = observed_usage

            if not choices:
                continue
            if len(choices) != 1:
                raise InferenceError(
                    InferenceFailure.RESPONSE_ENVELOPE,
                    "provider event did not contain exactly one completion choice",
                    artifact=artifact,
                )
            saw_choice = True
            try:
                choice = _object(choices[0])
                if choice.get("index") != 0:
                    raise TypeError("stream choice index was not zero")
                delta = _object(choice["delta"])
                event_finish_reason = choice.get("finish_reason")
                if event_finish_reason is not None:
                    event_finish_reason = _string(event_finish_reason)
            except (KeyError, TypeError):
                raise InferenceError(
                    InferenceFailure.RESPONSE_ENVELOPE,
                    "provider event contained a malformed completion delta",
                    artifact=artifact,
                ) from None

            refusal = delta.get("refusal")
            if isinstance(refusal, str) and refusal:
                raise InferenceError(
                    InferenceFailure.REFUSAL,
                    "model refused the structured inference request",
                    artifact=artifact,
                )
            content_value = delta.get("content")
            if content_value is not None:
                try:
                    content_parts.append(_message_content(content_value))
                except TypeError:
                    raise InferenceError(
                        InferenceFailure.RESPONSE_ENVELOPE,
                        "provider completion delta did not contain text",
                        artifact=artifact,
                    ) from None

            if event_finish_reason is not None:
                if finish_reason is not None and event_finish_reason != finish_reason:
                    raise InferenceError(
                        InferenceFailure.RESPONSE_ENVELOPE,
                        "provider changed the finish reason within one event stream",
                        artifact=artifact,
                    )
                finish_reason = event_finish_reason

        if not saw_choice or finish_reason is None:
            raise InferenceError(
                InferenceFailure.RESPONSE_ENVELOPE,
                "provider event stream did not contain one finished completion",
                artifact=artifact,
            )
        if finish_reason == "content_filter":
            raise InferenceError(
                InferenceFailure.REFUSAL,
                "provider content filtering stopped the model response",
                artifact=artifact,
            )
        if finish_reason not in self.endpoint.accepted_finish_reasons:
            detail = (
                "model output was truncated"
                if finish_reason == "length"
                else "model completion did not finish in an accepted state"
            )
            raise InferenceError(
                InferenceFailure.INCOMPLETE,
                detail,
                artifact=artifact,
            )
        if not content_parts:
            raise InferenceError(
                InferenceFailure.RESPONSE_ENVELOPE,
                "provider event stream did not contain one text response",
                artifact=artifact,
            )

        input_tokens, output_tokens, total_tokens = (
            (None, None, None) if usage_counts is None else usage_counts
        )
        return _Completion(
            content="".join(content_parts),
            resolved_model=resolved_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    @staticmethod
    def _validate_output(
        value: JsonValue,
        profile: StructuredOutputProfile,
        artifact: ArtifactReference,
    ) -> None:
        validator = cast(
            _SchemaValidator,
            offline_validator(
                profile.schema_json(),
                format_checker=FormatChecker(),
            ),
        )
        try:
            first_error = next(validator.iter_errors(value), None)
        except unresolved_reference_errors():
            raise InferenceError(
                InferenceFailure.CONFIGURATION,
                "profile schema reference cannot be resolved without retrieval",
                artifact=artifact,
            ) from None
        if first_error is not None:
            raise InferenceError(
                InferenceFailure.RESPONSE_SCHEMA,
                "model output did not satisfy the declared JSON Schema",
                artifact=artifact,
            )

    def invoke(
        self,
        input_value: JsonValue,
        profile: StructuredOutputProfile,
    ) -> StructuredInferenceResult:
        """Execute, capture, strictly parse, and schema-validate one attempt."""

        http_request = self._request(input_value, profile)
        started_at = self.monotonic_ns()
        try:
            response = self.transport.send(http_request)
        except InferenceError:
            raise
        except Exception as error:
            raise InferenceError(
                InferenceFailure.TRANSPORT,
                "custom inference transport failed",
            ) from error
        elapsed_ns = max(0, self.monotonic_ns() - started_at)
        artifact = self._capture(response, self.now())

        if not 200 <= response.status_code <= 299:
            raise InferenceError(
                _http_failure(response.status_code),
                f"inference endpoint returned HTTP {response.status_code}",
                status_code=response.status_code,
                artifact=artifact,
            )

        completion = self._completion(response, artifact)
        try:
            value = strict_json_loads(completion.content)
        except (ValueError, json.JSONDecodeError, UnicodeError):
            raise InferenceError(
                InferenceFailure.RESPONSE_JSON,
                "model content was not one strict JSON document",
                artifact=artifact,
            ) from None
        self._validate_output(value, profile, artifact)

        return StructuredInferenceResult(
            value=value,
            identity=InferenceIdentity(
                actor_id=self.endpoint.actor_id,
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                requested_model=self.endpoint.model,
                resolved_model=completion.resolved_model,
            ),
            raw_response=artifact,
            usage=InferenceUsage(
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                total_tokens=completion.total_tokens,
                wall_time_ms=elapsed_ns // 1_000_000,
            ),
            profile_id=profile.profile_id,
        )
