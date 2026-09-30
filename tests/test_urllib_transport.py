# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import socket
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest

from itself import (
    HttpRequest,
    InferenceError,
    InferenceFailure,
    UrllibHttpTransport,
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
    thread = Thread(target=server.serve_forever, daemon=True)
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
