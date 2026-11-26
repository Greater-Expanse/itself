# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""The compatible client's HTTP boundary and its standard-library transport."""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from email.message import Message
from functools import cache
from http.client import (
    HTTPConnection,
    HTTPException,
    HTTPMessage,
    HTTPResponse,
    HTTPSConnection,
    InvalidURL,
)
from types import MappingProxyType
from typing import IO, Protocol, cast
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

from ._endpoints import (
    is_loopback_hostname,
    validate_deadline,
    validate_timeout,
)
from ._inference_errors import InferenceError, InferenceFailure


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
    """Minimal synchronous HTTP boundary used by the compatible client.

    A transport may raise ``InferenceError``, which the client passes on
    unchanged.  When it rejects a response whose status it knows, it may
    give that status as the error's ``status_code``; it need not.
    """

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
        limit = cast(object, self.max_response_bytes)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("max_response_bytes must be a positive integer")

    def _read_body(self, response: object, status_code: int) -> bytes:
        read = getattr(response, "read", None)
        if not callable(read):
            raise InferenceError(
                InferenceFailure.TRANSPORT,
                "HTTP response does not expose a readable body",
                status_code=status_code,
            )
        body: object = read(self.max_response_bytes + 1)
        if not isinstance(body, bytes):
            raise InferenceError(
                InferenceFailure.TRANSPORT,
                "HTTP response body was not bytes",
                status_code=status_code,
            )
        if len(body) > self.max_response_bytes:
            raise InferenceError(
                InferenceFailure.RESPONSE_TOO_LARGE,
                "inference endpoint response exceeded the configured byte limit",
                status_code=status_code,
            )
        # A bounded read does not raise when a declared Content-Length is cut
        # short, so the undelivered remainder is checked explicitly.
        remaining: object = getattr(response, "length", None)
        if isinstance(remaining, int) and remaining > 0:
            raise InferenceError(
                InferenceFailure.TRANSPORT,
                "inference endpoint response ended before its declared length",
                status_code=status_code,
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

        A failure raised once the response's headers have arrived, such as a
        body over the byte limit or one cut short, a read that fails, or a
        deadline that passes while the body is read, carries the response's
        status in ``status_code``.  A failure before the status line arrives
        carries ``None``, and one while the headers are still arriving may
        carry either.
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
        except InferenceError as error:
            if tracker.expired.is_set():
                raise InferenceError(
                    InferenceFailure.TRANSPORT,
                    "inference endpoint exceeded the request deadline",
                    status_code=error.status_code,
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
                status_code=response.status_code,
            )
        return response

    def _exchange(
        self,
        opener: OpenerDirector,
        wire_request: Request,
        timeout_seconds: float,
    ) -> HttpResponse:
        # Set once the headers have arrived, so that every failure while the
        # body is read names the status the server answered.
        status_code: int | None = None
        try:
            try:
                with opener.open(
                    wire_request,
                    timeout=timeout_seconds,
                ) as response:
                    status_code = cast(int, response.status)
                    return HttpResponse(
                        status_code=status_code,
                        headers=self._headers(response),
                        body=self._read_body(response, status_code),
                    )
            except HTTPError as error:
                with error:
                    status_code = error.code
                    return HttpResponse(
                        status_code=status_code,
                        headers=self._headers(error),
                        body=self._read_body(error, status_code),
                    )
        except InvalidURL:
            raise InferenceError(
                InferenceFailure.CONFIGURATION,
                "inference request URL is not a valid HTTP request target",
                status_code=status_code,
            ) from None
        except UnicodeError:
            raise InferenceError(
                InferenceFailure.CONFIGURATION,
                "inference request URL or headers cannot be encoded for HTTP",
                status_code=status_code,
            ) from None
        except ValueError:
            # http.client and urllib raise ValueError for a request they cannot
            # form, such as a header value with a line break, and may quote the
            # value, which may be a credential, in the message.
            raise InferenceError(
                InferenceFailure.CONFIGURATION,
                "inference request could not be formed as valid HTTP",
                status_code=status_code,
            ) from None
        except (TimeoutError, URLError, OSError, HTTPException) as error:
            raise InferenceError(
                InferenceFailure.TRANSPORT,
                "inference endpoint request failed",
                status_code=status_code,
            ) from error


# Callers import these from itself.inference, which they keep as their module
# so that pickles, tracebacks, and reprs name the same place as before.
HttpRequest.__module__ = "itself.inference"
HttpResponse.__module__ = "itself.inference"
HttpTransport.__module__ = "itself.inference"
UrllibHttpTransport.__module__ = "itself.inference"
