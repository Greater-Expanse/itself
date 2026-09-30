# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Provider-neutral structured inference over OpenAI-compatible endpoints.

The concrete client in this module speaks the widely implemented Chat
Completions wire format.  Its HTTP transport, credential source, artifact sink,
and structured-output behavior are explicit and replaceable.  Model output is
always parsed locally as strict JSON and validated against the caller's schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from enum import StrEnum
from functools import cache
from http.client import (
    HTTPConnection,
    HTTPException,
    HTTPMessage,
    HTTPResponse,
    HTTPSConnection,
    InvalidURL,
)
from pathlib import Path
from types import MappingProxyType
from typing import IO, Final, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    AbstractHTTPHandler,
    BaseHandler,
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from ._endpoints import (
    HEADER_NAME_PATTERN,
    IDENTIFIER_PATTERN,
    content_type,
    defensive_extra_body,
    endpoint_url,
    frozen_extra_headers,
    frozen_query_parameters,
    is_loopback_hostname,
    normalized_base_url,
    normalized_resource_path,
    token_counts,
    validate_deadline,
    validate_timeout,
)
from ._filesystem import publish_path_no_replace
from ._json import strict_json_loads
from .types import JsonObject, JsonValue

ADAPTER_ID: Final = "openai-compatible-chat-completions"
ADAPTER_VERSION: Final = "0.3.1"

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


class InferenceFailure(StrEnum):
    """Stable failure stages for one structured-inference attempt."""

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
    ARTIFACT = "artifact"


_RETRYABLE_FAILURES: Final = frozenset(
    {
        InferenceFailure.TRANSPORT,
        InferenceFailure.HTTP_RATE_LIMIT,
        InferenceFailure.HTTP_SERVER,
    }
)


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
class ArtifactReference:
    """Immutable reference to one captured provider response."""

    uri: str
    media_type: str
    sha256: str
    captured_at: str

    def to_json_object(self) -> JsonObject:
        """Return the protocol-friendly artifact representation."""

        return {
            "uri": self.uri,
            "media_type": self.media_type,
            "digest": {
                "algorithm": "sha256",
                "value": self.sha256,
            },
            "captured_at": self.captured_at,
        }


class InferenceError(RuntimeError):
    """Sanitized failure from one explicit inference attempt."""

    def __init__(
        self,
        failure: InferenceFailure,
        detail: str,
        *,
        status_code: int | None = None,
        artifact: ArtifactReference | None = None,
    ) -> None:
        self.failure: InferenceFailure = failure
        self.detail: str = detail
        self.status_code: int | None = status_code
        self.artifact: ArtifactReference | None = artifact
        super().__init__(f"{failure.value}: {detail}")

    def __reduce__(
        self,
    ) -> tuple[type[InferenceError], tuple[InferenceFailure, str], dict[str, object]]:
        # The default reduction replays only the rendered message, which cannot
        # rebuild an error that requires its failure and detail.
        return (type(self), (self.failure, self.detail), self.__dict__)

    @property
    def retryable(self) -> bool:
        """Whether a later, separately recorded attempt may be reasonable."""

        return self.failure in _RETRYABLE_FAILURES


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
    deadline_seconds: float | None = None

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
    """Provider-neutral instructions and schema for one JSON output."""

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


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """One rendered HTTP request; secret-bearing fields are repr-hidden."""

    url: str
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    timeout_seconds: float
    deadline_seconds: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Bounded HTTP response captured by a transport."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


class HttpTransport(Protocol):
    """Minimal synchronous HTTP boundary used by the compatible client."""

    def send(self, request: HttpRequest, /) -> HttpResponse:
        """Send one request without implicit retry."""

        ...


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


@cache
def _direct_opener() -> OpenerDirector:
    """Return the shared opener for loopback hosts, which never uses a proxy."""

    return build_opener(ProxyHandler({}), _NoRedirectHandler())


class _SocketTracker:
    """One request's connections, shut down together when its deadline passes.

    The tracker holds a duplicate descriptor of each socket from the moment it
    connects.  Shutting a duplicate down ends the shared connection, so the
    deadline also covers a proxy's CONNECT exchange and the TLS handshake, and
    it still reaches the connection after TLS has taken over the original
    socket object.  The lock keeps shutdown and close from racing, so a
    descriptor number is never used after it is released.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self.expired = threading.Event()

    def add(self, connection_socket: socket.socket) -> None:
        with self._lock:
            duplicate = connection_socket.dup()
            self._sockets.append(duplicate)
            # A connection that opens after the deadline is shut down at once.
            if self.expired.is_set():
                _shut_down(duplicate)

    def expire(self) -> None:
        with self._lock:
            self.expired.set()
            for duplicate in self._sockets:
                _shut_down(duplicate)

    def close(self) -> None:
        with self._lock:
            for duplicate in self._sockets:
                duplicate.close()
            self._sockets.clear()


def _shut_down(connection_socket: socket.socket) -> None:
    with suppress(OSError):
        connection_socket.shutdown(socket.SHUT_RDWR)


def _tracked_socket_factory(
    tracker: _SocketTracker,
) -> Callable[[tuple[str, int], float | None, tuple[str, int] | None], socket.socket]:
    def create(
        address: tuple[str, int],
        timeout: float | None,
        source_address: tuple[str, int] | None,
    ) -> socket.socket:
        connection_socket = socket.create_connection(address, timeout, source_address)
        try:
            tracker.add(connection_socket)
        except BaseException:
            connection_socket.close()
            raise
        return connection_socket

    return create


class _TrackedHTTPConnection(HTTPConnection):
    def __init__(
        self,
        host: str,
        *,
        tracker: _SocketTracker,
        port: int | None = None,
        timeout: float | None = None,
        source_address: tuple[str, int] | None = None,
        blocksize: int = 8192,
    ) -> None:
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            source_address=source_address,
            blocksize=blocksize,
        )
        # Every Python release this package supports opens the socket, and
        # any proxy tunnel, through this factory attribute.
        self._create_connection = _tracked_socket_factory(tracker)


class _TrackedHTTPSConnection(HTTPSConnection):
    def __init__(
        self,
        host: str,
        *,
        tracker: _SocketTracker,
        port: int | None = None,
        timeout: float | None = None,
        source_address: tuple[str, int] | None = None,
        blocksize: int = 8192,
    ) -> None:
        # No context is passed, so certificates are trusted exactly as they
        # are for requests without a deadline.
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            source_address=source_address,
            blocksize=blocksize,
        )
        self._create_connection = _tracked_socket_factory(tracker)


class _TrackedHTTPHandler(HTTPHandler):
    def __init__(self, tracker: _SocketTracker) -> None:
        super().__init__()
        self._tracker = tracker

    def http_open(self, req: Request) -> HTTPResponse:
        tracker = self._tracker

        def connection(
            host: str,
            /,
            *,
            port: int | None = None,
            timeout: float | None = None,
            source_address: tuple[str, int] | None = None,
            blocksize: int = 8192,
        ) -> HTTPConnection:
            return _TrackedHTTPConnection(
                host,
                tracker=tracker,
                port=port,
                timeout=timeout,
                source_address=source_address,
                blocksize=blocksize,
            )

        return self.do_open(connection, req)


class _TrackedHTTPSHandler(HTTPSHandler):
    def __init__(self, tracker: _SocketTracker) -> None:
        # HTTPSHandler would load a trust store here for connections that this
        # handler never makes.  Each tracked connection loads its own through
        # the standard hook, so a plain HTTP request loads none.
        AbstractHTTPHandler.__init__(self)
        self._tracker = tracker

    def https_open(self, req: Request) -> HTTPResponse:
        tracker = self._tracker

        def connection(
            host: str,
            /,
            *,
            port: int | None = None,
            timeout: float | None = None,
            source_address: tuple[str, int] | None = None,
            blocksize: int = 8192,
        ) -> HTTPConnection:
            return _TrackedHTTPSConnection(
                host,
                tracker=tracker,
                port=port,
                timeout=timeout,
                source_address=source_address,
                blocksize=blocksize,
            )

        return self.do_open(connection, req)


def _opener(url: str, tracker: _SocketTracker | None = None) -> OpenerDirector:
    loopback = is_loopback_hostname(urlsplit(url).hostname)
    if tracker is None:
        if loopback:
            return _direct_opener()
        # Other hosts honor the proxy environment, which is read on every request.
        return build_opener(_NoRedirectHandler())
    handlers: list[BaseHandler] = [
        _NoRedirectHandler(),
        _TrackedHTTPHandler(tracker),
        _TrackedHTTPSHandler(tracker),
    ]
    if loopback:
        handlers.insert(0, ProxyHandler({}))
    return build_opener(*handlers)


@dataclass(frozen=True, slots=True)
class UrllibHttpTransport:
    """Standard-library transport with bounded reads, no redirect, and no retry."""

    max_response_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if isinstance(self.max_response_bytes, bool) or self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")

    def _read_body(self, response: object) -> bytes:
        read = getattr(response, "read", None)
        if not callable(read):
            raise InferenceError(
                InferenceFailure.TRANSPORT,
                "HTTP response does not expose a readable body",
            )
        body: object = read(self.max_response_bytes + 1)
        if not isinstance(body, bytes):
            raise InferenceError(
                InferenceFailure.TRANSPORT,
                "HTTP response body was not bytes",
            )
        if len(body) > self.max_response_bytes:
            raise InferenceError(
                InferenceFailure.RESPONSE_TOO_LARGE,
                "inference endpoint response exceeded the configured byte limit",
            )
        # A bounded read does not raise when a declared Content-Length is cut
        # short, so the undelivered remainder is checked explicitly.
        remaining: object = getattr(response, "length", None)
        if isinstance(remaining, int) and remaining > 0:
            raise InferenceError(
                InferenceFailure.TRANSPORT,
                "inference endpoint response ended before its declared length",
            )
        return body

    @staticmethod
    def _headers(response: object) -> Mapping[str, str]:
        headers = getattr(response, "headers", None)
        if isinstance(headers, Message):
            items = cast(list[tuple[str, str]], headers.items())
            return dict(items)
        if isinstance(headers, Mapping):
            values = cast(Mapping[object, object], headers)
            return {str(name): str(value) for name, value in values.items()}
        return {}

    def send(self, request: HttpRequest, /) -> HttpResponse:
        """Send one POST and return success or HTTP-error bodies uniformly.

        ``timeout_seconds`` bounds each socket operation, including each
        connection attempt.  When the request carries ``deadline_seconds``, a
        timer also shuts the connection down once that much time has passed,
        however the peer keeps it busy, from the moment the socket connects:
        a proxy's CONNECT exchange and the TLS handshake count against it.
        Host name resolution is bounded by neither setting.
        """

        if urlsplit(request.url).scheme not in {"http", "https"}:
            raise InferenceError(
                InferenceFailure.CONFIGURATION,
                "inference transport requests must use HTTP or HTTPS",
            )
        try:
            validate_timeout(request.timeout_seconds)
            validate_deadline(request.deadline_seconds)
        except ValueError as error:
            raise InferenceError(InferenceFailure.CONFIGURATION, str(error)) from None
        wire_request = Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method="POST",
        )
        if request.deadline_seconds is None:
            return self._exchange(
                _opener(request.url),
                wire_request,
                request.timeout_seconds,
            )

        tracker = _SocketTracker()
        timer = threading.Timer(request.deadline_seconds, tracker.expire)
        timer.daemon = True
        timer.start()
        try:
            response = self._exchange(
                _opener(request.url, tracker),
                wire_request,
                request.timeout_seconds,
            )
        except InferenceError:
            if tracker.expired.is_set():
                raise InferenceError(
                    InferenceFailure.TRANSPORT,
                    "inference endpoint exceeded the request deadline",
                ) from None
            raise
        finally:
            timer.cancel()
            tracker.close()
        # A shut-down connection can still end in a complete-looking response.
        if tracker.expired.is_set():
            raise InferenceError(
                InferenceFailure.TRANSPORT,
                "inference endpoint exceeded the request deadline",
            )
        return response

    def _exchange(
        self,
        opener: OpenerDirector,
        wire_request: Request,
        timeout_seconds: float,
    ) -> HttpResponse:
        try:
            try:
                with opener.open(
                    wire_request,
                    timeout=timeout_seconds,
                ) as response:
                    return HttpResponse(
                        status_code=response.status,
                        headers=self._headers(response),
                        body=self._read_body(response),
                    )
            except HTTPError as error:
                with error:
                    return HttpResponse(
                        status_code=error.code,
                        headers=self._headers(error),
                        body=self._read_body(error),
                    )
        except InvalidURL:
            raise InferenceError(
                InferenceFailure.CONFIGURATION,
                "inference request URL is not a valid HTTP request target",
            ) from None
        except UnicodeError:
            raise InferenceError(
                InferenceFailure.CONFIGURATION,
                "inference request URL or headers cannot be encoded for HTTP",
            ) from None
        except (TimeoutError, URLError, OSError, HTTPException) as error:
            raise InferenceError(
                InferenceFailure.TRANSPORT,
                "inference endpoint request failed",
            ) from error


class ArtifactSink(Protocol):
    """Capture an untrusted provider response under an operator policy."""

    def capture(
        self,
        content: bytes,
        /,
        *,
        media_type: str,
        captured_at: datetime,
    ) -> ArtifactReference:
        """Persist bytes and return their immutable reference and digest."""

        ...


def _require_private_artifact_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OSError("artifact root must be a directory, not a symbolic link")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & (
        stat.S_IRWXG | stat.S_IRWXO
    ):
        raise PermissionError("artifact root must not grant group or other permissions")


def _validate_existing_artifact(path: Path, content: bytes) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OSError("artifact path must be a regular file, not a symlink")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & (
        stat.S_IRWXG | stat.S_IRWXO
    ):
        raise PermissionError("artifact file must not grant group or other permissions")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(opened_metadata.st_mode) or (
            opened_metadata.st_dev,
            opened_metadata.st_ino,
        ) != (metadata.st_dev, metadata.st_ino):
            raise OSError("artifact path changed while opening")
    except BaseException:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "rb") as handle:
        observed = handle.read(len(content) + 1)
        final_size = os.fstat(handle.fileno()).st_size
    if (
        metadata.st_size != len(content)
        or final_size != metadata.st_size
        or observed != content
    ):
        raise OSError("content-addressed artifact digest collision")


@dataclass(frozen=True, slots=True)
class DirectoryArtifactSink:
    """Content-addressed local response sink with private file permissions."""

    root: Path
    uri_prefix: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        if self.uri_prefix is None:
            return
        prefix = self.uri_prefix
        if (
            not prefix
            or prefix != prefix.strip()
            or prefix.startswith("/")
            or prefix.endswith("/")
            or "\\" in prefix
            or any(part in {"", ".", ".."} for part in prefix.split("/"))
        ):
            raise ValueError("uri_prefix must be a normalized relative URI path")
        parsed_prefix = urlsplit(prefix)
        if parsed_prefix.scheme or parsed_prefix.netloc or parsed_prefix.query:
            raise ValueError("uri_prefix must be a normalized relative URI path")
        if parsed_prefix.fragment:
            raise ValueError("uri_prefix must not contain a fragment")

    def capture(
        self,
        content: bytes,
        /,
        *,
        media_type: str,
        captured_at: datetime,
    ) -> ArtifactReference:
        """Atomically capture exact response bytes under their SHA-256 digest."""

        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        normalized_media_type = media_type.strip()
        if not normalized_media_type:
            raise ValueError("media_type must be non-empty")

        digest = hashlib.sha256(content).hexdigest()
        suffix = ".json" if normalized_media_type == "application/json" else ".bin"
        _require_private_artifact_directory(self.root)
        path = self.root / f"sha256-{digest}{suffix}"

        try:
            metadata = path.lstat()
        except FileNotFoundError:
            metadata = None

        if metadata is not None:
            _validate_existing_artifact(path, content)
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.root,
                prefix=".artifact-",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    # Windows has no fchmod before Python 3.13 and no POSIX modes.
                    if os.name == "posix":
                        os.fchmod(handle.fileno(), stat.S_IRUSR | stat.S_IWUSR)
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    publish_path_no_replace(temporary_path, path)
                except FileExistsError:
                    _validate_existing_artifact(path, content)
            finally:
                temporary_path.unlink(missing_ok=True)

        timestamp = captured_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        uri = (
            path.resolve().as_uri()
            if self.uri_prefix is None
            else f"{self.uri_prefix}/{path.name}"
        )
        return ArtifactReference(
            uri=uri,
            media_type=normalized_media_type,
            sha256=digest,
            captured_at=timestamp,
        )


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


def _http_failure(status_code: int) -> InferenceFailure:
    if status_code == 401:
        return InferenceFailure.HTTP_AUTHENTICATION
    if status_code == 403:
        return InferenceFailure.HTTP_AUTHORIZATION
    if status_code == 429:
        return InferenceFailure.HTTP_RATE_LIMIT
    if 500 <= status_code <= 599:
        return InferenceFailure.HTTP_SERVER
    return InferenceFailure.HTTP_STATUS


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
            Draft202012Validator(
                profile.schema_json(),
                format_checker=FormatChecker(),
            ),
        )
        if next(validator.iter_errors(value), None) is not None:
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
