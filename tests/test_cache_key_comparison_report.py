# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import stat
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from experiments.cases.cache_key_comparison import ComparisonCondition
from experiments.cases.cache_key_comparison_report import (
    _metric_validity,
    _provider_control,
    _registered_manifest_time,
    _target_summary,
    _totals,
    render_markdown,
    write_comparison_report,
)
from experiments.cases.cache_key_comparison_study import (
    ComparisonStudyTarget,
    build_study_manifest,
)
from itself import JsonObject, JsonValue


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
