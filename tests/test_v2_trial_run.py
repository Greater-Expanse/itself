# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Run v2 trials end to end over fake transports."""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import experiments.cases.cache_key_model_run as model_run
import experiments.trial_bundles as trial_bundles
import itself.inference as shipped_inference
from experiments._bundle_validation import (
    AttemptQuarantinedError,
    file_entry,
    pretty_json_bytes,
    quarantine_path,
)
from experiments._study_contracts import RunnerFailure
from experiments.cases.cache_key_diagnostician import CacheKeyFixtureDiagnostician
from experiments.cases.cache_key_exchange import build_exchange_request
from experiments.cases.cache_key_scripted_run import (
    SCRIPTED_RUN_METRICS,
    ScriptedRunResult,
    run_scripted_case,
)
from experiments.diagnostician import Diagnostician
from experiments.model_adapters import (
    HttpRequest,
    HttpResponse,
    ModelAdapterError,
    ModelAdapterFailure,
    ShippedAdapterIdentity,
    running_shipped_adapter,
)
from experiments.trial_bundles import (
    TrialBundleBuilder,
    TrialBundleStateError,
    TrialBundleValidationError,
    TrialBundleValidator,
    VerifiedFailedTrialBundle,
)
from experiments.trial_manifests import (
    TrialManifestValidationError,
    build_trial_manifest,
    canonical_json_bytes,
    endpoint_from_trial_manifest,
    load_trial_manifest,
)
from itself import JsonObject

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = (
    ROOT / "experiments" / "contracts" / "v2" / "examples" / "trial-manifest.json"
)
V1_MANIFEST_PATH = (
    ROOT / "experiments" / "contracts" / "v1" / "examples" / "trial-manifest.json"
)
TOGETHER = "https://api.together.ai/v1"
SECRET = "v2-trial-secret-that-must-not-leak"
NOW = datetime(2026, 11, 26, 12, 5, tzinfo=UTC)


def _requests() -> list[HttpRequest]:
    return []


@dataclass(slots=True)
class RecordingTransport:
    response: HttpResponse | Exception
    requests: list[HttpRequest] = field(default_factory=_requests)

    def send(self, request: HttpRequest, /) -> HttpResponse:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _sse(events: list[JsonObject]) -> bytes:
    return (
        b"".join(
            b"data: " + json.dumps(event, separators=(",", ":")).encode() + b"\n\n"
            for event in events
        )
        + b"data: [DONE]\n\n"
    )


def _streamed_completion() -> bytes:
    assertion = CacheKeyFixtureDiagnostician().diagnose(build_exchange_request())
    content = json.dumps(assertion.assertion.to_model_json_object())
    chunk: JsonObject = {"id": "v2", "model": "openai/gpt-oss-20b"}
    return _sse(
        [
            {
                **chunk,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": None,
                        "delta": {"role": "assistant", "content": content},
                    }
                ],
                "usage": None,
            },
            {
                **chunk,
                "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}],
                "usage": None,
            },
            # The usage the provider sends only when the request asks for it.
            {
                **chunk,
                "choices": [],
                "usage": {
                    "prompt_tokens": 157,
                    "completion_tokens": 426,
                    "total_tokens": 583,
                },
            },
        ]
    )


def _stream_response(status: int = 200, body: bytes | None = None) -> HttpResponse:
    return HttpResponse(
        status,
        {"Content-Type": "text/event-stream"},
        _streamed_completion() if body is None else body,
    )


def _run(
    transport: RecordingTransport,
    bundle: Path,
    *,
    manifest: Path = MANIFEST_PATH,
) -> JsonObject:
    return model_run.run_cache_key_model_case(
        manifest,
        bundle,
        base_url=TOGETHER,
        api_key_env="TOGETHER_API_KEY",
        transport=transport,
        environment={"TOGETHER_API_KEY": SECRET},
        now=lambda: NOW,
        monotonic_ns=iter(range(0, 10**12, 10**6)).__next__,
    )


def _failed(bundle: Path) -> VerifiedFailedTrialBundle:
    verified = TrialBundleValidator().validate(bundle, build_exchange_request())
    assert isinstance(verified, VerifiedFailedTrialBundle)
    return verified


def _failure(verified: VerifiedFailedTrialBundle) -> JsonObject:
    failure = verified.attempt_report["failure"]
    assert isinstance(failure, dict)
    return failure


def _fail_after_the_response(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    def failing(adapter: Diagnostician) -> ScriptedRunResult:
        run_scripted_case(adapter)
        raise error

    monkeypatch.setattr(model_run, "run_scripted_case", failing)


def _all_text(root: Path) -> str:
    return "".join(
        path.read_text("utf-8", errors="replace")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def _rewrite_bundle(
    bundle: Path,
    edit: Callable[[JsonObject], None],
    *,
    attempt_report: Callable[[JsonObject], None] | None = None,
) -> None:
    """Rewrite a bundle consistently, so only the edited binding is wrong."""

    bundle.chmod(0o700)
    manifest_path = bundle / "bundle.json"
    manifest = cast(JsonObject, json.loads(manifest_path.read_text("utf-8")))
    if attempt_report is not None:
        report_path = bundle / "attempt-report.json"
        report = cast(JsonObject, json.loads(report_path.read_text("utf-8")))
        attempt_report(report)
        report_path.chmod(0o600)
        report_path.write_bytes(pretty_json_bytes(report))
        files = cast(list[JsonObject], manifest["files"])
        index = next(
            position
            for position, entry in enumerate(files)
            if entry["path"] == "attempt-report.json"
        )
        files[index] = file_entry(
            bundle,
            "attempt-report.json",
            media_type="application/json",
            visibility="shareable",
            error_type=TrialBundleValidationError,
        )
    edit(manifest)
    del manifest["bundle_id"]
    manifest = {
        "bundle_id": "urn:sha256:"
        + hashlib.sha256(canonical_json_bytes(manifest)).hexdigest(),
        **manifest,
    }
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(pretty_json_bytes(manifest))


def _other_client(value: JsonObject, field: str = "shipped_adapter") -> None:
    recorded = value[field]
    assert isinstance(recorded, dict)
    recorded["adapter_version"] = "9.9.9"


# A completed v2 trial: its deadline, shipped client, and stream usage.


def test_a_v2_trial_streams_asks_for_usage_and_records_its_client(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport(_stream_response())
    bundle_manifest = _run(transport, tmp_path / "bundle")
    verified = TrialBundleValidator().validate_completed(
        tmp_path / "bundle",
        build_exchange_request(),
    )

    assert bundle_manifest["outcome"] == "completed"
    assert bundle_manifest["bundle_version"] == "0.3.0"
    assert bundle_manifest["shipped_adapter"] == (
        running_shipped_adapter().to_json_object()
    )
    assert verified.scorecard["model_input_tokens"] == 157
    assert verified.scorecard["model_output_tokens"] == 426
    (request,) = transport.requests
    payload = cast(JsonObject, json.loads(request.body))
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert request.deadline_seconds == 300.0
    assert request.timeout_seconds == 90.0


def test_a_v1_trial_still_sends_no_deadline_and_no_usage_request(
    tmp_path: Path,
) -> None:
    body = json.loads(_streamed_completion().split(b"\n\n")[0].removeprefix(b"data: "))
    content = body["choices"][0]["delta"]["content"]
    envelope = {
        "id": "v1",
        "model": "openai/gpt-oss-20b",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    transport = RecordingTransport(
        HttpResponse(
            200,
            {"Content-Type": "application/json"},
            json.dumps(envelope).encode(),
        )
    )
    bundle_manifest = _run(transport, tmp_path / "bundle", manifest=V1_MANIFEST_PATH)

    assert bundle_manifest["bundle_version"] == "0.2.0"
    assert "shipped_adapter" not in bundle_manifest
    (request,) = transport.requests
    assert request.deadline_seconds is None
    assert "stream_options" not in json.loads(request.body)


def test_the_runner_refuses_a_different_running_client_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shipped_inference, "ADAPTER_VERSION", "0.5.0")
    transport = RecordingTransport(_stream_response())

    with pytest.raises(TrialManifestValidationError, match="running shipped client"):
        _run(transport, tmp_path / "bundle")
    assert transport.requests == []
    assert list(tmp_path.iterdir()) == []


def test_the_recorded_client_is_read_from_the_shipped_package_at_run_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned = ShippedAdapterIdentity(
        adapter_id="openai-compatible-chat-completions",
        adapter_version="0.5.0",
    )
    example = load_trial_manifest(MANIFEST_PATH)
    endpoint = endpoint_from_trial_manifest(example, base_url=TOGETHER, api_key_env="K")
    manifest = build_trial_manifest(
        endpoint,
        build_exchange_request(),
        trial_series_id="v2-recorded-client",
        registered_at=NOW,
        study_phase="engineering_pilot",
        metrics=cast(list[str], example["metrics"]),
        limitations=["A limitation."],
        shipped_adapter=pinned,
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(pretty_json_bytes(manifest))
    monkeypatch.setattr(shipped_inference, "ADAPTER_VERSION", "0.5.0")
    transport = RecordingTransport(_stream_response(status=503, body=b"{}"))

    bundle_manifest = _run(transport, tmp_path / "bundle", manifest=manifest_path)
    monkeypatch.undo()

    assert transport.requests[0].headers["User-Agent"] == "itself-inference/0.5.0"
    assert bundle_manifest["shipped_adapter"] == pinned.to_json_object()
    verified = _failed(tmp_path / "bundle")
    assert verified.attempt_report["shipped_adapter"] == pinned.to_json_object()


def test_validation_checks_the_recorded_client_against_the_pin(
    tmp_path: Path,
) -> None:
    _run(RecordingTransport(_stream_response()), tmp_path / "completed")
    _rewrite_bundle(tmp_path / "completed", _other_client)
    with pytest.raises(TrialBundleValidationError, match="bundle shipped_adapter"):
        TrialBundleValidator().validate(
            tmp_path / "completed", build_exchange_request()
        )

    _run(RecordingTransport(_stream_response(status=503, body=b"{}")), tmp_path / "a")
    _rewrite_bundle(tmp_path / "a", _other_client)
    with pytest.raises(TrialBundleValidationError, match="bundle shipped_adapter"):
        _failed(tmp_path / "a")

    _run(RecordingTransport(_stream_response(status=503, body=b"{}")), tmp_path / "b")
    _rewrite_bundle(tmp_path / "b", lambda _: None, attempt_report=_other_client)
    with pytest.raises(TrialBundleValidationError, match="attempt report shipped"):
        _failed(tmp_path / "b")


def test_a_bundle_must_declare_its_trial_manifest_version(tmp_path: Path) -> None:
    _run(RecordingTransport(_stream_response(status=503, body=b"{}")), tmp_path / "b")

    def as_v1(manifest: JsonObject) -> None:
        manifest["bundle_version"] = "0.2.0"
        del manifest["shipped_adapter"]

    _rewrite_bundle(tmp_path / "b", as_v1)
    with pytest.raises(TrialBundleValidationError, match="bundle_version"):
        _failed(tmp_path / "b")


def test_an_attempt_report_must_declare_its_bundle_version(tmp_path: Path) -> None:
    _run(RecordingTransport(_stream_response(status=503, body=b"{}")), tmp_path / "b")

    def as_v1(report: JsonObject) -> None:
        report["attempt_report_version"] = "0.2.0"
        del report["shipped_adapter"]

    _rewrite_bundle(tmp_path / "b", lambda _: None, attempt_report=as_v1)
    with pytest.raises(TrialBundleValidationError, match="report version"):
        _failed(tmp_path / "b")


def test_an_attempt_report_must_have_its_bundle_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_after_the_response(monkeypatch, RuntimeError("scorer failed"))
    _run(RecordingTransport(_stream_response()), tmp_path / "b")

    def as_adapter_failure(manifest: JsonObject) -> None:
        manifest["outcome"] = "adapter_failed"

    _rewrite_bundle(tmp_path / "b", as_adapter_failure)
    with pytest.raises(TrialBundleValidationError, match="report outcome"):
        _failed(tmp_path / "b")


# The observed HTTP status.


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (HttpResponse(999, {"Content-Type": "application/json"}, b"{}"), 999),
        (HttpResponse(42, {"Content-Type": "application/json"}, b"{}"), 42),
        (HttpResponse(200, {"Content-Type": "text/event-stream"}, b"data: {"), 200),
        (ConnectionResetError("reset by peer"), None),
    ],
)
def test_a_v2_adapter_failure_keeps_the_status_the_transport_observed(
    tmp_path: Path,
    response: HttpResponse | Exception,
    status: int | None,
) -> None:
    bundle_manifest = _run(RecordingTransport(response), tmp_path / "bundle")
    verified = _failed(tmp_path / "bundle")

    assert bundle_manifest["outcome"] == "adapter_failed"
    assert _failure(verified)["status_code"] == status


def test_a_v1_adapter_failure_still_clamps_its_status(tmp_path: Path) -> None:
    transport = RecordingTransport(
        HttpResponse(999, {"Content-Type": "application/json"}, b"{}")
    )
    _run(transport, tmp_path / "bundle", manifest=V1_MANIFEST_PATH)

    assert _failure(_failed(tmp_path / "bundle"))["status_code"] is None


def test_a_status_the_adapter_reports_must_be_the_one_observed(
    tmp_path: Path,
) -> None:
    manifest = load_trial_manifest(MANIFEST_PATH)
    builder = TrialBundleBuilder(
        tmp_path / "bundle",
        manifest,
        now=lambda: NOW,
        metric_fields=SCRIPTED_RUN_METRICS,
    )
    with pytest.raises(TrialBundleValidationError, match="observed"), builder:
        transport = builder.observed_transport(
            RecordingTransport(HttpResponse(503, {}, b""))
        )
        transport.send(
            HttpRequest(url=TOGETHER, headers={}, body=b"{}", timeout_seconds=1.0)
        )
        builder.fail(_adapter_error(status_code=500))


def _adapter_error(*, status_code: int | None) -> ModelAdapterError:
    return ModelAdapterError(
        ModelAdapterFailure.HTTP_STATUS,
        "provider failed",
        status_code=status_code,
    )


def test_only_a_v2_builder_observes_its_transport(tmp_path: Path) -> None:
    manifest = load_trial_manifest(V1_MANIFEST_PATH)
    with TrialBundleBuilder(
        tmp_path / "bundle",
        manifest,
        now=lambda: NOW,
        metric_fields=SCRIPTED_RUN_METRICS,
    ) as builder:
        with pytest.raises(TrialBundleStateError, match="only a v0.3.0"):
            builder.observed_transport(RecordingTransport(_stream_response()))
        assert builder.records_runner_failure is False


# A runner failure after the request.


def test_a_runner_failure_publishes_a_valid_bundle_with_its_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_after_the_response(
        monkeypatch,
        RuntimeError(f"scorer failed using {SECRET}\nwith a second line"),
    )
    bundle_manifest = _run(RecordingTransport(_stream_response()), tmp_path / "bundle")
    verified = _failed(tmp_path / "bundle")
    failure = _failure(verified)

    assert bundle_manifest["outcome"] == "runner_failed"
    assert verified.attempt_report["outcome"] == "runner_failed"
    assert failure["kind"] == "runner_error"
    assert failure["retryable"] is False
    assert failure["status_code"] == 200
    assert failure["error"] == {
        "type": "RuntimeError",
        "message": "scorer failed using [redacted]; with a second line",
    }
    artifact = failure["response_artifact"]
    assert isinstance(artifact, dict)
    uri = artifact["uri"]
    assert isinstance(uri, str)
    assert (tmp_path / "bundle" / uri).read_bytes() == _streamed_completion()
    assert SECRET not in _all_text(tmp_path / "bundle")


def test_a_runner_failure_removes_what_an_unfinished_completion_wrote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_disk(*_: object) -> bytes:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(trial_bundles, "canonical_ledger_bytes", no_disk)
    bundle_manifest = _run(RecordingTransport(_stream_response()), tmp_path / "bundle")
    verified = _failed(tmp_path / "bundle")

    assert bundle_manifest["outcome"] == "runner_failed"
    assert _failure(verified)["error"] == {
        "type": "OSError",
        "message": "[Errno 28] No space left on device",
    }
    assert not (tmp_path / "bundle" / "diagnosis-result.json").exists()


def test_a_failure_before_the_request_is_not_a_runner_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def before_request(_: Diagnostician) -> ScriptedRunResult:
        raise RuntimeError("the case could not be prepared")

    monkeypatch.setattr(model_run, "run_scripted_case", before_request)
    transport = RecordingTransport(_stream_response())

    with pytest.raises(RuntimeError, match="could not be prepared"):
        _run(transport, tmp_path / "bundle")
    assert transport.requests == []
    assert list(tmp_path.iterdir()) == []


def test_an_interrupt_after_the_request_is_published_and_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_after_the_response(monkeypatch, KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        _run(RecordingTransport(_stream_response()), tmp_path / "bundle")
    failure = _failure(_failed(tmp_path / "bundle"))

    assert failure["kind"] == "interrupted"
    assert failure["error"] == {
        "type": "KeyboardInterrupt",
        "message": "KeyboardInterrupt",
    }


def _refuse_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object) -> None:
        raise OSError("destination is read-only")

    monkeypatch.setattr(trial_bundles, "publish_path_no_replace", refuse)


def test_a_runner_failure_that_cannot_be_published_is_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_after_the_response(monkeypatch, RuntimeError("scorer failed"))
    _refuse_publication(monkeypatch)
    bundle = tmp_path / "bundle"

    with pytest.raises(AttemptQuarantinedError):
        _run(RecordingTransport(_stream_response()), bundle)
    assert not bundle.exists()
    assert any(quarantine_path(bundle).joinpath("artifacts").iterdir())


def test_an_interrupt_that_cannot_be_published_is_quarantined_and_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_after_the_response(monkeypatch, KeyboardInterrupt())
    _refuse_publication(monkeypatch)
    bundle = tmp_path / "bundle"

    with pytest.raises(KeyboardInterrupt) as raised:
        _run(RecordingTransport(_stream_response()), bundle)
    assert raised.value.__notes__ == [
        "the interrupted attempt could not be published: destination is read-only"
    ]
    assert any(quarantine_path(bundle).joinpath("artifacts").iterdir())


def test_a_v1_runner_failure_is_still_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = RecordingTransport(
        HttpResponse(200, {"Content-Type": "application/json"}, b"{}")
    )
    monkeypatch.setattr(
        model_run,
        "run_scripted_case",
        _raises_after_send(RuntimeError("scorer failed")),
    )
    bundle = tmp_path / "bundle"

    with pytest.raises(AttemptQuarantinedError):
        _run(transport, bundle, manifest=V1_MANIFEST_PATH)
    assert not bundle.exists()
    assert quarantine_path(bundle).exists()


def _raises_after_send(
    error: Exception,
) -> Callable[[Diagnostician], ScriptedRunResult]:
    def run(adapter: Diagnostician) -> ScriptedRunResult:
        with contextlib.suppress(ModelAdapterError):
            adapter.diagnose(build_exchange_request())
        raise error

    return run


def test_fail_runner_needs_a_sent_v2_request(tmp_path: Path) -> None:
    manifest = load_trial_manifest(MANIFEST_PATH)
    with TrialBundleBuilder(
        tmp_path / "bundle",
        manifest,
        now=lambda: NOW,
        metric_fields=SCRIPTED_RUN_METRICS,
    ) as builder:
        builder.observed_transport(RecordingTransport(_stream_response()))
        assert builder.records_runner_failure is False
        with pytest.raises(TrialBundleStateError, match="request was sent"):
            builder.fail_runner(RunnerFailure.from_exception(RuntimeError("x")))
