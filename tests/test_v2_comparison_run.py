# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

# pyright: reportPrivateUsage=false

"""Run and report v2 paired comparisons end to end over fake transports."""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import experiments.cases.cache_key_comparison_report as report_module
import experiments.cases.cache_key_comparison_run as comparison_run
import experiments.comparison_bundles as comparison_bundles
import itself.inference as shipped_inference
from experiments._bundle_validation import pretty_json_bytes
from experiments._study_contracts import V2_CONTRACT, RunnerFailure
from experiments.cases.cache_key_comparison import (
    NARRATIVE_ATTEMPT_ID,
    STRUCTURED_ATTEMPT_ID,
    ComparativeRunResult,
    ComparisonCondition,
)
from experiments.cases.cache_key_comparison_report import (
    build_comparison_report,
    render_markdown,
)
from experiments.cases.cache_key_comparison_run import run_cache_key_comparison
from experiments.cases.cache_key_comparison_study import (
    STUDY_TARGETS_BY_VERSION,
    ComparisonStudyTarget,
    build_study_manifest,
)
from experiments.cases.cache_key_diagnostician import (
    CacheKeyFixtureDiagnostician,
    CacheKeyNarrativeFixtureDiagnostician,
)
from experiments.comparison_bundles import (
    ComparisonBundleBuilder,
    ComparisonBundleStateError,
    ComparisonBundleValidationError,
    ComparisonBundleValidator,
    VerifiedComparisonBundle,
    VerifiedFailedComparisonBundle,
)
from experiments.comparison_manifests import ComparisonManifestValidationError
from experiments.diagnostician import DiagnosisRequest, DiagnosisResult
from experiments.model_adapters import (
    HttpRequest,
    HttpResponse,
    running_shipped_adapter,
)
from experiments.narrative_diagnostician import (
    ModelNarrativeDiagnostician,
    NarrativeResult,
)
from experiments.trial_manifests import canonical_json_bytes
from itself import JsonObject

ROOT = Path(__file__).resolve().parents[1]
SECRET = "v2-comparison-secret-that-must-not-leak"
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def _target() -> ComparisonStudyTarget:
    registered = STUDY_TARGETS_BY_VERSION["5"][0]
    assert registered.stream is True
    return replace(
        registered,
        contract=V2_CONTRACT,
        shipped_adapter=running_shipped_adapter(),
        stream_include_usage=True,
        deadline_seconds=300.0,
    )


def _requests() -> list[HttpRequest]:
    return []


@dataclass(slots=True)
class SequenceTransport:
    responses: list[HttpResponse]
    requests: list[HttpRequest] = field(default_factory=_requests)

    def send(self, request: HttpRequest, /) -> HttpResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def _request() -> DiagnosisRequest:
    return report_module._request()


def _streamed(content: JsonObject, *, input_tokens: int) -> HttpResponse:
    chunk: JsonObject = {"id": "v2-comparison", "model": _target().model_id}
    events: list[JsonObject] = [
        {
            **chunk,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": None,
                    "delta": {"role": "assistant", "content": json.dumps(content)},
                }
            ],
            "usage": None,
        },
        {
            **chunk,
            "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}],
            "usage": None,
        },
        {
            **chunk,
            "choices": [],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": 426,
                "total_tokens": input_tokens + 426,
            },
        },
    ]
    body = b"".join(
        b"data: " + json.dumps(event).encode() + b"\n\n" for event in events
    )
    return HttpResponse(
        200,
        {"Content-Type": "text/event-stream"},
        body + b"data: [DONE]\n\n",
    )


def _narrative() -> HttpResponse:
    assertion = CacheKeyNarrativeFixtureDiagnostician().diagnose(_request()).assertion
    return _streamed(assertion.to_json_object(), input_tokens=157)


def _structured() -> HttpResponse:
    assertion = CacheKeyFixtureDiagnostician().diagnose(_request()).assertion
    return _streamed(assertion.to_model_json_object(), input_tokens=200)


def _manifest_path(tmp_path: Path, target: ComparisonStudyTarget) -> Path:
    path = tmp_path / f"{target.target_id}.json"
    if not path.exists():
        path.write_bytes(
            pretty_json_bytes(build_study_manifest(target, registered_at=NOW))
        )
    return path


def _run(
    tmp_path: Path,
    transport: SequenceTransport,
    bundle: Path,
    *,
    replicate_index: int = 1,
    target: ComparisonStudyTarget | None = None,
) -> JsonObject:
    active = _target() if target is None else target
    return run_cache_key_comparison(
        _manifest_path(tmp_path, active),
        bundle,
        replicate_index=replicate_index,
        base_url=active.base_url,
        api_key_env=active.api_key_env,
        transport=transport,
        environment={active.api_key_env: SECRET},
        now=lambda: NOW,
        monotonic_ns=itertools.count(1_000_000, 1_000_000).__next__,
    )


def _failed(bundle: Path) -> VerifiedFailedComparisonBundle:
    verified = ComparisonBundleValidator().validate(bundle, _request())
    assert isinstance(verified, VerifiedFailedComparisonBundle)
    return verified


def _failure(verified: VerifiedFailedComparisonBundle) -> JsonObject:
    failure = verified.attempt_report["failure"]
    assert isinstance(failure, dict)
    return failure


def _fail_projection(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    def failing(
        request: DiagnosisRequest,
        narrative: NarrativeResult,
        structured: DiagnosisResult,
    ) -> ComparativeRunResult:
        del request, narrative, structured
        raise error

    monkeypatch.setattr(comparison_run, "project_comparative_results", failing)


def _fail_narrative_after_its_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class Failing(ModelNarrativeDiagnostician):
        def diagnose(self, request: DiagnosisRequest) -> NarrativeResult:
            super().diagnose(request)
            raise RuntimeError(f"narrative scorer failed with {SECRET}")

    monkeypatch.setattr(comparison_run, "ModelNarrativeDiagnostician", Failing)


# A completed v2 replicate: its deadline, shipped client, and stream usage.


def test_a_v2_replicate_streams_asks_for_usage_and_records_its_client(
    tmp_path: Path,
) -> None:
    transport = SequenceTransport([_narrative(), _structured()])
    bundle_manifest = _run(tmp_path, transport, tmp_path / "replicate-001")
    verified = ComparisonBundleValidator().validate(
        tmp_path / "replicate-001",
        _request(),
    )

    assert isinstance(verified, VerifiedComparisonBundle)
    assert bundle_manifest["bundle_version"] == "0.3.0"
    assert bundle_manifest["shipped_adapter"] == (
        running_shipped_adapter().to_json_object()
    )
    narrative = verified.branches[ComparisonCondition.NARRATIVE].scorecard
    assert narrative["model_input_tokens"] == 157
    assert narrative["model_output_tokens"] == 426
    assert len(transport.requests) == 2
    for request in transport.requests:
        payload = cast(JsonObject, json.loads(request.body))
        assert payload["stream_options"] == {"include_usage": True}
        assert request.deadline_seconds == 300.0


def test_the_comparison_runner_refuses_a_different_running_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest_path(tmp_path, _target())
    monkeypatch.setattr(shipped_inference, "ADAPTER_VERSION", "0.5.0")
    transport = SequenceTransport([_narrative(), _structured()])

    with pytest.raises(ComparisonManifestValidationError, match="running shipped"):
        _run(tmp_path, transport, tmp_path / "replicate-001")
    assert transport.requests == []
    assert not (tmp_path / "replicate-001").exists()


def test_comparison_validation_checks_the_recorded_client_against_the_pin(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "replicate-001"
    _run(tmp_path, SequenceTransport([_narrative(), _structured()]), bundle)
    bundle.chmod(0o700)
    manifest_path = bundle / "bundle.json"
    manifest = cast(JsonObject, json.loads(manifest_path.read_text("utf-8")))
    cast(JsonObject, manifest["shipped_adapter"])["adapter_version"] = "9.9.9"
    del manifest["bundle_id"]
    manifest = {
        "bundle_id": "urn:sha256:"
        + hashlib.sha256(canonical_json_bytes(manifest)).hexdigest(),
        **manifest,
    }
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(pretty_json_bytes(manifest))

    with pytest.raises(ComparisonBundleValidationError, match="bundle shipped"):
        ComparisonBundleValidator().validate(bundle, _request())


def test_a_comparison_runner_failure_needs_a_sent_request(tmp_path: Path) -> None:
    manifest = cast(
        JsonObject,
        json.loads(_manifest_path(tmp_path, _target()).read_text("utf-8")),
    )
    with ComparisonBundleBuilder(
        tmp_path / "replicate-001",
        manifest,
        _request(),
        replicate_index=1,
        now=lambda: NOW,
    ) as builder:
        builder.observed_transport(
            SequenceTransport([]),
            attempt_id=NARRATIVE_ATTEMPT_ID,
        )
        assert builder.records_runner_failure is False
        with pytest.raises(ComparisonBundleStateError, match="sent a request"):
            builder.fail_runner(
                RunnerFailure.from_exception(RuntimeError("x")),
                attempt_id=NARRATIVE_ATTEMPT_ID,
            )


def test_a_comparison_failure_before_any_request_is_not_a_runner_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Unprepared(ModelNarrativeDiagnostician):
        def diagnose(self, request: DiagnosisRequest) -> NarrativeResult:
            del request
            raise RuntimeError("the narrative attempt could not be prepared")

    monkeypatch.setattr(comparison_run, "ModelNarrativeDiagnostician", Unprepared)
    transport = SequenceTransport([_narrative(), _structured()])
    bundles = tmp_path / "bundles"
    bundles.mkdir()

    with pytest.raises(RuntimeError, match="could not be prepared"):
        _run(tmp_path, transport, bundles / "replicate-001")
    assert transport.requests == []
    assert list(bundles.iterdir()) == []


# The observed HTTP status.


def test_a_v2_structured_failure_keeps_an_unclamped_status(tmp_path: Path) -> None:
    transport = SequenceTransport(
        [
            _narrative(),
            HttpResponse(999, {"Content-Type": "application/json"}, b"{}"),
        ]
    )
    bundle_manifest = _run(tmp_path, transport, tmp_path / "replicate-001")
    verified = _failed(tmp_path / "replicate-001")

    assert bundle_manifest["outcome"] == "adapter_failed"
    assert verified.attempt_report["attempt_id"] == STRUCTURED_ATTEMPT_ID
    assert _failure(verified)["status_code"] == 999


# A runner failure is attributed to the attempt it interrupted.


def test_a_runner_failure_during_the_narrative_attempt_stops_the_replicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_narrative_after_its_response(monkeypatch)
    transport = SequenceTransport([_narrative(), _structured()])
    bundle_manifest = _run(tmp_path, transport, tmp_path / "replicate-001")
    verified = _failed(tmp_path / "replicate-001")
    failure = _failure(verified)

    assert len(transport.requests) == 1
    assert bundle_manifest["outcome"] == "runner_failed"
    assert bundle_manifest["attempt_outcomes"] == {
        NARRATIVE_ATTEMPT_ID: "runner_failed",
        STRUCTURED_ATTEMPT_ID: "not_run",
    }
    assert verified.attempt_report["attempt_id"] == NARRATIVE_ATTEMPT_ID
    assert verified.narrative_result is None
    assert failure["status_code"] == 200
    assert failure["error"] == {
        "type": "RuntimeError",
        "message": "narrative scorer failed with [redacted]",
    }
    artifact = cast(JsonObject, failure["response_artifact"])
    assert cast(str, artifact["uri"]).startswith(f"attempts/{NARRATIVE_ATTEMPT_ID}/")


@pytest.mark.parametrize(
    "outcomes",
    [
        {NARRATIVE_ATTEMPT_ID: "runner_failed", STRUCTURED_ATTEMPT_ID: "runner_failed"},
        {NARRATIVE_ATTEMPT_ID: "runner_failed", STRUCTURED_ATTEMPT_ID: "completed"},
        {NARRATIVE_ATTEMPT_ID: "adapter_failed", STRUCTURED_ATTEMPT_ID: "not_run"},
    ],
)
def test_a_runner_failed_bundle_names_exactly_one_stopped_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcomes: JsonObject,
) -> None:
    _fail_narrative_after_its_response(monkeypatch)
    bundle_manifest = _run(
        tmp_path,
        SequenceTransport([_narrative()]),
        tmp_path / "replicate-001",
    )
    validator = ComparisonBundleValidator()
    validator.validate_manifest(bundle_manifest)

    with pytest.raises(ComparisonBundleValidationError):
        validator.validate_manifest({**bundle_manifest, "attempt_outcomes": outcomes})


def test_a_runner_failure_after_both_responses_keeps_both(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_projection(monkeypatch, ValueError("projection failed"))
    bundle_manifest = _run(
        tmp_path,
        SequenceTransport([_narrative(), _structured()]),
        tmp_path / "replicate-001",
    )
    verified = _failed(tmp_path / "replicate-001")

    assert bundle_manifest["outcome"] == "runner_failed"
    assert bundle_manifest["attempt_outcomes"] == {
        NARRATIVE_ATTEMPT_ID: "completed",
        STRUCTURED_ATTEMPT_ID: "runner_failed",
    }
    assert verified.narrative_result is not None
    artifact = cast(JsonObject, _failure(verified)["response_artifact"])
    assert cast(str, artifact["uri"]).startswith(f"attempts/{STRUCTURED_ATTEMPT_ID}/")


def test_a_runner_failure_in_completion_removes_what_it_wrote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_disk(*_: object) -> bytes:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(comparison_bundles, "canonical_ledger_bytes", no_disk)
    bundle_manifest = _run(
        tmp_path,
        SequenceTransport([_narrative(), _structured()]),
        tmp_path / "replicate-001",
    )
    verified = _failed(tmp_path / "replicate-001")

    assert bundle_manifest["outcome"] == "runner_failed"
    assert _failure(verified)["error"] == {
        "type": "OSError",
        "message": "[Errno 28] No space left on device",
    }
    assert not (tmp_path / "replicate-001" / "branches").exists()


def test_an_interrupted_replicate_is_published_and_the_interrupt_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_projection(monkeypatch, KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        _run(
            tmp_path,
            SequenceTransport([_narrative(), _structured()]),
            tmp_path / "replicate-001",
        )
    failure = _failure(_failed(tmp_path / "replicate-001"))

    assert failure["kind"] == "interrupted"


# The report counts runner failures apart from adapter failures.


def _v2_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> JsonObject:
    target = _target()
    monkeypatch.setattr(report_module, "STUDY_TARGETS_BY_VERSION", {"5": (target,)})
    monkeypatch.setattr(report_module, "REPLICATE_COUNT", 3)
    root = tmp_path / "bundles" / target.target_id
    _run(
        tmp_path,
        SequenceTransport([_narrative(), _structured()]),
        root / "replicate-001",
    )
    _run(
        tmp_path,
        SequenceTransport([HttpResponse(429, {}, b'{"error":"slow down"}')]),
        root / "replicate-002",
        replicate_index=2,
    )
    with monkeypatch.context() as patch:
        _fail_projection(patch, RuntimeError(f"projection failed with {SECRET}"))
        _run(
            tmp_path,
            SequenceTransport([_narrative(), _structured()]),
            root / "replicate-003",
            replicate_index=3,
        )
    return build_comparison_report(
        tmp_path / "bundles",
        study_version="5",
        reported_at=NOW,
    )


def test_the_report_counts_runner_failures_apart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _v2_report(tmp_path, monkeypatch)
    totals = cast(JsonObject, report["totals"])
    (summary,) = cast(list[JsonObject], report["target_summaries"])

    assert report["report_version"] == "0.4.0"
    for counts in (totals, summary):
        assert counts["completed_replicates"] == 1
        assert counts["failed_replicates"] == 1
        assert counts["runner_failed_replicates"] == 1
        assert counts["failures"] == [
            {"attempt_id": NARRATIVE_ATTEMPT_ID, "kind": "http_rate_limit", "count": 1}
        ]
        assert counts["runner_failures"] == [
            {"attempt_id": STRUCTURED_ATTEMPT_ID, "kind": "runner_error", "count": 1}
        ]
    replicate = cast(list[JsonObject], report["replicates"])[2]
    assert replicate["outcome"] == "runner_failed"
    assert cast(JsonObject, replicate["failure"])["error_type"] == "RuntimeError"
    design = cast(JsonObject, report["design"])
    assert design["deadline_seconds"] == 300.0
    assert design["stream_include_usage"] is True
    assert design["shipped_adapter"] == running_shipped_adapter().to_json_object()
    assert SECRET not in json.dumps(report)
    assert "projection failed" not in json.dumps(report)


def test_the_markdown_labels_runner_failures_apart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markdown = render_markdown(_v2_report(tmp_path, monkeypatch))

    assert (
        "Of the other 2, 1 stopped on an explicit provider or model-output "
        "failure and 1 on a failure of the harness itself after a request was "
        "sent."
    ) in markdown
    assert (
        "harness: 1 test-plan request: harness failed after the request was "
        "sent (`runner_error`)"
    ) in markdown
    assert "1 runs stopped on provider or model-output failures and 1 on" in (markdown)


def _with_no_runner_failures(report: JsonObject) -> JsonObject:
    totals = cast(JsonObject, report["totals"])
    totals.update({"runner_failed_replicates": 0, "runner_failures": []})
    for summary in cast(list[JsonObject], report["target_summaries"]):
        summary.update({"runner_failed_replicates": 0, "runner_failures": []})
    return report


@pytest.mark.parametrize(
    "name",
    [
        "archive/2026-07-24-case-001-comparison-v2",
        "current/2026-07-24-case-001-comparison-v5",
    ],
)
def test_a_report_with_no_runner_failures_renders_as_before(name: str) -> None:
    path = ROOT / "experiments" / "results" / f"{name}.json"
    report = cast(JsonObject, json.loads(path.read_text("utf-8")))

    assert render_markdown(_with_no_runner_failures(report)) == (
        path.with_suffix(".md").read_text("utf-8")
    )


def test_a_v1_report_records_no_runner_fields() -> None:
    target = STUDY_TARGETS_BY_VERSION["5"][0]
    observations: list[JsonObject] = []
    summary = report_module._target_summary(target, observations)
    totals = report_module._totals(observations, [summary])

    assert "runner_failed_replicates" not in summary
    assert "runner_failures" not in totals
    assert "deadline_seconds" not in report_module._design((target,))
