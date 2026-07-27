# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from experiments.cases.cache_key_comparison import (
    NARRATIVE_ATTEMPT_ID,
    STRUCTURED_ATTEMPT_ID,
    ComparisonCondition,
    build_comparison_request,
    run_comparative_case,
)
from experiments.cases.cache_key_diagnostician import (
    CacheKeyFixtureDiagnostician,
    CacheKeyNarrativeFixtureDiagnostician,
)
from experiments.cases.cache_key_omission import CaseEnvironment, Mechanism
from experiments.comparison_bundles import (
    ComparisonBundleBuilder,
    ComparisonBundleValidationError,
    ComparisonBundleValidator,
)
from experiments.comparison_manifests import build_comparison_manifest
from experiments.diagnostician import DiagnosisRequest
from experiments.model_adapters import (
    HttpRequest,
    HttpResponse,
    ModelAdapterError,
    OpenAIChatDiagnostician,
    OpenAIChatEndpoint,
    OpenAIChatStructuredInvoker,
)
from experiments.narrative_diagnostician import ModelNarrativeDiagnostician
from itself import JsonObject, JsonValue

SECRET = "comparison-secret-that-must-not-leak"
NOW = datetime(2026, 7, 24, 18, 0, tzinfo=UTC)
FINISHED = datetime(2026, 7, 24, 18, 1, tzinfo=UTC)


def _request() -> DiagnosisRequest:
    context = CaseEnvironment(
        Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION
    ).diagnostic_context()
    return build_comparison_request(context)


def _endpoint() -> OpenAIChatEndpoint:
    return OpenAIChatEndpoint(
        actor_id="together-glm-5p2-case-001-comparison",
        base_url="https://api.together.xyz/v1",
        model="zai-org/GLM-5.2",
        api_key_env="TOGETHER_API_KEY",
        timeout_seconds=180.0,
        max_output_tokens=4_096,
        extra_body={"temperature": 0.0},
    )


def _manifest(
    *,
    expected_observation_values: tuple[str, ...] = (),
) -> JsonObject:
    return build_comparison_manifest(
        _endpoint(),
        _request(),
        comparison_series_id="case-001-together-glm-5p2-comparison-v1",
        registered_at=NOW,
        replicate_count=3,
        study_phase="preregistered_study",
        limitations=("One transparent case does not establish behavioral transfer.",),
        expected_observation_values=expected_observation_values,
    )


def _completion_body(
    assertion: JsonObject,
    *,
    input_tokens: int,
    output_tokens: int,
) -> bytes:
    envelope: JsonObject = {
        "id": "comparison-completion",
        "model": "zai-org/GLM-5.2",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(assertion),
                },
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }
    return json.dumps(envelope).encode()


def _narrative_body() -> bytes:
    result = CacheKeyNarrativeFixtureDiagnostician().diagnose(_request())
    return _completion_body(
        result.assertion.to_json_object(),
        input_tokens=90,
        output_tokens=30,
    )


def _structured_body() -> bytes:
    result = CacheKeyFixtureDiagnostician().diagnose(_request())
    return _completion_body(
        result.assertion.to_model_json_object(),
        input_tokens=150,
        output_tokens=100,
    )


@dataclass(frozen=True, slots=True)
class StaticTransport:
    response: HttpResponse

    def send(self, request: HttpRequest, /) -> HttpResponse:
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        return self.response


def _narrative_diagnostician(
    builder: ComparisonBundleBuilder,
    response: HttpResponse,
) -> ModelNarrativeDiagnostician:
    return ModelNarrativeDiagnostician(
        OpenAIChatStructuredInvoker(
            endpoint=_endpoint(),
            artifact_sink=builder.narrative_artifact_sink,
            transport=StaticTransport(response),
            environment={"TOGETHER_API_KEY": SECRET},
            now=lambda: NOW,
            monotonic_ns=iter([1_000_000, 4_000_000]).__next__,
        )
    )


def _structured_diagnostician(
    builder: ComparisonBundleBuilder,
    response: HttpResponse,
) -> OpenAIChatDiagnostician:
    return OpenAIChatDiagnostician(
        endpoint=_endpoint(),
        artifact_sink=builder.structured_artifact_sink,
        transport=StaticTransport(response),
        environment={"TOGETHER_API_KEY": SECRET},
        now=lambda: NOW,
        monotonic_ns=iter([5_000_000, 12_000_000]).__next__,
    )


def _build_completed_bundle(path: Path) -> JsonObject:
    request = _request()
    with ComparisonBundleBuilder(
        path,
        _manifest(),
        request,
        replicate_index=2,
        now=lambda: NOW,
    ) as builder:
        result = run_comparative_case(
            _narrative_diagnostician(
                builder,
                HttpResponse(
                    200,
                    {"Content-Type": "application/json"},
                    _narrative_body(),
                ),
            ),
            _structured_diagnostician(
                builder,
                HttpResponse(
                    200,
                    {"Content-Type": "application/json"},
                    _structured_body(),
                ),
            ),
        )
        return builder.complete(result)


def test_completed_bundle_is_atomic_private_and_recomputable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "replicate-002"

    bundle_manifest = _build_completed_bundle(path)
    verified = ComparisonBundleValidator().validate_completed(path, _request())

    assert bundle_manifest["outcome"] == "completed"
    assert bundle_manifest["replicate_index"] == 2
    assert verified.bundle_manifest == bundle_manifest
    assert set(verified.branches) == set(ComparisonCondition)
    assert (
        verified.branches[ComparisonCondition.NARRATIVE].scorecard["model_input_tokens"]
        == 90
    )
    assert (
        verified.branches[ComparisonCondition.STRUCTURED_WITHOUT_TESTING].scorecard[
            "model_output_tokens"
        ]
        == 100
    )
    assert (
        verified.branches[ComparisonCondition.EVIDENCE_ENFORCED].scorecard[
            "receipt_recomputes"
        ]
        is True
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE((path / "bundle.json").stat().st_mode) == 0o600


def test_completed_bundle_binds_structured_branches_to_identical_source(
    tmp_path: Path,
) -> None:
    path = tmp_path / "replicate-002"
    bundle_manifest = _build_completed_bundle(path)
    bindings = cast(JsonObject, bundle_manifest["branch_bindings"])
    structured = cast(
        JsonObject,
        bindings[ComparisonCondition.STRUCTURED_WITHOUT_TESTING.value],
    )
    evidence = cast(
        JsonObject,
        bindings[ComparisonCondition.EVIDENCE_ENFORCED.value],
    )

    assert structured == evidence
    assert structured["source_attempt_id"] == STRUCTURED_ATTEMPT_ID
    assert structured["result_path"] == "attempts/structured-attempt-1/result.json"


def test_bundle_contains_no_credential_endpoint_or_absolute_artifact_path(
    tmp_path: Path,
) -> None:
    path = tmp_path / "replicate-002"
    _build_completed_bundle(path)

    all_content = b"".join(
        item.read_bytes() for item in path.rglob("*") if item.is_file()
    )
    bundle = cast(
        JsonObject,
        json.loads((path / "bundle.json").read_text(encoding="utf-8")),
    )
    files = cast(list[JsonValue], bundle["files"])
    artifact_paths = [
        cast(str, cast(JsonObject, entry)["path"])
        for entry in files
        if cast(JsonObject, entry)["visibility"] == "private"
    ]

    assert SECRET.encode() not in all_content
    assert b"TOGETHER_API_KEY" not in all_content
    assert b"api.together.xyz" not in all_content
    assert artifact_paths
    assert all(not path.startswith("/") for path in artifact_paths)


def test_narrative_failure_stops_before_structured_attempt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "replicate-001"
    private_body = b'{"error":"private rate-limit detail"}'
    builder_clock = iter([NOW, FINISHED]).__next__

    with ComparisonBundleBuilder(
        path,
        _manifest(),
        _request(),
        replicate_index=1,
        now=builder_clock,
    ) as builder:
        diagnostician = _narrative_diagnostician(
            builder,
            HttpResponse(
                429,
                {"Content-Type": "application/json"},
                private_body,
            ),
        )
        with pytest.raises(ModelAdapterError) as captured:
            diagnostician.diagnose(_request())
        bundle_manifest = builder.fail(
            captured.value,
            attempt_id=NARRATIVE_ATTEMPT_ID,
        )

    verified = ComparisonBundleValidator().validate_failure(path, _request())
    failure = cast(JsonObject, verified.attempt_report["failure"])
    response_artifact = cast(JsonObject, failure["response_artifact"])

    assert bundle_manifest["attempt_outcomes"] == {
        NARRATIVE_ATTEMPT_ID: "adapter_failed",
        STRUCTURED_ATTEMPT_ID: "not_run",
    }
    assert failure["kind"] == "http_rate_limit"
    assert verified.bundle_manifest["created_at"] == "2026-07-24T18:00:00Z"
    assert verified.attempt_report["completed_at"] == "2026-07-24T18:01:00Z"
    assert "private rate-limit detail" not in (path / "attempt-report.json").read_text(
        encoding="utf-8"
    )
    assert (path / cast(str, response_artifact["uri"])).read_bytes() == private_body


def test_structured_failure_retains_completed_narrative_attempt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "replicate-003"
    private_body = b'{"error":"private server detail"}'

    with ComparisonBundleBuilder(
        path,
        _manifest(),
        _request(),
        replicate_index=3,
        now=lambda: NOW,
    ) as builder:
        narrative_result = _narrative_diagnostician(
            builder,
            HttpResponse(
                200,
                {"Content-Type": "application/json"},
                _narrative_body(),
            ),
        ).diagnose(_request())
        diagnostician = _structured_diagnostician(
            builder,
            HttpResponse(
                503,
                {"Content-Type": "application/json"},
                private_body,
            ),
        )
        with pytest.raises(ModelAdapterError) as captured:
            diagnostician.diagnose(_request())
        bundle_manifest = builder.fail(
            captured.value,
            attempt_id=STRUCTURED_ATTEMPT_ID,
            narrative_result=narrative_result,
        )

    verified = ComparisonBundleValidator().validate_failure(path, _request())

    assert bundle_manifest["attempt_outcomes"] == {
        NARRATIVE_ATTEMPT_ID: "completed",
        STRUCTURED_ATTEMPT_ID: "adapter_failed",
    }
    assert verified.narrative_result == narrative_result
    assert not (path / "branches").exists()


def test_structured_failure_binds_registered_output_profile(
    tmp_path: Path,
) -> None:
    path = tmp_path / "replicate-v3"
    manifest = _manifest(expected_observation_values=("A", "B"))

    with ComparisonBundleBuilder(
        path,
        manifest,
        _request(),
        replicate_index=1,
        now=lambda: NOW,
    ) as builder:
        narrative_result = _narrative_diagnostician(
            builder,
            HttpResponse(
                200,
                {"Content-Type": "application/json"},
                _narrative_body(),
            ),
        ).diagnose(_request())
        diagnostician = _structured_diagnostician(
            builder,
            HttpResponse(
                503,
                {"Content-Type": "application/json"},
                b'{"error":"server failure"}',
            ),
        )
        with pytest.raises(ModelAdapterError) as captured:
            diagnostician.diagnose(_request())
        builder.fail(
            captured.value,
            attempt_id=STRUCTURED_ATTEMPT_ID,
            narrative_result=narrative_result,
        )

    verified = ComparisonBundleValidator().validate_failure(path, _request())

    assert verified.attempt_report["profile_id"] == "diagnosis-assertion-v0.3.0"
    mismatched: JsonObject = dict(verified.attempt_report)
    mismatched["profile_id"] = "diagnosis-assertion-v0.2.0"
    with pytest.raises(
        ComparisonBundleValidationError,
        match="profile does not match",
    ):
        ComparisonBundleValidator().validate_attempt_report_against_manifest(
            mismatched,
            manifest,
        )


def test_bundle_validation_detects_tampered_scorecard(tmp_path: Path) -> None:
    path = tmp_path / "replicate-002"
    _build_completed_bundle(path)
    scorecard_path = path / "branches" / "evidence_enforced" / "scorecard.json"
    scorecard = cast(
        JsonObject,
        json.loads(scorecard_path.read_text(encoding="utf-8")),
    )
    scorecard["behavioral_recovery"] = False
    scorecard_path.write_text(json.dumps(scorecard), encoding="utf-8")

    with pytest.raises(ComparisonBundleValidationError, match="digest mismatch"):
        ComparisonBundleValidator().validate_completed(path, _request())


@pytest.mark.parametrize("replicate_index", [0, 4])
def test_builder_rejects_replicate_outside_registered_series(
    tmp_path: Path,
    replicate_index: int,
) -> None:
    with pytest.raises(
        ComparisonBundleValidationError,
        match="outside the registered comparison series",
    ):
        ComparisonBundleBuilder(
            tmp_path / "replicate",
            _manifest(),
            _request(),
            replicate_index=replicate_index,
            now=lambda: NOW,
        )


def test_builder_does_not_publish_partial_bundle_on_exception(
    tmp_path: Path,
) -> None:
    path = tmp_path / "replicate-001"

    with (
        pytest.raises(RuntimeError, match="simulated interruption"),
        ComparisonBundleBuilder(
            path,
            _manifest(),
            _request(),
            replicate_index=1,
            now=lambda: NOW,
        ),
    ):
        raise RuntimeError("simulated interruption")

    assert not path.exists()
    assert not tuple(tmp_path.glob(".replicate-001.*"))


def test_builder_never_overwrites_existing_destination(tmp_path: Path) -> None:
    path = tmp_path / "replicate-001"
    path.mkdir()
    marker = path / "owner-data.txt"
    marker.write_text("preserve", encoding="utf-8")

    with (
        pytest.raises(FileExistsError),
        ComparisonBundleBuilder(
            path,
            _manifest(),
            _request(),
            replicate_index=1,
            now=lambda: NOW,
        ),
    ):
        pass

    assert marker.read_text(encoding="utf-8") == "preserve"
