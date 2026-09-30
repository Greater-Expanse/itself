# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Endpoint configuration checks and response helpers shared by model adapters."""

from __future__ import annotations

import ipaddress
import json
import math
import re
from collections.abc import Iterator, Mapping, Set
from copy import deepcopy
from types import MappingProxyType
from typing import Final
from urllib.parse import urlencode, urlsplit

from .types import JsonObject, JsonValue

IDENTIFIER_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
HEADER_NAME_PATTERN: Final = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_HEADER_VALUE_PATTERN: Final = re.compile(r"[\t\x20-\x7e]*")
RESERVED_HEADERS: Final = frozenset({"accept", "content-type", "user-agent"})


class DefensiveJsonObject(Mapping[str, JsonValue]):
    """Read-only mapping whose nested values are returned as defensive copies."""

    def __init__(self, value: Mapping[str, JsonValue]) -> None:
        self._value: JsonObject = deepcopy(dict(value))

    def __getitem__(self, key: str) -> JsonValue:
        return deepcopy(self._value[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self._value)

    def __len__(self) -> int:
        return len(self._value)


def is_loopback_hostname(hostname: str | None) -> bool:
    if hostname is None:
        return False
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def normalized_base_url(
    base_url: str,
    *,
    credential_bearing: bool,
    allow_insecure_http: object,
) -> str:
    """Return a validated base URL without a trailing slash."""

    normalized_url = base_url.rstrip("/")
    parsed_url = urlsplit(normalized_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("base_url must be an absolute HTTP or HTTPS URL")
    if parsed_url.username is not None or parsed_url.password is not None:
        raise ValueError("base_url must not contain credentials")
    if parsed_url.query or parsed_url.fragment:
        raise ValueError("base_url must not contain a query or fragment")
    try:
        port = parsed_url.port
    except ValueError as error:
        raise ValueError("base_url must have a valid port") from error
    if port == 0:
        raise ValueError("base_url must have a valid port")
    if type(allow_insecure_http) is not bool:
        raise ValueError("allow_insecure_http must be a boolean")
    if parsed_url.scheme == "http":
        if credential_bearing:
            raise ValueError("credential-bearing endpoints must use HTTPS")
        if not allow_insecure_http:
            raise ValueError("HTTP endpoints require allow_insecure_http=True")
        if not is_loopback_hostname(parsed_url.hostname):
            raise ValueError("insecure HTTP endpoints are limited to loopback hosts")
    return normalized_url


def normalized_resource_path(resource_path: str) -> str:
    """Return a validated relative resource path without outer slashes."""

    normalized_resource = resource_path.strip("/")
    if normalized_resource and (
        "\\" in normalized_resource
        or any(part in {"", ".", ".."} for part in normalized_resource.split("/"))
    ):
        raise ValueError("resource_path must be a normalized relative URL path")
    return normalized_resource


def validate_timeout(timeout_seconds: float) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be finite and greater than zero")


def validate_deadline(deadline_seconds: float | None) -> None:
    if deadline_seconds is not None and (
        isinstance(deadline_seconds, bool)
        or not math.isfinite(deadline_seconds)
        or deadline_seconds <= 0
    ):
        raise ValueError(
            "deadline_seconds must be finite and greater than zero, or None"
        )


def frozen_query_parameters(
    query_parameters: Mapping[str, str],
) -> Mapping[str, str]:
    """Return an immutable copy of non-secret, single-line query parameters."""

    parameters = dict(query_parameters)
    for name, value in parameters.items():
        if (
            not name
            or name != name.strip()
            or "\r" in name
            or "\n" in name
            or "\r" in value
            or "\n" in value
        ):
            raise ValueError("query parameters must contain single-line values")
    return MappingProxyType(parameters)


def frozen_extra_headers(
    extra_headers: Mapping[str, str],
    *,
    credential_header: str | None,
) -> Mapping[str, str]:
    """Return an immutable copy of headers that cannot override owned fields."""

    reserved_headers = set(RESERVED_HEADERS)
    if credential_header is not None:
        reserved_headers.add(credential_header.lower())
    headers = dict(extra_headers)
    for name, value in headers.items():
        if name.lower() in reserved_headers:
            raise ValueError(f"extra_headers cannot override {name!r}")
        if HEADER_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError("extra header names must be valid HTTP field names")
        if "\r" in value or "\n" in value:
            raise ValueError("extra header values must be single lines")
        if _HEADER_VALUE_PATTERN.fullmatch(value) is None:
            raise ValueError("extra header values must be printable ASCII")
    return MappingProxyType(headers)


def defensive_extra_body(
    extra_body: Mapping[str, JsonValue],
    *,
    reserved: Set[str],
) -> Mapping[str, JsonValue]:
    """Return a defensive copy of finite JSON fields that avoid reserved names."""

    body = deepcopy(dict(extra_body))
    conflicts = sorted(set(reserved).intersection(body))
    if conflicts:
        raise ValueError(
            "extra_body cannot override reserved fields: " + ", ".join(conflicts)
        )
    try:
        json.dumps(body, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("extra_body must contain finite JSON values") from error
    return DefensiveJsonObject(body)


def endpoint_url(
    base_url: str,
    resource_path: str,
    query_parameters: Mapping[str, str],
) -> str:
    """Render a validated endpoint URL without credentials."""

    target = base_url if not resource_path else f"{base_url}/{resource_path}"
    if not query_parameters:
        return target
    return f"{target}?{urlencode(query_parameters)}"


def content_type(headers: Mapping[str, str]) -> str:
    """Return the lower-case media type of a captured response."""

    for name, value in headers.items():
        if name.lower() == "content-type":
            media_type = value.partition(";")[0].strip().lower()
            return media_type or "application/octet-stream"
    return "application/json"


def optional_non_negative_integer(value: JsonValue | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError("expected a non-negative JSON integer or null")
    return value


def token_counts(
    value: JsonValue,
) -> tuple[int | None, int | None, int | None]:
    """Read input, output, and total token counts from a usage object."""

    if not isinstance(value, dict):
        raise TypeError("expected a JSON object")
    return (
        optional_non_negative_integer(
            value.get("prompt_tokens", value.get("input_tokens"))
        ),
        optional_non_negative_integer(
            value.get("completion_tokens", value.get("output_tokens"))
        ),
        optional_non_negative_integer(value.get("total_tokens")),
    )
