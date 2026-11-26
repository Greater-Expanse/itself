# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

# pyright: reportPrivateUsage=false

from __future__ import annotations

import itertools
import json
import stat
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import experiments.cases.cache_key_comparison_report as report_module
from experiments.cases.cache_key_comparison import ComparisonCondition
from experiments.cases.cache_key_comparison_report import (
    _metric_validity,
    _provider_control,
    _registered_manifest_time,
    _target_summary,
    _totals,
    build_comparison_report,
    render_markdown,
    write_comparison_report,
)
from experiments.cases.cache_key_comparison_run import run_cache_key_comparison
from experiments.cases.cache_key_comparison_study import (
    CURRENT_STUDY_VERSIONS,
    STUDY_TARGETS_BY_VERSION,
    STUDY_VERSION,
    ComparisonStudyTarget,
    build_study_manifest,
)
from experiments.cases.cache_key_diagnostician import (
    CacheKeyFixtureDiagnostician,
    CacheKeyNarrativeFixtureDiagnostician,
)
from experiments.model_adapters import HttpRequest, HttpResponse
from itself import JsonObject, JsonValue

ROOT = Path(__file__).resolve().parents[1]


def _target() -> ComparisonStudyTarget:
    return ComparisonStudyTarget(
        study_version="2",
        target_id="example-glm",
        provider="Example Provider",
        model_family="GLM 5.2",
        model_id="example/glm-5p2",
        base_url="https://inference.example/v1",
        api_key_env="EXAMPLE_API_KEY",
        checkpoint_status="public_weights",
        checkpoint_url="https://weights.example/glm-5p2",
        license_name="MIT",
        stream=True,
    )


def _scorecard(condition: ComparisonCondition) -> JsonObject:
    structured = condition is not ComparisonCondition.NARRATIVE
    evidence = condition is ComparisonCondition.EVIDENCE_ENFORCED
    return {
        "condition": condition.value,
        "model_primary_is_ground_truth": True,
        "model_retained_ground_truth": True,
        "selected_test_discriminates": True if structured else None,
        "predictions_declared": 3 if structured else None,
        "predictions_correct": 0 if structured else None,
        "external_evidence_identifies_ground_truth": True if evidence else None,
        "behavioral_recovery": True if evidence else None,
        "false_promotions_accepted": 0,
        "receipt_recomputes": True,
        "ground_truth_final_status": "mixed_evidence" if evidence else "testable",
    }


def _completed_observation() -> JsonObject:
    scorecards: JsonObject = {
        condition.value: _scorecard(condition) for condition in ComparisonCondition
    }
    return {
        "target_id": "example-glm",
        "replicate_index": 1,
        "bundle_id": "urn:sha256:" + ("a" * 64),
        "outcome": "completed",
        "scorecards": scorecards,
    }


def _failed_observation() -> JsonObject:
    return {
        "target_id": "example-glm",
        "replicate_index": 2,
        "bundle_id": "urn:sha256:" + ("b" * 64),
        "outcome": "adapter_failed",
        "failed_attempt_id": "structured-attempt-1",
        "failure": {
            "kind": "assertion_contract",
            "retryable": False,
            "status_code": None,
        },
    }


def _report() -> JsonObject:
    observations = [_completed_observation(), _failed_observation()]
    summary = _target_summary(_target(), observations)
    observation_values: list[JsonValue] = [observation for observation in observations]
    summary_values: list[JsonValue] = [summary]
    anomaly_values: list[JsonValue] = [
        {
            "kind": "failure_completion_precedes_artifact_capture",
            "target_id": "example-glm",
            "replicate_index": 1,
        }
    ]
    return {
        "study_version": "2",
        "totals": _totals(observations, [summary]),
        "target_summaries": summary_values,
        "replicates": observation_values,
        "metadata_anomalies": anomaly_values,
        "metric_validity": {
            "invalid": {
                "predictions_correct": "free-text-versus-code mismatch",
                "ground_truth_final_status": "inherited mismatch",
            }
        },
    }


def _v4_report() -> JsonObject:
    target = ComparisonStudyTarget(
        study_version="4",
        target_id="example-glm",
        provider="Example Provider",
        model_family="GLM 5.2",
        model_id="example/glm-5p2",
        base_url="https://inference.example/v1",
        api_key_env="EXAMPLE_API_KEY",
        checkpoint_status="public_weights",
        checkpoint_url="https://weights.example/glm-5p2",
        license_name="MIT",
        stream=True,
        expected_observation_values=("A", "B"),
    )
    observation = _completed_observation()
    scorecards = observation["scorecards"]
    assert isinstance(scorecards, dict)
    for condition in (
        ComparisonCondition.STRUCTURED_WITHOUT_TESTING,
        ComparisonCondition.EVIDENCE_ENFORCED,
    ):
        scorecard = scorecards[condition.value]
        assert isinstance(scorecard, dict)
        scorecard["predictions_correct"] = 3
    evidence = scorecards[ComparisonCondition.EVIDENCE_ENFORCED.value]
    assert isinstance(evidence, dict)
    evidence["ground_truth_final_status"] = "supported"

    observations = [observation]
    summary = _target_summary(
        target,
        observations,
        prediction_metrics_valid=True,
    )
    observation_values: list[JsonValue] = [observation]
    summary_values: list[JsonValue] = [summary]
    return {
        "study_version": "4",
        "totals": _totals(
            observations,
            [summary],
            prediction_metrics_valid=True,
        ),
        "target_summaries": summary_values,
        "replicates": observation_values,
        "metadata_anomalies": [],
        "metric_validity": _metric_validity(
            study_version="4",
            prediction_metrics_valid=True,
        ),
    }


def _v5_report() -> JsonObject:
    report = deepcopy(_v4_report())
    report["study_version"] = "5"
    report["design"] = {
        "provider_control": _provider_control((_target(),)),
    }
    return report


def test_summary_excludes_invalid_prediction_and_verdict_aggregates() -> None:
    report = _report()
    totals = report["totals"]
    assert isinstance(totals, dict)

    assert totals["completed_replicates"] == 1
    assert totals["failed_replicates"] == 1
    assert totals["structured_selected_discriminating_test"] == 1
    assert "structured_predictions_correct" not in totals
    assert "ground_truth_final_statuses" not in totals


def test_markdown_marks_harness_defect_without_reinterpreting_predictions() -> None:
    markdown = render_markdown(_report())

    assert "## What the experiment actually did" in markdown
    assert "Why does this report keep coming back stale?" in markdown
    assert "The first model call mirrored ordinary AI-assistant use" in markdown
    assert "two model calls and three resulting paths" in markdown
    assert markdown.index("This report calls those two model calls") < markdown.index(
        "## Outcome"
    )
    assert (
        "We scheduled that two-call, three-path procedure 2 times across 1 "
        "model/provider configuration"
    ) in markdown
    assert "stopped on an explicit provider or model-output failure" in markdown
    assert "## What this tells an SDK user" in markdown
    assert "remained an explicit integration result" in markdown
    assert "registered replicate slots" not in markdown
    assert "paired replicates" not in markdown
    assert "typed failures" not in markdown
    assert "Status: historical study version" in markdown
    assert "## Why some measurements cannot be used" in markdown
    assert "cannot support a prediction-correctness" in markdown
    assert "No post-hoc text parser" in markdown
    assert "## Models and weight availability" in markdown
    assert "## Data-quality checks" in markdown
    assert "0/3 evaluator outcomes" not in markdown


def test_v4_report_aggregates_registered_categorical_predictions() -> None:
    report = _v4_report()
    totals = report["totals"]
    assert isinstance(totals, dict)

    assert totals["structured_predictions_declared"] == 3
    assert totals["structured_predictions_correct"] == 3
    assert totals["ground_truth_final_statuses"] == {"supported": 1}
    markdown = render_markdown(report)
    assert "# Case 001 comparison study v4" in markdown
    assert "3/3 matched the case's known outcome table" in markdown
    assert "exact revision codes `A` and `B`" in markdown
    assert "no v3 output was reused" in markdown
    assert "## How the measurement was corrected" in markdown


def test_v5_report_keeps_provider_as_controlled_provenance() -> None:
    report = _v5_report()
    design = report["design"]
    assert isinstance(design, dict)
    control = design["provider_control"]
    assert isinstance(control, dict)

    assert control["provider_count"] == 1
    assert control["role"] == "controlled_infrastructure"
    assert control["provider_is_behavioral_metric"] is False
    assert control["provider_aggregates_reported"] is False
    markdown = render_markdown(report)
    assert "All model calls used Example Provider" in markdown
    assert "this report does not compare providers" in markdown
    assert "| GLM 5.2 |" in markdown
    assert "| Example Provider / GLM 5.2 |" not in markdown
    assert "does not show that Itself made the model more accurate" in markdown
    assert "the experiment recorded 3 ledger-backed paths" in markdown
    assert "the ordinary answer, the testable investigation plan" in markdown
    assert "Reasoning receipts recomputed from their ledgers for 3/3 paths" in markdown
    assert "no v4 model output or scorecard is reused" in markdown
    assert "## How the experiment was controlled" in markdown


def test_reporter_rejects_drifted_embedded_study_manifest() -> None:
    target = _target()
    registered_at = datetime(2026, 7, 24, 20, 0, tzinfo=UTC)
    manifest = build_study_manifest(target, registered_at=registered_at)

    assert _registered_manifest_time(target, manifest) == "2026-07-24T20:00:00Z"
    drifted = deepcopy(manifest)
    drifted["study_phase"] = "post_hoc"
    with pytest.raises(ValueError, match="exact registered study manifest"):
        _registered_manifest_time(target, drifted)


def test_report_writer_is_private_input_free_and_never_overwrites(
    tmp_path: Path,
) -> None:
    json_path = tmp_path / "report.json"
    markdown_path = tmp_path / "report.md"

    write_comparison_report(
        _report(),
        json_path=json_path,
        markdown_path=markdown_path,
    )

    assert json.loads(json_path.read_text(encoding="utf-8")) == _report()
    assert stat.S_IMODE(json_path.stat().st_mode) == 0o644
    assert stat.S_IMODE(markdown_path.stat().st_mode) == 0o644
    with pytest.raises(FileExistsError):
        write_comparison_report(
            _report(),
            json_path=json_path,
            markdown_path=markdown_path,
        )


def _completion(assertion: JsonObject, *, input_tokens: int) -> HttpResponse:
    envelope: JsonObject = {
        "id": "v1-report-completion",
        "model": "Qwen/Qwen3.7-Max",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(assertion)},
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": 40,
            "total_tokens": input_tokens + 40,
        },
    }
    return HttpResponse(
        200, {"Content-Type": "application/json"}, json.dumps(envelope).encode()
    )


@dataclass(slots=True)
class _Responses:
    responses: list[HttpResponse]

    def send(self, request: HttpRequest, /) -> HttpResponse:
        del request
        return self.responses.pop(0)


def test_a_v1_report_derives_its_design_and_limitations_from_its_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One registered v1 target and one replicate keep the bundle set small;
    # the target, its manifest and its non-streaming responses are v1's own.
    target = STUDY_TARGETS_BY_VERSION["1"][4]
    assert target.target_id == "together-qwen3p7-max" and target.stream is False
    monkeypatch.setattr(report_module, "STUDY_TARGETS_BY_VERSION", {"1": (target,)})
    monkeypatch.setattr(report_module, "REPLICATE_COUNT", 1)
    request = report_module._request()
    narrative = CacheKeyNarrativeFixtureDiagnostician().diagnose(request).assertion
    structured = CacheKeyFixtureDiagnostician().diagnose(request).assertion
    root = tmp_path / "bundles"
    run_cache_key_comparison(
        ROOT
        / "experiments/001-cache-key-omission/manifests/v1/together-qwen3p7-max.json",
        root / target.target_id / "replicate-001",
        replicate_index=1,
        base_url=target.base_url,
        api_key_env=target.api_key_env,
        transport=_Responses(
            [
                _completion(narrative.to_json_object(), input_tokens=90),
                _completion(structured.to_model_json_object(), input_tokens=150),
            ]
        ),
        environment={target.api_key_env: "v1-report-secret"},
        now=lambda: datetime(2026, 7, 24, 20, 0, tzinfo=UTC),
        monotonic_ns=itertools.count(1_000_000, 1_000_000).__next__,
    )

    report = build_comparison_report(
        root,
        study_version="1",
        reported_at=datetime(2026, 7, 25, tzinfo=UTC),
    )

    design = cast(JsonObject, report["design"])
    limitations = cast(list[str], report["limitations"])
    assert design["stream"] is False
    assert (
        "Qwen 3.7 Max is a hosted-only comparator, not an open-weight result."
        in limitations
    )
    assert not any("Qwen 3.7 Plus" in limitation for limitation in limitations)
    assert "Status: historical study version" in render_markdown(report)


@pytest.mark.parametrize("study_version", ["1", "2", "3", "4"])
def test_every_superseded_study_version_carries_the_historical_banner(
    study_version: str,
) -> None:
    report = deepcopy(_v4_report())
    report["study_version"] = study_version

    assert "Status: historical study version" in render_markdown(report)


def test_the_current_study_version_carries_no_historical_banner() -> None:
    assert sorted(CURRENT_STUDY_VERSIONS) == ["5", "6", "7"]
    assert STUDY_VERSION in CURRENT_STUDY_VERSIONS
    assert "Status: historical study version" not in render_markdown(_v5_report())


@pytest.mark.parametrize(
    ("count", "label"),
    [
        (1, "1 test-plan request: endpoint refused the request"),
        (5, "5 test-plan requests: endpoint refused the requests"),
    ],
)
def test_a_refused_request_is_labelled_in_words(count: int, label: str) -> None:
    description = report_module._failure_description(
        {"attempt_id": "structured-attempt-1", "count": count, "kind": "http_status"}
    )

    assert description == f"{label} with an HTTP error status (`http_status`)"


def test_one_stopped_run_is_reported_in_the_singular() -> None:
    path = ROOT / "experiments/results/current/2026-11-26-case-001-comparison-v7.json"
    report = cast(JsonObject, json.loads(path.read_text("utf-8")))

    markdown = render_markdown(report)

    assert cast(JsonObject, report["totals"])["failed_replicates"] == 1
    assert (
        "- 1 run stopped on a provider or model-output failure. That failure "
        "remains visible and is not counted as a wrong diagnosis or replaced "
        "with a new sample."
    ) in markdown
    assert "1 runs" not in markdown


@pytest.mark.parametrize(
    "name",
    [
        "archive/2026-07-24-case-001-comparison-v2",
        "archive/2026-07-24-case-001-comparison-v4",
        "current/2026-07-24-case-001-comparison-v5",
        "current/2026-11-26-case-001-comparison-v6",
        "current/2026-11-26-case-001-comparison-v7",
    ],
)
def test_committed_reports_are_reproduced_from_their_targets(name: str) -> None:
    # The study record is immutable: what the reporter derives from the
    # registered targets must match what each committed report says.
    path = ROOT / "experiments" / "results" / name
    report = cast(JsonObject, json.loads(path.with_suffix(".json").read_text("utf-8")))
    study_version = cast(str, report["study_version"])
    targets = STUDY_TARGETS_BY_VERSION[study_version]
    validity = cast(JsonObject, report["metric_validity"])
    prediction_metrics_valid = "predictions_correct" not in cast(
        JsonObject, validity["invalid"]
    )

    assert render_markdown(report) == path.with_suffix(".md").read_text("utf-8")
    assert report_module._design(targets) == report["design"]
    assert (
        report_module._limitations(
            targets,
            study_version=study_version,
            prediction_metrics_valid=prediction_metrics_valid,
        )
        == report["limitations"]
    )
