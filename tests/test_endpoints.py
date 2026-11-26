# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import math
import re
import threading

import pytest

from itself._endpoints import (
    content_type,
    defensive_extra_body,
    endpoint_url,
    frozen_extra_headers,
    frozen_query_parameters,
    is_loopback_hostname,
    normalized_base_url,
    normalized_resource_path,
    optional_non_negative_integer,
    token_counts,
    validate_deadline,
    validate_timeout,
)
from itself.types import JsonValue

TIMEOUT_MESSAGE = (
    "timeout_seconds must be greater than zero and at most threading.TIMEOUT_MAX"
)
DEADLINE_MESSAGE = (
    "deadline_seconds must be greater than zero and at most "
    "threading.TIMEOUT_MAX, or None"
)
TOO_LONG = threading.TIMEOUT_MAX * 2


def _anchored(message: str) -> str:
    return f"^{re.escape(message)}$"


def _base_url(
    url: str,
    *,
    credential_bearing: bool = False,
    allow_insecure_http: object = False,
) -> str:
    return normalized_base_url(
        url,
        credential_bearing=credential_bearing,
        allow_insecure_http=allow_insecure_http,
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param("https://api.example.com", "https://api.example.com", id="https"),
        pytest.param(
            "https://api.example.com/v1", "https://api.example.com/v1", id="path"
        ),
        pytest.param(
            "https://api.example.com/v1/", "https://api.example.com/v1", id="one"
        ),
        pytest.param(
            "https://api.example.com/v1///", "https://api.example.com/v1", id="many"
        ),
        pytest.param(
            "https://api.example.com:8443/v1",
            "https://api.example.com:8443/v1",
            id="port",
        ),
        pytest.param(
            "https://[2001:db8::1]:8443/v1", "https://[2001:db8::1]:8443/v1", id="ipv6"
        ),
    ],
)
def test_normalized_base_url_returns_https_urls_without_a_trailing_slash(
    url: str, expected: str
) -> None:
    assert _base_url(url) == expected


@pytest.mark.parametrize(
    ("url", "message"),
    [
        pytest.param(
            "https://example.com/a b",
            "base_url must be printable ASCII without spaces or control characters",
            id="space",
        ),
        pytest.param(
            "https://example.com/\n",
            "base_url must be printable ASCII without spaces or control characters",
            id="newline",
        ),
        pytest.param(
            "https://exämple.com",
            "base_url must be printable ASCII without spaces or control characters",
            id="non-ascii",
        ),
        pytest.param(
            "ftp://example.com",
            "base_url must be an absolute HTTP or HTTPS URL",
            id="ftp-scheme",
        ),
        pytest.param(
            "file:///tmp/model",
            "base_url must be an absolute HTTP or HTTPS URL",
            id="file-scheme",
        ),
        pytest.param(
            "example.com/v1",
            "base_url must be an absolute HTTP or HTTPS URL",
            id="no-scheme",
        ),
        pytest.param(
            "https://", "base_url must be an absolute HTTP or HTTPS URL", id="no-netloc"
        ),
        pytest.param("", "base_url must be an absolute HTTP or HTTPS URL", id="empty"),
        pytest.param(
            "https://user@example.com",
            "base_url must not contain credentials",
            id="username",
        ),
        pytest.param(
            "https://user:secret@example.com",
            "base_url must not contain credentials",
            id="username-password",
        ),
        pytest.param(
            "https://:secret@example.com",
            "base_url must not contain credentials",
            id="password-only",
        ),
        pytest.param("https://:443/v1", "base_url must name a host", id="empty-host"),
        pytest.param(
            "https://example.com/v1?key=1",
            "base_url must not contain a query or fragment",
            id="query",
        ),
        pytest.param(
            "https://example.com/v1#frag",
            "base_url must not contain a query or fragment",
            id="fragment",
        ),
        pytest.param(
            "https://h.example/v1/?",
            "base_url must not contain a query or fragment",
            id="empty-query",
        ),
        pytest.param(
            "https://h.example/v1#",
            "base_url must not contain a query or fragment",
            id="empty-fragment",
        ),
        pytest.param(
            "https://h.example/v1?/",
            "base_url must not contain a query or fragment",
            id="empty-query-before-slash",
        ),
        pytest.param(
            "https://h.example?#",
            "base_url must not contain a query or fragment",
            id="empty-query-and-fragment",
        ),
        pytest.param(
            "https://example.com:0", "base_url must have a valid port", id="port-0"
        ),
        pytest.param(
            "https://example.com:65536",
            "base_url must have a valid port",
            id="port-high",
        ),
        pytest.param(
            "https://example.com:http",
            "base_url must have a valid port",
            id="port-text",
        ),
    ],
)
def test_normalized_base_url_rejects_malformed_urls(url: str, message: str) -> None:
    with pytest.raises(ValueError, match=_anchored(message)):
        _base_url(url)


@pytest.mark.parametrize(
    "flag",
    [
        pytest.param("yes", id="string"),
        pytest.param(1, id="int"),
        pytest.param(None, id="none"),
    ],
)
@pytest.mark.parametrize("url", ["https://example.com", "http://localhost"])
def test_normalized_base_url_requires_a_boolean_insecure_http_flag(
    url: str, flag: object
) -> None:
    with pytest.raises(
        ValueError, match=_anchored("allow_insecure_http must be a boolean")
    ):
        _base_url(url, allow_insecure_http=flag)


@pytest.mark.parametrize("allow_insecure_http", [False, True])
def test_normalized_base_url_accepts_https_whatever_the_insecure_flag_says(
    allow_insecure_http: bool,
) -> None:
    assert (
        _base_url(
            "https://example.com/v1",
            credential_bearing=True,
            allow_insecure_http=allow_insecure_http,
        )
        == "https://example.com/v1"
    )


@pytest.mark.parametrize("allow_insecure_http", [False, True])
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]", "example.com"])
def test_normalized_base_url_refuses_http_for_credential_bearing_endpoints(
    host: str, allow_insecure_http: bool
) -> None:
    with pytest.raises(
        ValueError, match=_anchored("credential-bearing endpoints must use HTTPS")
    ):
        _base_url(
            f"http://{host}/v1",
            credential_bearing=True,
            allow_insecure_http=allow_insecure_http,
        )


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]", "example.com"])
def test_normalized_base_url_requires_opt_in_for_http(host: str) -> None:
    with pytest.raises(
        ValueError, match=_anchored("HTTP endpoints require allow_insecure_http=True")
    ):
        _base_url(f"http://{host}/v1")


@pytest.mark.parametrize(
    "host",
    [
        pytest.param("example.com", id="dns-name"),
        pytest.param("192.168.1.10", id="private-ipv4"),
        pytest.param("0.0.0.0", id="unspecified-ipv4"),
        pytest.param("[2001:db8::1]", id="global-ipv6"),
        pytest.param("localhost..", id="localhost-two-trailing-dots"),
        pytest.param("127.0.0.1.", id="ipv4-trailing-dot"),
        pytest.param("localhost.example.com", id="localhost-prefix"),
        pytest.param("notlocalhost", id="localhost-suffix-without-dot"),
    ],
)
def test_normalized_base_url_limits_insecure_http_to_loopback_hosts(host: str) -> None:
    with pytest.raises(
        ValueError,
        match=_anchored("insecure HTTP endpoints are limited to loopback hosts"),
    ):
        _base_url(f"http://{host}/v1", allow_insecure_http=True)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://localhost:11434/v1", id="localhost"),
        pytest.param("http://LOCALHOST:11434/v1", id="uppercase-localhost"),
        pytest.param("http://localhost.:11434/v1", id="localhost-trailing-dot"),
        pytest.param("http://api.localhost/v1", id="localhost-subdomain"),
        pytest.param("http://API.LocalHost./v1", id="mixed-case-subdomain-dot"),
        pytest.param("http://127.0.0.1:8080/v1", id="ipv4"),
        pytest.param("http://127.9.8.7/v1", id="ipv4-elsewhere-in-127"),
        pytest.param("http://[::1]:8080/v1", id="ipv6"),
    ],
)
def test_normalized_base_url_allows_opted_in_http_on_loopback_hosts(url: str) -> None:
    assert _base_url(url, allow_insecure_http=True) == url


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        pytest.param(None, False, id="none"),
        pytest.param("", False, id="empty"),
        pytest.param("localhost", True, id="localhost"),
        pytest.param("LOCALHOST", True, id="uppercase-localhost"),
        pytest.param("LocalHost", True, id="mixed-case-localhost"),
        pytest.param("localhost.", True, id="trailing-dot"),
        pytest.param("LOCALHOST.", True, id="uppercase-trailing-dot"),
        pytest.param("localhost..", False, id="two-trailing-dots"),
        pytest.param(".", False, id="dot-only"),
        pytest.param("api.localhost", True, id="localhost-subdomain"),
        pytest.param("api.localhost.", True, id="localhost-subdomain-trailing-dot"),
        pytest.param("API.LOCALHOST", True, id="uppercase-localhost-subdomain"),
        pytest.param("127.0.0.1.", False, id="ipv4-trailing-dot"),
        pytest.param("::1.", False, id="ipv6-trailing-dot"),
        pytest.param("localhost.example.com.", False, id="localhost-prefix-dot"),
        pytest.param("127.0.0.1", True, id="ipv4-loopback"),
        pytest.param("127.255.255.254", True, id="ipv4-loopback-range"),
        pytest.param("::1", True, id="ipv6-loopback"),
        pytest.param("0.0.0.0", False, id="ipv4-unspecified"),
        pytest.param("::", False, id="ipv6-unspecified"),
        pytest.param("10.0.0.1", False, id="private-ipv4"),
        pytest.param("8.8.8.8", False, id="public-ipv4"),
        pytest.param("2001:db8::1", False, id="non-loopback-ipv6"),
        pytest.param("[::1]", False, id="bracketed-ipv6"),
        pytest.param("example.com", False, id="dns-name"),
        pytest.param("localhost.example.com", False, id="localhost-prefix"),
        pytest.param("127.0.0.1.example.com", False, id="ipv4-lookalike"),
    ],
)
def test_is_loopback_hostname_classifies_hostnames(
    hostname: str | None, expected: bool
) -> None:
    assert is_loopback_hostname(hostname) is expected


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(1, id="int"),
        pytest.param(0.001, id="small-float"),
        pytest.param(30.5, id="float"),
        pytest.param(threading.TIMEOUT_MAX, id="timeout-max"),
    ],
)
def test_validate_timeout_accepts_positive_finite_durations(value: float) -> None:
    validate_timeout(value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param(math.nan, id="nan"),
        pytest.param(math.inf, id="positive-infinity"),
        pytest.param(-math.inf, id="negative-infinity"),
        pytest.param(0, id="zero-int"),
        pytest.param(0.0, id="zero-float"),
        pytest.param(-1, id="negative-int"),
        pytest.param(-0.5, id="negative-float"),
        pytest.param(TOO_LONG, id="above-timeout-max"),
    ],
)
def test_validate_timeout_rejects_unusable_durations(value: float) -> None:
    with pytest.raises(ValueError, match=_anchored(TIMEOUT_MESSAGE)):
        validate_timeout(value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="none"),
        pytest.param(1, id="int"),
        pytest.param(0.25, id="float"),
        pytest.param(threading.TIMEOUT_MAX, id="timeout-max"),
    ],
)
def test_validate_deadline_accepts_none_and_positive_finite_durations(
    value: float | None,
) -> None:
    validate_deadline(value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param(math.nan, id="nan"),
        pytest.param(math.inf, id="positive-infinity"),
        pytest.param(-math.inf, id="negative-infinity"),
        pytest.param(0, id="zero-int"),
        pytest.param(0.0, id="zero-float"),
        pytest.param(-1, id="negative-int"),
        pytest.param(-0.5, id="negative-float"),
        pytest.param(TOO_LONG, id="above-timeout-max"),
    ],
)
def test_validate_deadline_rejects_unusable_durations(value: float) -> None:
    with pytest.raises(ValueError, match=_anchored(DEADLINE_MESSAGE)):
        validate_deadline(value)


@pytest.mark.parametrize(
    ("resource_path", "expected"),
    [
        pytest.param("", "", id="empty"),
        pytest.param("/", "", id="slash-only"),
        pytest.param("chat/completions", "chat/completions", id="plain"),
        pytest.param("/chat/completions/", "chat/completions", id="outer-slashes"),
    ],
)
def test_normalized_resource_path_strips_outer_slashes(
    resource_path: str, expected: str
) -> None:
    assert normalized_resource_path(resource_path) == expected


@pytest.mark.parametrize(
    "resource_path",
    [
        pytest.param("a//b", id="empty-segment"),
        pytest.param("a/./b", id="dot-segment"),
        pytest.param("a/../b", id="dotdot-segment"),
        pytest.param("..", id="dotdot-only"),
        pytest.param("a\\b", id="backslash"),
    ],
)
def test_normalized_resource_path_rejects_unnormalized_paths(
    resource_path: str,
) -> None:
    with pytest.raises(
        ValueError,
        match=_anchored("resource_path must be a normalized relative URL path"),
    ):
        normalized_resource_path(resource_path)


@pytest.mark.parametrize("resource_path", ["a b", "a\tb", "café"])
def test_normalized_resource_path_rejects_unprintable_text(resource_path: str) -> None:
    with pytest.raises(
        ValueError,
        match=_anchored(
            "resource_path must be printable ASCII without spaces or control characters"
        ),
    ):
        normalized_resource_path(resource_path)


def test_frozen_query_parameters_returns_an_immutable_copy() -> None:
    source = {"api-version": "2024-01-01"}

    frozen = frozen_query_parameters(source)
    source["api-version"] = "changed"

    assert dict(frozen) == {"api-version": "2024-01-01"}
    with pytest.raises(TypeError):
        frozen["x"] = "y"  # type: ignore[index]


@pytest.mark.parametrize(
    "parameters",
    [
        pytest.param({"": "v"}, id="empty-name"),
        pytest.param({" a": "v"}, id="padded-name"),
        pytest.param({"a\n": "v"}, id="newline-in-name"),
        pytest.param({"a": "v\r"}, id="carriage-return-in-value"),
        pytest.param({"a": "v\nw"}, id="newline-in-value"),
    ],
)
def test_frozen_query_parameters_rejects_multiline_or_blank_names(
    parameters: dict[str, str],
) -> None:
    with pytest.raises(
        ValueError, match=_anchored("query parameters must contain single-line values")
    ):
        frozen_query_parameters(parameters)


def test_frozen_extra_headers_returns_an_immutable_copy() -> None:
    source = {"X-Trace": "abc"}

    frozen = frozen_extra_headers(source, credential_header=None)
    source["X-Trace"] = "changed"

    assert dict(frozen) == {"X-Trace": "abc"}
    with pytest.raises(TypeError):
        frozen["x"] = "y"  # type: ignore[index]


@pytest.mark.parametrize(
    ("name", "credential_header"),
    [
        pytest.param("Content-Type", None, id="reserved"),
        pytest.param("ACCEPT", None, id="reserved-uppercase"),
        pytest.param("Authorization", "authorization", id="credential"),
        pytest.param(
            "authorization", "Authorization", id="credential-case-insensitive"
        ),
    ],
)
def test_frozen_extra_headers_rejects_owned_headers(
    name: str, credential_header: str | None
) -> None:
    with pytest.raises(
        ValueError, match=_anchored(f"extra_headers cannot override {name!r}")
    ):
        frozen_extra_headers({name: "x"}, credential_header=credential_header)


def test_frozen_extra_headers_allows_the_credential_header_name_when_not_declared() -> (
    None
):
    headers = frozen_extra_headers({"Authorization": "x"}, credential_header=None)

    assert dict(headers) == {"Authorization": "x"}


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        pytest.param(
            {"bad name": "v"},
            "extra header names must be valid HTTP field names",
            id="space",
        ),
        pytest.param(
            {"": "v"}, "extra header names must be valid HTTP field names", id="empty"
        ),
        pytest.param(
            {"X-A": "a\nb"}, "extra header values must be single lines", id="newline"
        ),
        pytest.param(
            {"X-A": "a\rb"}, "extra header values must be single lines", id="return"
        ),
        pytest.param(
            {"X-A": "café"}, "extra header values must be printable ASCII", id="latin"
        ),
        pytest.param(
            {"X-A": "a\x00b"}, "extra header values must be printable ASCII", id="nul"
        ),
    ],
)
def test_frozen_extra_headers_rejects_invalid_fields(
    headers: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=_anchored(message)):
        frozen_extra_headers(headers, credential_header=None)


def test_defensive_extra_body_copies_nested_values_on_read() -> None:
    source: dict[str, JsonValue] = {"options": {"stop": ["a"]}}

    body = defensive_extra_body(source, reserved=frozenset())
    options = body["options"]
    assert isinstance(options, dict)
    options["stop"] = ["mutated"]
    source["options"] = {"stop": ["changed"]}

    assert body["options"] == {"stop": ["a"]}
    assert dict(body) == {"options": {"stop": ["a"]}}
    assert len(body) == 1
    assert list(body) == ["options"]


def test_defensive_extra_body_lists_sorted_reserved_conflicts() -> None:
    with pytest.raises(
        ValueError,
        match=_anchored("extra_body cannot override reserved fields: messages, model"),
    ):
        defensive_extra_body(
            {"model": "m", "messages": [], "top_k": 1},
            reserved=frozenset({"model", "messages", "stream"}),
        )


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(math.nan, id="nan"),
        pytest.param(math.inf, id="infinity"),
        pytest.param(object(), id="non-json"),
    ],
)
def test_defensive_extra_body_rejects_non_finite_or_non_json_values(
    value: object,
) -> None:
    body: dict[str, JsonValue] = {"x": value}  # type: ignore[dict-item]

    with pytest.raises(
        ValueError, match=_anchored("extra_body must contain finite JSON values")
    ):
        defensive_extra_body(body, reserved=frozenset())


@pytest.mark.parametrize(
    ("resource_path", "parameters", "expected"),
    [
        pytest.param("", {}, "https://h/v1", id="base-only"),
        pytest.param("models", {}, "https://h/v1/models", id="resource"),
        pytest.param("", {"a": "1"}, "https://h/v1?a=1", id="base-query"),
        pytest.param(
            "models",
            {"a": "x y", "b": "&"},
            "https://h/v1/models?a=x+y&b=%26",
            id="encoded",
        ),
    ],
)
def test_endpoint_url_joins_path_and_encodes_query(
    resource_path: str, parameters: dict[str, str], expected: str
) -> None:
    assert endpoint_url("https://h/v1", resource_path, parameters) == expected


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param({}, "application/json", id="missing-defaults-to-json"),
        pytest.param({"X-Other": "text/plain"}, "application/json", id="other-header"),
        pytest.param({"Content-Type": "text/html"}, "text/html", id="plain"),
        pytest.param(
            {"content-type": "Application/JSON; charset=utf-8"},
            "application/json",
            id="parameters-and-case",
        ),
        pytest.param({"CONTENT-TYPE": ""}, "application/octet-stream", id="empty"),
        pytest.param(
            {"Content-Type": "; charset=x"},
            "application/octet-stream",
            id="only-parameters",
        ),
    ],
)
def test_content_type_returns_the_lower_case_media_type(
    headers: dict[str, str], expected: str
) -> None:
    assert content_type(headers) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(None, None, id="null"),
        pytest.param(0, 0, id="zero"),
        pytest.param(7, 7, id="positive"),
    ],
)
def test_optional_non_negative_integer_accepts_null_and_non_negative_integers(
    value: int | None, expected: int | None
) -> None:
    assert optional_non_negative_integer(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(-1, id="negative"),
        pytest.param(True, id="bool"),
        pytest.param(1.0, id="float"),
        pytest.param("1", id="string"),
        pytest.param([1], id="list"),
    ],
)
def test_optional_non_negative_integer_rejects_everything_else(
    value: JsonValue,
) -> None:
    with pytest.raises(
        TypeError, match=_anchored("expected a non-negative JSON integer or null")
    ):
        optional_non_negative_integer(value)


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        pytest.param({}, (None, None, None), id="empty"),
        pytest.param(
            {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            (1, 2, 3),
            id="chat-names",
        ),
        pytest.param(
            {"input_tokens": 4, "output_tokens": 5}, (4, 5, None), id="alt-names"
        ),
        pytest.param(
            {"prompt_tokens": 1, "input_tokens": 9, "completion_tokens": None},
            (1, None, None),
            id="prompt-wins-and-null-does-not-fall-back",
        ),
    ],
)
def test_token_counts_reads_either_naming_scheme(
    usage: dict[str, JsonValue], expected: tuple[int | None, int | None, int | None]
) -> None:
    assert token_counts(usage) == expected


@pytest.mark.parametrize("value", [None, [], "usage", 3])
def test_token_counts_requires_a_json_object(value: JsonValue) -> None:
    with pytest.raises(TypeError, match=_anchored("expected a JSON object")):
        token_counts(value)


def test_token_counts_propagates_invalid_counts() -> None:
    with pytest.raises(
        TypeError, match=_anchored("expected a non-negative JSON integer or null")
    ):
        token_counts({"total_tokens": -1})
