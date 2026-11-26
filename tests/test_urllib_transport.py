# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import os
import socket
import ssl
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import replace
from http.client import IncompleteRead, InvalidURL
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from typing import cast
from urllib.error import URLError
from urllib.request import BaseHandler, HTTPRedirectHandler

import pytest

from itself import (
    HttpRequest,
    InferenceError,
    InferenceFailure,
    UrllibHttpTransport,
)
from itself._http_transport import (
    _NoRedirectHandler,  # pyright: ignore[reportPrivateUsage]
    _SocketTracker,  # pyright: ignore[reportPrivateUsage]
)
from itself._http_transport import (
    _opener as _transport_opener,  # pyright: ignore[reportPrivateUsage]
)

SECRET = "Bearer redirect-canary"


def _redirect_handler(
    location: str,
    received_authorization: list[str | None],
) -> type[BaseHTTPRequestHandler]:
    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received_authorization.append(self.headers.get("Authorization"))
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return RedirectHandler


def _capture_handler(
    received_authorization: list[str | None],
) -> type[BaseHTTPRequestHandler]:
    class CaptureHandler(BaseHTTPRequestHandler):
        def _capture(self) -> None:
            received_authorization.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_GET(self) -> None:
            self._capture()

        def do_POST(self) -> None:
            self._capture()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return CaptureHandler


def _body_handler(body: bytes) -> type[BaseHTTPRequestHandler]:
    class BodyHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return BodyHandler


def _truncating_handler(status: int, framing: str) -> type[BaseHTTPRequestHandler]:
    body = b'{"choices":[{"message":{"content":"four"}}]}'
    sent = body[: len(body) // 2]

    class TruncatingHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            # Reading the request first makes the close a clean FIN, not a reset.
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(status)
            if framing == "content-length":
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(sent)
            else:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(b"%x\r\n%s" % (len(body), sent))

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return TruncatingHandler


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Generator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    # A short poll interval keeps shutdown from adding half a second per server.
    thread = Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(url: str) -> HttpRequest:
    return HttpRequest(
        url=url,
        headers={
            "Authorization": SECRET,
            "Content-Type": "application/json",
        },
        body=b"{}",
        timeout_seconds=5,
    )


def test_default_transport_returns_redirect_without_forwarding_headers() -> None:
    target_headers: list[str | None] = []
    redirect_headers: list[str | None] = []
    with (
        _serve(_capture_handler(target_headers)) as target,
        _serve(_redirect_handler(f"{target}/capture", redirect_headers)) as redirect,
    ):
        response = UrllibHttpTransport().send(_request(f"{redirect}/start"))

    assert response.status_code == 302
    assert redirect_headers == [SECRET]
    assert target_headers == []


REDIRECT_STATUSES = [301, 302, 303, 307, 308]


def _redirect_status_handler(
    status: int,
    location: str,
    received_authorization: list[str | None],
) -> type[BaseHTTPRequestHandler]:
    class RedirectStatusHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received_authorization.append(self.headers.get("Authorization"))
            self.send_response(status)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return RedirectStatusHandler


@pytest.mark.parametrize("status", REDIRECT_STATUSES)
@pytest.mark.parametrize("deadline", [None, 5.0], ids=["shared", "tracked"])
def test_loopback_openers_return_every_redirect_unfollowed(
    status: int,
    deadline: float | None,
) -> None:
    target_headers: list[str | None] = []
    redirect_headers: list[str | None] = []
    with (
        _serve(_capture_handler(target_headers)) as target,
        _serve(
            _redirect_status_handler(status, f"{target}/capture", redirect_headers)
        ) as redirect,
    ):
        request = replace(_request(f"{redirect}/start"), deadline_seconds=deadline)
        response = UrllibHttpTransport().send(request)

    assert response.status_code == status
    assert redirect_headers == [SECRET]
    assert target_headers == []


@pytest.mark.parametrize("status", REDIRECT_STATUSES)
@pytest.mark.parametrize("deadline", [None, 5.0], ids=["per-request", "tracked"])
def test_proxied_openers_return_every_redirect_unfollowed(
    status: int,
    deadline: float | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_headers: list[str | None] = []
    proxy_headers: list[str | None] = []
    with (
        _serve(_capture_handler(target_headers)) as target,
        _serve(
            _redirect_status_handler(status, f"{target}/capture", proxy_headers)
        ) as proxy,
    ):
        # A non-loopback host goes through the proxy, which answers for it.
        _set_proxy_environment(monkeypatch, proxy)
        request = replace(
            _request("http://models.example.test/v1"),
            deadline_seconds=deadline,
        )
        response = UrllibHttpTransport().send(request)

    assert response.status_code == status
    assert proxy_headers == [SECRET]
    assert target_headers == []


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:9/v1", "https://models.example.test/v1"],
    ids=["loopback", "other-host"],
)
@pytest.mark.parametrize("tracked", [False, True])
def test_every_opener_replaces_the_redirect_handler(url: str, tracked: bool) -> None:
    opener = _transport_opener(url, _SocketTracker() if tracked else None)
    # The standard library stubs do not declare OpenerDirector.handlers.
    handlers = cast(list[BaseHandler], vars(opener)["handlers"])

    redirect_handlers = [
        handler for handler in handlers if isinstance(handler, HTTPRedirectHandler)
    ]
    assert len(redirect_handlers) == 1
    assert type(redirect_handlers[0]) is _NoRedirectHandler


@pytest.mark.parametrize("deadline", [None, 5.0])
def test_response_of_exactly_the_byte_limit_is_accepted(
    deadline: float | None,
) -> None:
    with _serve(_body_handler(b"12345")) as endpoint:
        request = replace(_request(f"{endpoint}/v1"), deadline_seconds=deadline)
        response = UrllibHttpTransport(max_response_bytes=5).send(request)
        with pytest.raises(InferenceError) as raised:
            UrllibHttpTransport(max_response_bytes=4).send(request)

    assert response.body == b"12345"
    assert raised.value.failure is InferenceFailure.RESPONSE_TOO_LARGE


@pytest.mark.parametrize("limit", [0, -1, True, False, 1.5, 2.0, "3", None])
def test_response_byte_limit_must_be_a_positive_integer(limit: object) -> None:
    with pytest.raises(ValueError, match="max_response_bytes must be a positive"):
        UrllibHttpTransport(max_response_bytes=limit)  # type: ignore[arg-type]


def test_smallest_response_byte_limit_is_accepted() -> None:
    assert UrllibHttpTransport(max_response_bytes=1).max_response_bytes == 1


def test_default_transport_enforces_response_byte_limit() -> None:
    with (
        _serve(_body_handler(b"12345")) as endpoint,
        pytest.raises(InferenceError) as raised,
    ):
        UrllibHttpTransport(max_response_bytes=4).send(
            _request(f"{endpoint}/completion")
        )

    assert raised.value.failure is InferenceFailure.RESPONSE_TOO_LARGE


@pytest.mark.parametrize("status", [200, 500])
@pytest.mark.parametrize("framing", ["content-length", "chunked"])
def test_default_transport_rejects_a_response_that_ends_early(
    status: int,
    framing: str,
) -> None:
    with (
        _serve(_truncating_handler(status, framing)) as endpoint,
        pytest.raises(InferenceError) as raised,
    ):
        UrllibHttpTransport().send(_request(f"{endpoint}/completion"))

    assert raised.value.failure is InferenceFailure.TRANSPORT
    assert raised.value.retryable


def _set_proxy_environment(monkeypatch: pytest.MonkeyPatch, proxy: str) -> None:
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, proxy)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


def test_default_transport_never_sends_loopback_requests_through_a_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_headers: list[str | None] = []
    proxy_headers: list[str | None] = []
    with (
        _serve(_capture_handler(target_headers)) as target,
        _serve(_capture_handler(proxy_headers)) as proxy,
    ):
        _set_proxy_environment(monkeypatch, proxy)
        response = UrllibHttpTransport().send(_request(f"{target}/v1"))

    assert response.status_code == 200
    assert target_headers == [SECRET]
    assert proxy_headers == []


@pytest.mark.parametrize("host", ["localhost.", "LOCALHOST.", "Api.LocalHost"])
@pytest.mark.parametrize("deadline_seconds", [None, 5.0], ids=["timeout", "deadline"])
def test_loopback_names_bypass_the_proxy_in_any_case_or_with_a_trailing_dot(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    deadline_seconds: float | None,
) -> None:
    proxy_headers: list[str | None] = []
    with _serve(_capture_handler(proxy_headers)) as proxy:
        _set_proxy_environment(monkeypatch, proxy)
        # Whether the name resolves here is up to the host's resolver; a request
        # that went through the proxy would reach it and succeed.
        request = replace(
            _request(f"http://{host}:9/v1"), deadline_seconds=deadline_seconds
        )
        with pytest.raises(InferenceError) as raised:
            UrllibHttpTransport().send(request)

    assert raised.value.failure is InferenceFailure.TRANSPORT
    assert proxy_headers == []


def test_default_transport_keeps_the_proxy_environment_for_other_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_headers: list[str | None] = []
    with _serve(_capture_handler(proxy_headers)) as proxy:
        _set_proxy_environment(monkeypatch, proxy)
        response = UrllibHttpTransport().send(_request("http://models.example.test/v1"))

    assert response.status_code == 200
    assert proxy_headers == [SECRET]


@pytest.mark.parametrize(
    "url",
    ["file:///etc/hosts", "ftp://127.0.0.1/v1", "data:,{}"],
)
def test_default_transport_sends_only_http_or_https(url: str) -> None:
    with pytest.raises(InferenceError) as raised:
        UrllibHttpTransport().send(_request(url))

    assert raised.value.failure is InferenceFailure.CONFIGURATION


def _stalling_server(behavior: str, stop: Event) -> str:
    """Serve one connection that never finishes but keeps every read busy."""

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            data = b""
            while b"\r\n\r\n" not in data:
                data += connection.recv(65536)
            try:
                if behavior == "heartbeat":
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                        b"Transfer-Encoding: chunked\r\n\r\n"
                    )
                    while not stop.is_set():
                        comment = b": keep-alive\n\n"
                        connection.sendall(b"%x\r\n%s\r\n" % (len(comment), comment))
                        time.sleep(0.1)
                else:
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                        b"2\r\n{}\r\n0\r\n"
                    )
                    trailer = b"X-Trailer: " + b"a" * 1000 + b"\r\n"
                    while not stop.is_set():
                        connection.sendall(trailer)
            except OSError:
                pass
        listener.close()

    Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{listener.getsockname()[1]}"


def _within(seconds: float, call: Callable[[], object]) -> list[BaseException]:
    """Run a call on a daemon thread, failing instead of hanging the suite."""

    raised: list[BaseException] = []

    def run() -> None:
        try:
            call()
        except BaseException as error:  # noqa: BLE001 - collected for assertions
            raised.append(error)

    worker = Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), f"the request outlived its deadline by {seconds} s"
    return raised


@pytest.mark.parametrize("behavior", ["heartbeat", "trailer-flood"])
def test_deadline_ends_a_request_the_peer_keeps_busy(behavior: str) -> None:
    stop = Event()
    try:
        endpoint = _stalling_server(behavior, stop)
        request = replace(
            _request(f"{endpoint}/v1"),
            timeout_seconds=5.0,
            deadline_seconds=0.5,
        )
        started = time.monotonic()
        raised = _within(5.0, lambda: UrllibHttpTransport().send(request))
        elapsed = time.monotonic() - started
    finally:
        stop.set()

    assert len(raised) == 1
    error = raised[0]
    assert isinstance(error, InferenceError)
    assert error.failure is InferenceFailure.TRANSPORT
    assert error.retryable
    # Only the deadline can end a stream of keep-alive comments. Some Python
    # versions' http.client refuses an endless trailer section on its own.
    if behavior == "heartbeat":
        assert "deadline" in str(error)
    assert elapsed < 3.0


def test_deadline_leaves_a_prompt_response_unchanged() -> None:
    with _serve(_body_handler(b'{"ok":true}')) as endpoint:
        request = replace(_request(f"{endpoint}/v1"), deadline_seconds=5.0)
        response = UrllibHttpTransport().send(request)

    assert response.status_code == 200
    assert response.body == b'{"ok":true}'


def test_deadline_requests_still_bypass_proxies_for_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_headers: list[str | None] = []
    proxy_headers: list[str | None] = []
    with (
        _serve(_capture_handler(target_headers)) as target,
        _serve(_capture_handler(proxy_headers)) as proxy,
    ):
        _set_proxy_environment(monkeypatch, proxy)
        request = replace(_request(f"{target}/v1"), deadline_seconds=5.0)
        response = UrllibHttpTransport().send(request)

    assert response.status_code == 200
    assert target_headers == [SECRET]
    assert proxy_headers == []


def _trickling_proxy(stop: Event) -> str:
    """Accept one CONNECT and answer it one header line at a time."""

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            data = b""
            while b"\r\n\r\n" not in data:
                data += connection.recv(65536)
            try:
                connection.sendall(b"HTTP/1.1 200 Connection established\r\n")
                while not stop.is_set():
                    connection.sendall(b"X-Slow: 1\r\n")
                    time.sleep(0.1)
            except OSError:
                pass
        listener.close()

    Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{listener.getsockname()[1]}"


def test_deadline_covers_a_proxy_that_trickles_its_connect_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = Event()
    try:
        _set_proxy_environment(monkeypatch, _trickling_proxy(stop))
        request = replace(
            _request("https://models.example.test/v1"),
            timeout_seconds=5.0,
            deadline_seconds=0.5,
        )
        started = time.monotonic()
        raised = _within(5.0, lambda: UrllibHttpTransport().send(request))
        elapsed = time.monotonic() - started
    finally:
        stop.set()

    assert len(raised) == 1
    error = raised[0]
    assert isinstance(error, InferenceError)
    assert "deadline" in str(error)
    assert elapsed < 3.0


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_deadline_requests_load_trust_through_the_standard_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[ssl.SSLContext] = []

    def recording_context() -> ssl.SSLContext:
        context = ssl.create_default_context()
        loaded.append(context)
        return context

    monkeypatch.setattr("ssl._create_default_https_context", recording_context)
    port = _closed_port()
    for _ in range(2):
        request = replace(
            _request(f"https://127.0.0.1:{port}/v1"),
            timeout_seconds=2.0,
            deadline_seconds=5.0,
        )
        with pytest.raises(InferenceError):
            UrllibHttpTransport().send(request)

    # One trust store per connection, loaded the way requests without a
    # deadline load theirs, so a process-wide trust setting applies to both.
    assert len(loaded) == 2


@pytest.mark.parametrize(
    ("url", "headers"),
    [
        ("http://127.0.0.1:9/v1 x", {}),
        ("http://127.0.0.1:9/v1\x7f", {}),
        ("http://127.0.0.1:9/système", {}),
        ("http://127.0.0.1:9/v1", {"X-Note": "quote ’"}),
        (
            "http://127.0.0.1:9/v1",
            {"Authorization": "Bearer canary-secret\nX-Injected: 1"},
        ),
    ],
    ids=[
        "space",
        "control",
        "non-ascii-path",
        "non-latin-1-header",
        "line-break-in-credential-header",
    ],
)
@pytest.mark.parametrize("deadline", [None, 5.0])
def test_requests_http_cannot_carry_are_configuration_failures(
    url: str,
    headers: dict[str, str],
    deadline: float | None,
) -> None:
    request = HttpRequest(
        url=url,
        headers=headers,
        body=b"{}",
        timeout_seconds=1.0,
        deadline_seconds=deadline,
    )

    with pytest.raises(InferenceError) as raised:
        UrllibHttpTransport().send(request)

    assert raised.value.failure is InferenceFailure.CONFIGURATION
    assert not raised.value.retryable
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert "canary-secret" not in str(raised.value)
    assert "canary-secret" not in repr(raised.value)


@pytest.mark.parametrize(
    ("timeout", "deadline"),
    [
        (0.0, None),
        (threading.TIMEOUT_MAX * 2, None),
        (1.0, threading.TIMEOUT_MAX * 2),
        (1.0, float("nan")),
    ],
    ids=["zero-timeout", "huge-timeout", "huge-deadline", "nan-deadline"],
)
def test_out_of_range_durations_are_configuration_failures(
    timeout: float,
    deadline: float | None,
) -> None:
    request = replace(
        _request(f"http://127.0.0.1:{_closed_port()}/v1"),
        timeout_seconds=timeout,
        deadline_seconds=deadline,
    )

    with pytest.raises(InferenceError) as raised:
        UrllibHttpTransport().send(request)

    assert raised.value.failure is InferenceFailure.CONFIGURATION


@pytest.mark.skipif(not Path("/dev/fd").is_dir(), reason="needs /dev/fd")
def test_deadline_requests_release_their_duplicate_descriptors() -> None:
    with _serve(_body_handler(b"{}")) as endpoint:
        request = replace(_request(f"{endpoint}/v1"), deadline_seconds=5.0)
        UrllibHttpTransport().send(request)
        before = len(os.listdir("/dev/fd"))
        for _ in range(30):
            UrllibHttpTransport().send(request)
        after = len(os.listdir("/dev/fd"))

    assert after - before < 10


# A failure after the response's headers arrived names its status; one before
# then names none.  Neither changes the failure's kind, detail, or
# retryability.

BYTE_LIMIT_DETAIL = "inference endpoint response exceeded the configured byte limit"
REQUEST_FAILED_DETAIL = "inference endpoint request failed"
DEADLINE_DETAIL = "inference endpoint exceeded the request deadline"


def _status_body_handler(status: int, body: bytes) -> type[BaseHTTPRequestHandler]:
    class StatusBodyHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return StatusBodyHandler


def _stalled_body_handler(
    status: int,
    release: Event,
) -> type[BaseHTTPRequestHandler]:
    class StalledBodyHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(status)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"{")
            release.wait(5)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return StalledBodyHandler


def _silent_server(stop: Event, sent: bytes = b"") -> str:
    """Accept one connection, send ``sent`` on it, and then say nothing more."""

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            if sent:
                connection.sendall(sent)
            stop.wait(10)
        listener.close()

    Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{listener.getsockname()[1]}"


@pytest.mark.parametrize("status", [200, 503])
@pytest.mark.parametrize("deadline", [None, 5.0])
def test_a_response_over_the_byte_limit_names_its_status(
    status: int,
    deadline: float | None,
) -> None:
    with _serve(_status_body_handler(status, b"12345")) as endpoint:
        request = replace(_request(f"{endpoint}/v1"), deadline_seconds=deadline)
        with pytest.raises(InferenceError) as raised:
            UrllibHttpTransport(max_response_bytes=4).send(request)

    assert raised.value.failure is InferenceFailure.RESPONSE_TOO_LARGE
    assert raised.value.detail == BYTE_LIMIT_DETAIL
    assert not raised.value.retryable
    assert raised.value.status_code == status


@pytest.mark.parametrize("status", [200, 503])
@pytest.mark.parametrize(
    ("framing", "detail"),
    [
        (
            "content-length",
            "inference endpoint response ended before its declared length",
        ),
        ("chunked", REQUEST_FAILED_DETAIL),
    ],
)
@pytest.mark.parametrize("deadline", [None, 5.0])
def test_a_response_that_ends_early_names_its_status(
    status: int,
    framing: str,
    detail: str,
    deadline: float | None,
) -> None:
    with _serve(_truncating_handler(status, framing)) as endpoint:
        request = replace(_request(f"{endpoint}/v1"), deadline_seconds=deadline)
        with pytest.raises(InferenceError) as raised:
            UrllibHttpTransport().send(request)

    assert raised.value.failure is InferenceFailure.TRANSPORT
    assert raised.value.detail == detail
    assert raised.value.retryable
    assert raised.value.status_code == status


@pytest.mark.parametrize("status", [200, 503])
@pytest.mark.parametrize("deadline", [None, 5.0])
def test_a_body_read_that_times_out_names_its_status(
    status: int,
    deadline: float | None,
) -> None:
    release = Event()
    with _serve(_stalled_body_handler(status, release)) as endpoint:
        request = replace(
            _request(f"{endpoint}/v1"),
            timeout_seconds=0.3,
            deadline_seconds=deadline,
        )
        try:
            with pytest.raises(InferenceError) as raised:
                UrllibHttpTransport().send(request)
        finally:
            release.set()

    assert raised.value.failure is InferenceFailure.TRANSPORT
    assert raised.value.detail == REQUEST_FAILED_DETAIL
    assert raised.value.retryable
    assert raised.value.status_code == status


def test_a_deadline_that_passes_while_the_body_is_read_names_its_status() -> None:
    stop = Event()
    try:
        endpoint = _stalling_server("heartbeat", stop)
        request = replace(
            _request(f"{endpoint}/v1"),
            timeout_seconds=5.0,
            deadline_seconds=0.5,
        )
        raised = _within(5.0, lambda: UrllibHttpTransport().send(request))
    finally:
        stop.set()

    assert len(raised) == 1
    error = raised[0]
    assert isinstance(error, InferenceError)
    assert error.failure is InferenceFailure.TRANSPORT
    assert error.detail == DEADLINE_DETAIL
    assert error.status_code == 200


def _stalled_failure(
    sent: bytes,
    timeout: float,
    deadline: float | None,
) -> InferenceError:
    """Return the failure of a request whose peer sends ``sent`` and stalls."""

    stop = Event()
    try:
        endpoint = _silent_server(stop, sent)
        request = replace(
            _request(f"{endpoint}/v1"),
            timeout_seconds=timeout,
            deadline_seconds=deadline,
        )
        raised = _within(5.0, lambda: UrllibHttpTransport().send(request))
    finally:
        stop.set()

    assert len(raised) == 1
    error = raised[0]
    assert isinstance(error, InferenceError)
    assert error.failure is InferenceFailure.TRANSPORT
    return error


STALLS = pytest.mark.parametrize(
    ("timeout", "deadline", "detail"),
    [(0.3, None, REQUEST_FAILED_DETAIL), (5.0, 0.5, DEADLINE_DETAIL)],
    ids=["timeout", "deadline"],
)


@STALLS
def test_a_failure_before_the_status_line_names_no_status(
    timeout: float,
    deadline: float | None,
    detail: str,
) -> None:
    error = _stalled_failure(b"", timeout, deadline)

    assert error.detail == detail
    assert error.status_code is None


@STALLS
@pytest.mark.parametrize(
    "sent",
    [b"HTTP/1.1 503 Busy\r\n", b"HTTP/1.1 503 Busy\r\nContent-Length: 2\r\n"],
    ids=["status-line", "unfinished-headers"],
)
def test_a_failure_while_the_headers_arrive_names_the_status_or_none(
    sent: bytes,
    timeout: float,
    deadline: float | None,
    detail: str,
) -> None:
    error = _stalled_failure(sent, timeout, deadline)

    assert error.detail == detail
    assert error.status_code in (None, 503)


@pytest.mark.parametrize("deadline", [None, 5.0])
def test_a_refused_connection_names_no_status(deadline: float | None) -> None:
    request = replace(
        _request(f"http://127.0.0.1:{_closed_port()}/v1"),
        deadline_seconds=deadline,
    )

    with pytest.raises(InferenceError) as raised:
        UrllibHttpTransport().send(request)

    assert raised.value.failure is InferenceFailure.TRANSPORT
    assert raised.value.status_code is None


class _ScriptedResponse:
    """A response whose headers have arrived and whose body read is scripted."""

    def __init__(self, read: object) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}
        self.read = read

    def __enter__(self) -> _ScriptedResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args


class _ScriptedOpener:
    def __init__(self, response: _ScriptedResponse) -> None:
        self.response = response

    def open(self, request: object, timeout: float | None = None) -> object:
        del request, timeout
        return self.response


def _use_opener(
    monkeypatch: pytest.MonkeyPatch,
    opener_for: Callable[[_SocketTracker | None], object],
) -> None:
    def opener(url: str, tracker: _SocketTracker | None = None) -> object:
        del url
        return opener_for(tracker)

    monkeypatch.setattr("itself._http_transport._opener", opener)


def _raising(error: Exception) -> Callable[[int], bytes]:
    def read(amount: int) -> bytes:
        del amount
        raise error

    return read


def _text(amount: int) -> str:
    del amount
    return "{}"


@pytest.mark.parametrize(
    ("read", "failure", "detail"),
    [
        (
            _raising(ConnectionResetError("reset by peer")),
            InferenceFailure.TRANSPORT,
            REQUEST_FAILED_DETAIL,
        ),
        (
            _raising(TimeoutError("timed out")),
            InferenceFailure.TRANSPORT,
            REQUEST_FAILED_DETAIL,
        ),
        (
            _raising(IncompleteRead(b"{")),
            InferenceFailure.TRANSPORT,
            REQUEST_FAILED_DETAIL,
        ),
        (
            _raising(ValueError("I/O operation on closed file")),
            InferenceFailure.CONFIGURATION,
            "inference request could not be formed as valid HTTP",
        ),
        (
            _raising(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")),
            InferenceFailure.CONFIGURATION,
            "inference request URL or headers cannot be encoded for HTTP",
        ),
        (
            _raising(InvalidURL("not a target")),
            InferenceFailure.CONFIGURATION,
            "inference request URL is not a valid HTTP request target",
        ),
        (
            None,
            InferenceFailure.TRANSPORT,
            "HTTP response does not expose a readable body",
        ),
        (_text, InferenceFailure.TRANSPORT, "HTTP response body was not bytes"),
    ],
    ids=[
        "reset",
        "timeout",
        "incomplete-read",
        "value-error",
        "unicode-error",
        "invalid-url",
        "no-read",
        "not-bytes",
    ],
)
@pytest.mark.parametrize("deadline", [None, 5.0])
def test_every_failure_reading_the_body_names_the_status(
    monkeypatch: pytest.MonkeyPatch,
    read: object,
    failure: InferenceFailure,
    detail: str,
    deadline: float | None,
) -> None:
    _use_opener(monkeypatch, lambda tracker: _ScriptedOpener(_ScriptedResponse(read)))
    request = replace(_request("http://127.0.0.1:9/v1"), deadline_seconds=deadline)

    with pytest.raises(InferenceError) as raised:
        UrllibHttpTransport().send(request)

    assert raised.value.failure is failure
    assert raised.value.detail == detail
    assert raised.value.status_code == 200


@pytest.mark.parametrize("completes", [False, True], ids=["read-fails", "completes"])
def test_a_deadline_that_passes_during_the_body_keeps_the_status(
    monkeypatch: pytest.MonkeyPatch,
    completes: bool,
) -> None:
    def opener_for(tracker: _SocketTracker | None) -> object:
        assert tracker is not None

        def read(amount: int) -> bytes:
            del amount
            tracker.expire()
            if completes:
                return b"{}"
            raise ConnectionResetError("shut down by the deadline")

        return _ScriptedOpener(_ScriptedResponse(read))

    _use_opener(monkeypatch, opener_for)
    request = replace(_request("http://127.0.0.1:9/v1"), deadline_seconds=5.0)

    with pytest.raises(InferenceError) as raised:
        UrllibHttpTransport().send(request)

    assert raised.value.failure is InferenceFailure.TRANSPORT
    assert raised.value.detail == DEADLINE_DETAIL
    assert raised.value.status_code == 200


def test_a_deadline_that_passes_before_the_status_line_names_no_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExpiringOpener:
        def __init__(self, tracker: _SocketTracker) -> None:
            self.tracker = tracker

        def open(self, request: object, timeout: float | None = None) -> object:
            del request, timeout
            self.tracker.expire()
            raise URLError(ConnectionResetError("shut down by the deadline"))

    def opener_for(tracker: _SocketTracker | None) -> object:
        assert tracker is not None
        return ExpiringOpener(tracker)

    _use_opener(monkeypatch, opener_for)
    request = replace(_request("http://127.0.0.1:9/v1"), deadline_seconds=5.0)

    with pytest.raises(InferenceError) as raised:
        UrllibHttpTransport().send(request)

    assert raised.value.detail == DEADLINE_DETAIL
    assert raised.value.status_code is None
