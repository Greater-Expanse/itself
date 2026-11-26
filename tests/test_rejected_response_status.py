# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Record the status of a response the shipped transport rejects.

The shipped transport names the status of a response it rejects after its
headers arrived, such as one over its byte limit.  A v2 attempt report
keeps that status.  A v1 attempt report holds a status only for an HTTP-status
failure, so it records null for these failures.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import cast

import pytest

import experiments.cases.cache_key_model_run as model_run
from experiments._bundle_validation import pretty_json_bytes
from experiments._study_contracts import V2_CONTRACT
from experiments.cases.cache_key_comparison import (
    NARRATIVE_ATTEMPT_ID,
    STRUCTURED_ATTEMPT_ID,
    build_comparison_request,
)
from experiments.cases.cache_key_comparison_run import run_cache_key_comparison
from experiments.cases.cache_key_comparison_study import (
    STUDY_TARGETS_BY_VERSION,
    ComparisonStudyTarget,
    build_study_manifest,
)
from experiments.cases.cache_key_exchange import build_exchange_request
from experiments.cases.cache_key_omission import CaseEnvironment, Mechanism
from experiments.cases.cache_key_scripted_run import SCRIPTED_RUN_METRICS
from experiments.comparison_bundles import (
    ComparisonBundleBuilder,
    ComparisonBundleValidationError,
    ComparisonBundleValidator,
    VerifiedFailedComparisonBundle,
)
from experiments.diagnostician import DiagnosisRequest
from experiments.model_adapters import (
    HttpRequest,
    HttpResponse,
    HttpTransport,
    ModelAdapterError,
    ModelAdapterFailure,
    StatusObservingTransport,
    UrllibHttpTransport,
    running_shipped_adapter,
)
from experiments.trial_bundles import (
    TrialBundleBuilder,
    TrialBundleValidationError,
    TrialBundleValidator,
    VerifiedFailedTrialBundle,
)
from experiments.trial_manifests import load_trial_manifest
from itself import JsonObject
from itself.inference import InferenceError, InferenceFailure

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "experiments" / "contracts"
TOGETHER = "https://api.together.ai/v1"
SECRET = "rejected-response-secret-that-must-not-leak"
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
BODY = b'{"choices":[{"message":{"content":"a body the transport rejects"}}]}'
LIMIT = 16

# Each way the shipped transport rejects a response after its headers,
# with the failure kind it reports and whether that kind is retryable.
REJECTIONS = [
    pytest.param("over-limit", "response_too_large", False, id="over-limit"),
    pytest.param("ends-early", "transport", True, id="ends-early"),
]


def _handler(status: int, rejection: str) -> type[BaseHTTPRequestHandler]:
    class RejectedHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            # Reading the request first makes the close a clean FIN, not a reset.
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            # Over the limit, the whole body; ending early, less than declared.
            self.wfile.write(BODY if rejection == "over-limit" else BODY[: LIMIT // 2])

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return RejectedHandler


@contextmanager
def _serve(status: int, rejection: str) -> Generator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(status, rejection))
    thread = Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _requests() -> list[HttpRequest]:
    return []


@dataclass(slots=True)
class LoopbackTransport:
    """Send each request through the shipped transport to a loopback server."""

    url: str
    transport: UrllibHttpTransport = field(
        default_factory=lambda: UrllibHttpTransport(max_response_bytes=LIMIT)
    )
    requests: list[HttpRequest] = field(default_factory=_requests)

    def send(self, request: HttpRequest, /) -> HttpResponse:
        self.requests.append(request)
        return self.transport.send(replace(request, url=self.url))


@dataclass(slots=True)
class RaisingTransport:
    error: InferenceError

    def send(self, request: HttpRequest, /) -> HttpResponse:
        del request
        raise self.error


@dataclass(slots=True)
class AnsweringTransport:
    """Answer every request with one status and a complete body."""

    status: int

    def send(self, request: HttpRequest, /) -> HttpResponse:
        del request
        return HttpResponse(
            status_code=self.status,
            headers={"Content-Type": "application/json"},
            body=b'{"error":"refused"}',
        )


def _comparison_request() -> DiagnosisRequest:
    context = CaseEnvironment(
        Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION
    ).diagnostic_context()
    return build_comparison_request(context)


def _comparison_target(contract: str) -> ComparisonStudyTarget:
    registered = STUDY_TARGETS_BY_VERSION["5"][0]
    if contract == "v1":
        return registered
    return replace(
        registered,
        contract=V2_CONTRACT,
        shipped_adapter=running_shipped_adapter(),
        stream_include_usage=True,
        deadline_seconds=300.0,
    )


def _comparison_manifest(tmp_path: Path, contract: str) -> Path:
    path = tmp_path / f"comparison-manifest-{contract}.json"
    target = _comparison_target(contract)
    path.write_bytes(pretty_json_bytes(build_study_manifest(target, registered_at=NOW)))
    return path


def _failure(attempt_report: JsonObject) -> JsonObject:
    failure = attempt_report["failure"]
    assert isinstance(failure, dict)
    return failure


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, "http_status"),
        (401, "http_authentication"),
        (403, "http_authorization"),
        (404, "http_status"),
        (429, "http_rate_limit"),
        (500, "http_server"),
        (599, "http_server"),
    ],
)
def test_a_v1_trial_records_the_status_of_every_http_status_failure(
    tmp_path: Path,
    status: int,
    kind: str,
) -> None:
    bundle = tmp_path / "bundle"
    model_run.run_cache_key_model_case(
        CONTRACTS / "v1" / "examples" / "trial-manifest.json",
        bundle,
        base_url=TOGETHER,
        api_key_env="TOGETHER_API_KEY",
        transport=AnsweringTransport(status),
        environment={"TOGETHER_API_KEY": SECRET},
        now=lambda: NOW,
        monotonic_ns=itertools.count(0, 1_000_000).__next__,
    )
    verified = TrialBundleValidator().validate(bundle, build_exchange_request())
    assert isinstance(verified, VerifiedFailedTrialBundle)
    failure = _failure(verified.attempt_report)

    assert failure["kind"] == kind
    assert failure["status_code"] == status


@pytest.mark.parametrize("status", [200, 503])
@pytest.mark.parametrize(("rejection", "kind", "retryable"), REJECTIONS)
@pytest.mark.parametrize(
    ("contract", "recorded"),
    [("v1", False), ("v2", True)],
)
def test_a_trial_records_a_rejected_response_status_only_under_v2(
    tmp_path: Path,
    status: int,
    rejection: str,
    kind: str,
    retryable: bool,
    contract: str,
    recorded: bool,
) -> None:
    bundle = tmp_path / "bundle"
    with _serve(status, rejection) as url:
        transport = LoopbackTransport(url)
        bundle_manifest = model_run.run_cache_key_model_case(
            CONTRACTS / contract / "examples" / "trial-manifest.json",
            bundle,
            base_url=TOGETHER,
            api_key_env="TOGETHER_API_KEY",
            transport=transport,
            environment={"TOGETHER_API_KEY": SECRET},
            now=lambda: NOW,
            monotonic_ns=itertools.count(0, 1_000_000).__next__,
        )
    verified = TrialBundleValidator().validate(bundle, build_exchange_request())
    assert isinstance(verified, VerifiedFailedTrialBundle)
    failure = _failure(verified.attempt_report)

    assert len(transport.requests) == 1
    assert bundle_manifest["outcome"] == "adapter_failed"
    assert failure["kind"] == kind
    assert failure["retryable"] is retryable
    assert failure["status_code"] == (status if recorded else None)


@pytest.mark.parametrize("status", [200, 503])
@pytest.mark.parametrize(("rejection", "kind", "retryable"), REJECTIONS)
@pytest.mark.parametrize(
    ("contract", "recorded"),
    [("v1", False), ("v2", True)],
)
def test_a_comparison_records_a_rejected_response_status_only_under_v2(
    tmp_path: Path,
    status: int,
    rejection: str,
    kind: str,
    retryable: bool,
    contract: str,
    recorded: bool,
) -> None:
    target = _comparison_target(contract)
    bundle = tmp_path / "replicate-001"
    with _serve(status, rejection) as url:
        transport = LoopbackTransport(url)
        bundle_manifest = run_cache_key_comparison(
            _comparison_manifest(tmp_path, contract),
            bundle,
            replicate_index=1,
            base_url=target.base_url,
            api_key_env=target.api_key_env,
            transport=transport,
            environment={target.api_key_env: SECRET},
            now=lambda: NOW,
            monotonic_ns=itertools.count(1_000_000, 1_000_000).__next__,
        )
    verified = ComparisonBundleValidator().validate(bundle, _comparison_request())
    assert isinstance(verified, VerifiedFailedComparisonBundle)
    failure = _failure(verified.attempt_report)

    assert len(transport.requests) == 1
    assert bundle_manifest["attempt_outcomes"] == {
        NARRATIVE_ATTEMPT_ID: "adapter_failed",
        STRUCTURED_ATTEMPT_ID: "not_run",
    }
    assert failure["kind"] == kind
    assert failure["retryable"] is retryable
    assert failure["status_code"] == (status if recorded else None)


def _rejection(status_code: int | None) -> InferenceError:
    return InferenceError(
        InferenceFailure.RESPONSE_TOO_LARGE,
        "inference endpoint response exceeded the configured byte limit",
        status_code=status_code,
    )


def _adapter_error(status_code: int) -> ModelAdapterError:
    return ModelAdapterError(
        ModelAdapterFailure.RESPONSE_TOO_LARGE,
        "inference endpoint response exceeded the configured byte limit",
        status_code=status_code,
    )


def _send(transport: HttpTransport) -> None:
    with pytest.raises(InferenceError):
        transport.send(
            HttpRequest(url=TOGETHER, headers={}, body=b"{}", timeout_seconds=1.0)
        )


def test_the_observer_keeps_the_status_a_transport_error_names() -> None:
    error = _rejection(200)
    observer = StatusObservingTransport(RaisingTransport(error))

    with pytest.raises(InferenceError) as raised:
        observer.send(
            HttpRequest(url=TOGETHER, headers={}, body=b"{}", timeout_seconds=1.0)
        )

    assert raised.value is error
    assert observer.request_sent
    assert observer.status_code == 200


def test_an_error_that_names_no_status_leaves_the_last_observed_one() -> None:
    raising = RaisingTransport(_rejection(503))
    observer = StatusObservingTransport(raising)
    _send(observer)
    raising.error = _rejection(None)
    _send(observer)

    assert observer.status_code == 503


def test_a_trial_status_from_a_transport_error_must_match_the_adapter(
    tmp_path: Path,
) -> None:
    builder = TrialBundleBuilder(
        tmp_path / "bundle",
        load_trial_manifest(CONTRACTS / "v2" / "examples" / "trial-manifest.json"),
        now=lambda: NOW,
        metric_fields=SCRIPTED_RUN_METRICS,
    )
    with pytest.raises(TrialBundleValidationError, match="observed"), builder:
        _send(builder.observed_transport(RaisingTransport(_rejection(200))))
        builder.fail(_adapter_error(503))


def test_a_comparison_status_from_a_transport_error_must_match_the_adapter(
    tmp_path: Path,
) -> None:
    manifest = cast(
        JsonObject,
        json.loads(_comparison_manifest(tmp_path, "v2").read_text("utf-8")),
    )
    builder = ComparisonBundleBuilder(
        tmp_path / "replicate-001",
        manifest,
        _comparison_request(),
        replicate_index=1,
        now=lambda: NOW,
    )
    with pytest.raises(ComparisonBundleValidationError, match="observed"), builder:
        _send(
            builder.observed_transport(
                RaisingTransport(_rejection(200)),
                attempt_id=NARRATIVE_ATTEMPT_ID,
            )
        )
        builder.fail(_adapter_error(503), attempt_id=NARRATIVE_ATTEMPT_ID)
