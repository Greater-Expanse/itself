# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

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
