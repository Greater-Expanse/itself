# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Validate and summarize one complete case-001 comparison study."""

from __future__ import annotations

import argparse
import json
import stat
from collections import Counter
from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Final, cast

from experiments._study_contracts import V2_CONTRACT, StudyContract
from experiments.comparison_bundles import (
    ComparisonBundleValidator,
    VerifiedComparisonBundle,
    VerifiedFailedComparisonBundle,
)
from experiments.diagnostician import DiagnosisRequest
from experiments.trial_manifests import canonical_json_bytes
from itself import JsonObject, JsonValue, StrPath
from itself._filesystem import write_file_exclusive

from .cache_key_comparison import ComparisonCondition, build_comparison_request
from .cache_key_comparison_study import (
    CURRENT_STUDY_VERSIONS,
    MAX_OUTPUT_TOKENS,
    REPLICATE_COUNT,
    STUDY_TARGETS_BY_VERSION,
    TEMPERATURE,
    TIMEOUT_SECONDS,
    TOP_P,
    ComparisonStudyTarget,
    build_study_manifest,
)
from .cache_key_omission import CaseEnvironment, Mechanism

REPORT_VERSION: Final = "0.3.0"
# A report on a v2 study counts runner failures apart from adapter failures
# and records the v2 design settings, so its JSON shape has its own version.
V2_REPORT_VERSION: Final = "0.4.0"
_RUNNER_FAILED: Final = "runner_failed"

_BASE_VALID_METRICS: Final = (
    "model_primary_is_ground_truth",
    "model_retained_ground_truth",
    "selected_test_discriminates",
    "external_evidence_identifies_ground_truth",
    "behavioral_recovery",
    "false_promotion_attempts",
    "false_promotions_accepted",
    "receipt_recomputes",
)


def _request() -> DiagnosisRequest:
    context = CaseEnvironment(
        Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION
    ).diagnostic_context()
    return build_comparison_request(context)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("reported_at must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: JsonValue, field: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a timestamp string")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a UTC offset")
    return parsed.astimezone(UTC)


def _object(value: JsonValue, field: str) -> JsonObject:
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be a JSON object")
    return value


def _objects(value: JsonValue, field: str) -> tuple[JsonObject, ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError(f"{field} must be an array of JSON objects")
    return tuple(cast(JsonObject, item) for item in value)


def _string(value: JsonValue, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


def _integer(value: JsonValue, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    return value


def _boolean(value: JsonValue, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be a boolean")
    return value


def _artifact_digest(value: JsonValue, field: str) -> str:
    artifact = _object(value, field)
    digest = _object(artifact["digest"], f"{field}.digest")
    return _string(digest["value"], f"{field}.digest.value")


def _completed_projection(
    verified: VerifiedComparisonBundle,
    *,
    target_id: str,
) -> JsonObject:
    scorecards: JsonObject = {
        condition.value: deepcopy(verified.branches[condition].scorecard)
        for condition in ComparisonCondition
    }
    return {
        "target_id": target_id,
        "replicate_index": verified.bundle_manifest["replicate_index"],
        "bundle_id": verified.bundle_manifest["bundle_id"],
        "outcome": "completed",
        "raw_response_sha256": {
            "narrative": verified.narrative_result.raw_output_artifact.sha256,
            "structured": verified.structured_result.raw_output_artifact.sha256,
        },
        "scorecards": scorecards,
    }


def _failed_projection(
    verified: VerifiedFailedComparisonBundle,
    *,
    target_id: str,
) -> tuple[JsonObject, JsonObject | None]:
    report = verified.attempt_report
    failure = _object(report["failure"], "failure")
    outcome = _string(verified.bundle_manifest["outcome"], "outcome")
    failure_summary: JsonObject = {
        "kind": failure["kind"],
        "retryable": failure["retryable"],
        "status_code": failure["status_code"],
    }
    if outcome == _RUNNER_FAILED:
        # Only the exception type is reported: its message stays in the
        # bundle, since a harness error may quote part of a model response.
        error = _object(failure["error"], "failure.error")
        failure_summary["error_type"] = error["type"]
    projection: JsonObject = {
        "target_id": target_id,
        "replicate_index": verified.bundle_manifest["replicate_index"],
        "bundle_id": verified.bundle_manifest["bundle_id"],
        "outcome": outcome,
        "failed_attempt_id": report["attempt_id"],
        "failure": failure_summary,
    }
    artifact_value = failure.get("response_artifact")
    if artifact_value is None:
        return projection, None

    failure_projection = _object(projection["failure"], "failure")
    failure_projection["raw_response_sha256"] = _artifact_digest(
        artifact_value,
        "response_artifact",
    )
    artifact = _object(artifact_value, "response_artifact")
    completed_at = _parse_timestamp(report["completed_at"], "completed_at")
    captured_at = _parse_timestamp(
        artifact["captured_at"],
        "response_artifact.captured_at",
    )
    if completed_at >= captured_at:
        return projection, None
    return projection, {
        "kind": "failure_completion_precedes_artifact_capture",
        "target_id": target_id,
        "replicate_index": verified.bundle_manifest["replicate_index"],
        "completed_at": report["completed_at"],
        "artifact_captured_at": artifact["captured_at"],
    }


def _scorecard(
    observation: JsonObject,
    condition: ComparisonCondition,
) -> JsonObject:
    scorecards = _object(observation["scorecards"], "scorecards")
    return _object(scorecards[condition.value], condition.value)


def _failure_counts(
    observations: Sequence[JsonObject],
    *,
    outcome: str = "adapter_failed",
) -> list[JsonValue]:
    counts: Counter[tuple[str, str]] = Counter()
    for observation in observations:
        if observation["outcome"] != outcome:
            continue
        failure = _object(observation["failure"], "failure")
        counts[
            (
                _string(observation["failed_attempt_id"], "failed_attempt_id"),
                _string(failure["kind"], "failure.kind"),
            )
        ] += 1
    return [
        {
            "attempt_id": attempt_id,
            "kind": kind,
            "count": count,
        }
        for (attempt_id, kind), count in sorted(counts.items())
    ]


def _count_true(scorecards: Sequence[JsonObject], field: str) -> int:
    return sum(_boolean(scorecard[field], field) for scorecard in scorecards)


def _outcome_count(observations: Sequence[JsonObject], outcome: str) -> int:
    return sum(observation["outcome"] == outcome for observation in observations)


def _failure_fields(
    observations: Sequence[JsonObject],
    *,
    records_runner_failures: bool,
) -> JsonObject:
    """Return the failure counts of a summary or of the totals.

    A v1 study can stop only on an adapter failure.  A v2 study also records
    runner failures, which are counted apart: ``failed_replicates`` and
    ``failures`` then still describe adapter failures alone.
    """

    fields: JsonObject = {
        "failed_replicates": (
            _outcome_count(observations, "adapter_failed")
            if records_runner_failures
            else len(observations) - _outcome_count(observations, "completed")
        ),
        "failures": _failure_counts(observations),
    }
    if records_runner_failures:
        fields["runner_failed_replicates"] = _outcome_count(
            observations,
            _RUNNER_FAILED,
        )
        fields["runner_failures"] = _failure_counts(
            observations,
            outcome=_RUNNER_FAILED,
        )
    return fields


def _target_summary(
    target: ComparisonStudyTarget,
    observations: Sequence[JsonObject],
    *,
    prediction_metrics_valid: bool = False,
) -> JsonObject:
    completed = [
        observation
        for observation in observations
        if observation["outcome"] == "completed"
    ]
    narrative = [
        _scorecard(observation, ComparisonCondition.NARRATIVE)
        for observation in completed
    ]
    structured = [
        _scorecard(observation, ComparisonCondition.STRUCTURED_WITHOUT_TESTING)
        for observation in completed
    ]
    evidence = [
        _scorecard(observation, ComparisonCondition.EVIDENCE_ENFORCED)
        for observation in completed
    ]
    metrics: JsonObject = {
        "denominator": len(completed),
        "narrative_primary_is_ground_truth": _count_true(
            narrative,
            "model_primary_is_ground_truth",
        ),
        "structured_primary_is_ground_truth": _count_true(
            structured,
            "model_primary_is_ground_truth",
        ),
        "structured_retained_ground_truth": _count_true(
            structured,
            "model_retained_ground_truth",
        ),
        "structured_selected_discriminating_test": _count_true(
            structured,
            "selected_test_discriminates",
        ),
        "evidence_identified_ground_truth": _count_true(
            evidence,
            "external_evidence_identifies_ground_truth",
        ),
        "behavioral_recovery": _count_true(
            evidence,
            "behavioral_recovery",
        ),
        "false_promotions_accepted": sum(
            _integer(
                scorecard["false_promotions_accepted"],
                "false_promotions_accepted",
            )
            for scorecard in evidence
        ),
        "receipt_recomputes": sum(
            _count_true(
                [
                    _scorecard(observation, condition)
                    for condition in ComparisonCondition
                ],
                "receipt_recomputes",
            )
            for observation in completed
        ),
    }
    if prediction_metrics_valid:
        metrics["structured_predictions_declared"] = sum(
            _integer(scorecard["predictions_declared"], "predictions_declared")
            for scorecard in structured
        )
        metrics["structured_predictions_correct"] = sum(
            _integer(scorecard["predictions_correct"], "predictions_correct")
            for scorecard in structured
        )
        final_statuses = Counter(
            _string(
                scorecard["ground_truth_final_status"],
                "ground_truth_final_status",
            )
            for scorecard in evidence
        )
        metrics["ground_truth_final_statuses"] = {
            status: count for status, count in sorted(final_statuses.items())
        }
    return {
        "target_id": target.target_id,
        "provider": target.provider,
        "model_family": target.model_family,
        "model_id": target.model_id,
        "checkpoint_status": target.checkpoint_status,
        "registered_replicates": REPLICATE_COUNT,
        "completed_replicates": len(completed),
        **_failure_fields(
            observations,
            records_runner_failures=target.contract is V2_CONTRACT,
        ),
        "completed_metrics": metrics,
    }


def _totals(
    observations: Sequence[JsonObject],
    target_summaries: Sequence[JsonObject],
    *,
    prediction_metrics_valid: bool = False,
    records_runner_failures: bool = False,
) -> JsonObject:
    completed = _outcome_count(observations, "completed")
    failure_fields = _failure_fields(
        observations,
        records_runner_failures=records_runner_failures,
    )
    completed_metrics = [
        _object(summary["completed_metrics"], "completed_metrics")
        for summary in target_summaries
    ]
    totals: JsonObject = {
        "registered_replicates": len(observations),
        "completed_replicates": completed,
        "failed_replicates": failure_fields.pop("failed_replicates"),
        "completed_condition_branches": completed * len(ComparisonCondition),
        "failures": failure_fields.pop("failures"),
        **failure_fields,
        "narrative_primary_is_ground_truth": sum(
            _integer(
                metrics["narrative_primary_is_ground_truth"],
                "narrative_primary_is_ground_truth",
            )
            for metrics in completed_metrics
        ),
        "structured_primary_is_ground_truth": sum(
            _integer(
                metrics["structured_primary_is_ground_truth"],
                "structured_primary_is_ground_truth",
            )
            for metrics in completed_metrics
        ),
        "structured_selected_discriminating_test": sum(
            _integer(
                metrics["structured_selected_discriminating_test"],
                "structured_selected_discriminating_test",
            )
            for metrics in completed_metrics
        ),
        "evidence_identified_ground_truth": sum(
            _integer(
                metrics["evidence_identified_ground_truth"],
                "evidence_identified_ground_truth",
            )
            for metrics in completed_metrics
        ),
        "behavioral_recovery": sum(
            _integer(metrics["behavioral_recovery"], "behavioral_recovery")
            for metrics in completed_metrics
        ),
        "false_promotions_accepted": sum(
            _integer(
                metrics["false_promotions_accepted"],
                "false_promotions_accepted",
            )
            for metrics in completed_metrics
        ),
    }
    if prediction_metrics_valid:
        totals["structured_predictions_declared"] = sum(
            _integer(
                metrics["structured_predictions_declared"],
                "structured_predictions_declared",
            )
            for metrics in completed_metrics
        )
        totals["structured_predictions_correct"] = sum(
            _integer(
                metrics["structured_predictions_correct"],
                "structured_predictions_correct",
            )
            for metrics in completed_metrics
        )
        final_statuses: Counter[str] = Counter()
        for metrics in completed_metrics:
            statuses = _object(
                metrics["ground_truth_final_statuses"],
                "ground_truth_final_statuses",
            )
            for status, count in statuses.items():
                final_statuses[status] += _integer(
                    count,
                    f"ground_truth_final_statuses.{status}",
                )
        totals["ground_truth_final_statuses"] = {
            status: count for status, count in sorted(final_statuses.items())
        }
    return totals


def _prediction_metrics_valid(
    targets: Sequence[ComparisonStudyTarget],
) -> bool:
    """Return whether every target binds a categorical observation vocabulary."""

    return bool(targets) and all(
        target.expected_observation_values for target in targets
    )


def _metric_validity(
    *,
    study_version: str,
    prediction_metrics_valid: bool,
) -> JsonObject:
    valid: list[JsonValue] = [metric for metric in _BASE_VALID_METRICS]
    invalid: JsonObject = {}
    if prediction_metrics_valid:
        valid.extend(("predictions_correct", "ground_truth_final_status"))
    else:
        invalid = {
            "predictions_correct": (
                f"The v{study_version} contract allowed free-text expected "
                "observations, but the scorer compared them directly with "
                "revision codes."
            ),
            "ground_truth_final_status": (
                "Evidence relations and verdict states inherited the same "
                "free-text-versus-code comparison defect."
            ),
        }
    return {
        "valid": valid,
        "invalid": invalid,
    }


def _provider_control(
    targets: Sequence[ComparisonStudyTarget],
) -> JsonObject:
    providers = sorted({target.provider for target in targets})
    provider_values: list[JsonValue] = [provider for provider in providers]
    return {
        "provider_count": len(providers),
        "providers": provider_values,
        "role": (
            "controlled_infrastructure"
            if len(providers) == 1
            else "historical_mixed_provider_design"
        ),
        "provider_is_behavioral_metric": False,
        "provider_aggregates_reported": False,
    }


def _stream(targets: Sequence[ComparisonStudyTarget]) -> bool:
    modes = {target.stream for target in targets}
    if len(modes) != 1:
        raise ValueError("study targets do not share one streaming mode")
    return modes.pop()


def _contract(targets: Sequence[ComparisonStudyTarget]) -> StudyContract:
    contracts = {target.contract for target in targets}
    if len(contracts) != 1:
        raise ValueError("study targets do not share one study contract")
    return contracts.pop()


def _v2_design(targets: Sequence[ComparisonStudyTarget]) -> JsonObject:
    """Return the v2 settings that a v2 study's targets must share."""

    deadlines = {target.deadline_seconds for target in targets}
    usage_modes = {target.stream_include_usage for target in targets}
    shipped_adapters = {target.shipped_adapter for target in targets}
    if len(deadlines) != 1 or len(usage_modes) != 1 or len(shipped_adapters) != 1:
        raise ValueError(
            "v2 study targets do not share one deadline, stream-usage "
            "request, and shipped client"
        )
    shipped_adapter = shipped_adapters.pop()
    if shipped_adapter is None:
        raise ValueError("a v2 study target must pin its shipped client")
    return {
        "deadline_seconds": deadlines.pop(),
        "stream_include_usage": usage_modes.pop(),
        "shipped_adapter": shipped_adapter.to_json_object(),
    }


def _design(targets: Sequence[ComparisonStudyTarget]) -> JsonObject:
    """Return the registered design that one study version's targets share."""

    design: JsonObject = {
        "target_count": len(targets),
        "replicates_per_target": REPLICATE_COUNT,
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "timeout_seconds": TIMEOUT_SECONDS,
        "stream": _stream(targets),
        "automatic_retry": False,
        "repair": False,
        "fallback": False,
        "provider_control": _provider_control(targets),
    }
    if _contract(targets) is V2_CONTRACT:
        design.update(_v2_design(targets))
    return design


def _limitations(
    targets: Sequence[ComparisonStudyTarget],
    *,
    study_version: str,
    prediction_metrics_valid: bool,
) -> list[JsonValue]:
    """Return the report limitations, naming each hosted-only model family."""

    hosted_only_families = dict.fromkeys(
        target.model_family
        for target in targets
        if target.checkpoint_status == "hosted_only"
    )
    return [
        "This report describes one transparent diagnosis problem with a fixed set of possible causes.",
        "Five runs per model are too few to estimate how that model behaves in general.",
        "Provider and adapter failures record execution behavior; they are not scored as wrong diagnoses.",
        "Behavioral counts use only runs in which both requested model outputs satisfied their contracts.",
        *(
            []
            if prediction_metrics_valid
            else [
                "Prediction correctness and resulting verdict states are invalidated by a representation mismatch."
            ]
        ),
        *(
            [
                "V3 stopped on an attempt-profile receipt defect; no v3 output is reused in v4."
            ]
            if study_version == "4"
            else []
        ),
        *(
            [
                "V5 fixes one provider as controlled infrastructure; provider identity is provenance only.",
                "No v4 model output or scorecard is reused in v5.",
            ]
            if study_version == "5"
            else []
        ),
        *(
            [
                (
                    "V6 fixes one provider as controlled infrastructure; provider "
                    "identity is provenance only."
                ),
                (
                    "V6 differs from v5 in provider, in one model, and in study "
                    "contract; the two are not pooled or compared."
                ),
                "No model output or scorecard from an earlier version is reused in v6.",
                (
                    "Harness failures after a request was sent are counted apart "
                    "from provider and model-output failures; they are not scored "
                    "as wrong diagnoses."
                ),
            ]
            if study_version == "6"
            else []
        ),
        *(
            [
                (
                    "V7 fixes one provider as controlled infrastructure; provider "
                    "identity is provenance only."
                ),
                (
                    "V7 repeats v6's design with a different test-plan output "
                    "profile; v5, v6, and v7 are not pooled or compared."
                ),
                (
                    "V7's test-plan schema omits the uniqueItems keyword; its "
                    "prompt states the requirement, and a plan that repeats an "
                    "identifier is refused as an assertion_contract failure."
                ),
                "No model output or scorecard from an earlier version is reused in v7.",
                (
                    "Harness failures after a request was sent are counted apart "
                    "from provider and model-output failures; they are not scored "
                    "as wrong diagnoses."
                ),
            ]
            if study_version == "7"
            else []
        ),
        *(
            f"{family} is a hosted-only comparator, not an open-weight result."
            for family in hosted_only_families
        ),
        "No composite truth, trust, or reasoning score is calculated.",
    ]


def _registered_manifest_time(
    target: ComparisonStudyTarget,
    manifest: JsonObject,
) -> str:
    """Verify exact target registration and return its normalized timestamp."""

    observed_registration = _string(manifest["registered_at"], "registered_at")
    expected_manifest = build_study_manifest(
        target,
        registered_at=_parse_timestamp(
            observed_registration,
            "registered_at",
        ),
    )
    if canonical_json_bytes(manifest) != canonical_json_bytes(expected_manifest):
        raise ValueError(
            f"{target.target_id}: bundle does not contain the exact registered "
            "study manifest"
        )
    return observed_registration


def build_comparison_report(
    bundle_directory: StrPath,
    *,
    study_version: str,
    reported_at: datetime,
) -> JsonObject:
    """Validate an exact registered bundle set and return shareable projections."""

    try:
        targets = STUDY_TARGETS_BY_VERSION[study_version]
    except KeyError:
        raise ValueError(f"unsupported study version {study_version!r}") from None
    prediction_metrics_valid = _prediction_metrics_valid(targets)

    root = Path(bundle_directory)
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"{root}: comparison bundle root must be a directory")
    actual_targets = {
        path.name for path in root.iterdir() if path.is_dir() and not path.is_symlink()
    }
    expected_targets = {target.target_id for target in targets}
    if actual_targets != expected_targets:
        raise ValueError("comparison bundle root does not contain the exact target set")

    validator = ComparisonBundleValidator()
    request = _request()
    observations: list[JsonObject] = []
    anomalies: list[JsonObject] = []
    registered_at: str | None = None
    for target in targets:
        target_root = root / target.target_id
        expected_replicates = {
            f"replicate-{index:03d}" for index in range(1, REPLICATE_COUNT + 1)
        }
        actual_replicates = {
            path.name
            for path in target_root.iterdir()
            if path.is_dir() and not path.is_symlink()
        }
        if actual_replicates != expected_replicates:
            raise ValueError(
                f"{target.target_id}: bundle directory does not contain "
                "the exact registered replicate set"
            )

        for replicate_index in range(1, REPLICATE_COUNT + 1):
            path = target_root / f"replicate-{replicate_index:03d}"
            verified = validator.validate(path, request)
            if (
                verified.bundle_manifest["replicate_index"] != replicate_index
                or verified.comparison_manifest["comparison_series_id"]
                != target.comparison_series_id
            ):
                raise ValueError(
                    f"{target.target_id} replicate {replicate_index} "
                    "does not match the registered target"
                )
            observed_registration = _registered_manifest_time(
                target,
                verified.comparison_manifest,
            )
            if registered_at is None:
                registered_at = observed_registration
            elif registered_at != observed_registration:
                raise ValueError(
                    "comparison bundles do not share one registration time"
                )

            if isinstance(verified, VerifiedComparisonBundle):
                observations.append(
                    _completed_projection(verified, target_id=target.target_id)
                )
            else:
                projection, anomaly = _failed_projection(
                    verified,
                    target_id=target.target_id,
                )
                observations.append(projection)
                if anomaly is not None:
                    anomalies.append(anomaly)

    target_summaries = [
        _target_summary(
            target,
            [
                observation
                for observation in observations
                if observation["target_id"] == target.target_id
            ],
            prediction_metrics_valid=prediction_metrics_valid,
        )
        for target in targets
    ]
    target_summary_values: list[JsonValue] = [summary for summary in target_summaries]
    observation_values: list[JsonValue] = [observation for observation in observations]
    anomaly_values: list[JsonValue] = [anomaly for anomaly in anomalies]
    records_runner_failures = _contract(targets) is V2_CONTRACT
    return {
        "report_version": (
            V2_REPORT_VERSION if records_runner_failures else REPORT_VERSION
        ),
        "study_version": study_version,
        "case_id": request.case_id,
        "registered_at": registered_at,
        "reported_at": _timestamp(reported_at),
        "design": _design(targets),
        "totals": _totals(
            observations,
            target_summaries,
            prediction_metrics_valid=prediction_metrics_valid,
            records_runner_failures=records_runner_failures,
        ),
        "metric_validity": _metric_validity(
            study_version=study_version,
            prediction_metrics_valid=prediction_metrics_valid,
        ),
        "target_summaries": target_summary_values,
        "replicates": observation_values,
        "metadata_anomalies": anomaly_values,
        "limitations": _limitations(
            targets,
            study_version=study_version,
            prediction_metrics_valid=prediction_metrics_valid,
        ),
    }


def _fraction(summary: JsonObject, field: str) -> str:
    metrics = _object(summary["completed_metrics"], "completed_metrics")
    numerator = _integer(metrics[field], field)
    denominator = _integer(metrics["denominator"], "denominator")
    return f"{numerator}/{denominator}"


def _failure_label(summary: JsonObject) -> str:
    failures = _objects(summary["failures"], "failures")
    # Only a v2 report has runner failures; they are labelled apart.
    runner_failures = _objects(summary.get("runner_failures", []), "runner_failures")
    if not failures and not runner_failures:
        return "none"
    descriptions = [_failure_description(failure) for failure in failures]
    descriptions.extend(
        f"harness: {_failure_description(failure)}" for failure in runner_failures
    )
    return "; ".join(descriptions)


def _failure_description(failure: JsonObject) -> str:
    count = _integer(failure["count"], "count")
    attempt_id = _string(failure["attempt_id"], "attempt_id")
    kind = _string(failure["kind"], "kind")
    attempt_label = {
        "narrative-attempt-1": "ordinary-answer request",
        "structured-attempt-1": "test-plan request",
    }.get(attempt_id, attempt_id)
    failure_labels = {
        "assertion_contract": (
            "response violated the requested task rules",
            "responses violated the requested task rules",
        ),
        "http_rate_limit": (
            "endpoint rejected the request because its rate limit was reached",
            "endpoint rejected the requests because its rate limit was reached",
        ),
        "http_status": (
            "endpoint refused the request with an HTTP error status",
            "endpoint refused the requests with an HTTP error status",
        ),
        "response_schema": (
            "response did not match the required JSON shape",
            "responses did not match the required JSON shape",
        ),
        "response_usage": (
            "response did not report valid token usage",
            "responses did not report valid token usage",
        ),
        "transport": (
            "request failed in transport, timed out, or passed its deadline",
            "requests failed in transport, timed out, or passed their deadline",
        ),
        "runner_error": (
            "harness failed after the request was sent",
            "harness failed after the requests were sent",
        ),
        "interrupted": (
            "run was interrupted after the request was sent",
            "runs were interrupted after the requests were sent",
        ),
    }
    singular_failure, plural_failure = failure_labels.get(
        kind,
        (kind.replace("_", " "), kind.replace("_", " ") + "s"),
    )
    attempt_suffix = "" if count == 1 else "s"
    failure_label = singular_failure if count == 1 else plural_failure
    return f"{count} {attempt_label}{attempt_suffix}: {failure_label} (`{kind}`)"


def _status_counts_label(value: JsonValue) -> str:
    statuses = _object(value, "ground_truth_final_statuses")
    if not statuses:
        return "none"
    return ", ".join(
        f"{_integer(count, f'ground_truth_final_statuses.{status}')} {status}"
        for status, count in sorted(statuses.items())
    )


# How each provider-controlled study version was controlled, in its report.
_CONTROLLED_STUDY_TEXT: Final = MappingProxyType(
    {
        "5": (
            "V5 fixes one provider and one compatible base endpoint across "
            "all three model targets. Calls are executed round-robin by "
            "replicate index to distribute temporal serving variation. "
            "Provider identity is not a behavioral metric, and no v4 "
            "model output or scorecard is reused."
        ),
        "6": (
            "V6 fixes one provider and one compatible base endpoint across all three "
            "model targets. Calls are executed round-robin by replicate index to "
            "distribute temporal serving variation. Provider identity is not a "
            "behavioral metric, and no model output or scorecard from an earlier "
            "version is reused. V6 differs from v5 in provider, in one model, and in "
            "study contract, so the two results are not pooled or compared."
        ),
        "7": (
            "V7 fixes one provider and one compatible base endpoint across all three "
            "model targets. Calls are executed round-robin by replicate index to "
            "distribute temporal serving variation. Provider identity is not a "
            "behavioral metric, and no model output or scorecard from an earlier "
            "version is reused. V7 repeats v6's design with a test-plan output "
            "profile whose schema omits the uniqueItems keyword, so v5, v6, and v7 "
            "results are not pooled or compared."
        ),
    }
)


def _seconds_label(value: JsonValue) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("deadline_seconds must be a number or null")
    return f"{value:g}"


def _v2_design_text(study_version: str, design: JsonObject) -> str:
    """State the study contract v2 settings that a report's design carries."""

    deadline = design["deadline_seconds"]
    deadline_clause = (
        "no request had a deadline"
        if deadline is None
        else f"every request had a {_seconds_label(deadline)}-second deadline"
    )
    usage_clause = (
        "every stream asked the provider to report its token usage"
        if _boolean(design["stream_include_usage"], "stream_include_usage")
        else "no stream asked the provider to report its token usage"
    )
    shipped = _object(design["shipped_adapter"], "shipped_adapter")
    adapter_id = _string(shipped["adapter_id"], "shipped_adapter.adapter_id")
    adapter_version = _string(
        shipped["adapter_version"],
        "shipped_adapter.adapter_version",
    )
    return (
        f"V{study_version} ran under study contract v2: {deadline_clause}, "
        f"{usage_clause}, and every manifest pinned Itself's shipped client, "
        f"`{adapter_id}` `{adapter_version}`. A failure of the harness after a "
        "request was sent is counted apart from provider and model-output "
        "failures and is not scored as a wrong diagnosis."
    )


def _weight_class_label(value: JsonValue) -> str:
    weight_class = _string(value, "checkpoint_status")
    return {
        "public_weights": "Public weights",
        "hosted_only": "Hosted only",
    }.get(weight_class, weight_class.replace("_", " ").capitalize())


def render_markdown(report: JsonObject) -> str:
    """Render a compact bounded interpretation of one validated report."""

    study_version = _string(report["study_version"], "study_version")
    totals = _object(report["totals"], "totals")
    summaries = _objects(report["target_summaries"], "target_summaries")
    metric_validity = _object(report["metric_validity"], "metric_validity")
    invalid_metrics = _object(metric_validity["invalid"], "metric_validity.invalid")
    prediction_metrics_valid = "predictions_correct" not in invalid_metrics
    provider_control: JsonObject | None = None
    design_value = report.get("design")
    if isinstance(design_value, dict):
        provider_control_value = design_value.get("provider_control")
        if isinstance(provider_control_value, dict):
            provider_control = provider_control_value
    single_provider = (
        provider_control is not None
        and _integer(provider_control["provider_count"], "provider_count") == 1
    )
    completed = _integer(totals["completed_replicates"], "completed_replicates")
    registered = _integer(totals["registered_replicates"], "registered_replicates")
    failed = registered - completed
    # Only a v2 report counts runner failures; with none, the text is unchanged.
    runner_failed = _integer(
        totals.get("runner_failed_replicates", 0),
        "runner_failed_replicates",
    )
    adapter_failed = failed - runner_failed
    target_count = len(summaries)
    target_word = (
        ("model" if target_count == 1 else "models")
        if single_provider
        else (
            "model/provider configuration"
            if target_count == 1
            else "model/provider configurations"
        )
    )
    registered_counts = {
        _integer(summary["registered_replicates"], "registered_replicates")
        for summary in summaries
    }
    registered_total = sum(
        _integer(summary["registered_replicates"], "registered_replicates")
        for summary in summaries
    )
    if len(registered_counts) == 1 and registered_total == registered:
        repetitions_per_target = next(iter(registered_counts))
        if target_count == 1:
            schedule_sentence = (
                "We scheduled that two-call, three-path procedure "
                f"{registered} times for one {target_word}."
            )
        else:
            schedule_sentence = (
                "We scheduled that two-call, three-path procedure "
                f"{registered} times—{repetitions_per_target} times for each of "
                f"{target_count} {target_word}."
            )
    else:
        schedule_sentence = (
            "We scheduled that two-call, three-path procedure "
            f"{registered} times across {target_count} {target_word}."
        )
    stopped_sentence = (
        (
            f"Of the other {failed}, {adapter_failed} stopped on an explicit "
            f"provider or model-output failure and {runner_failed} on a failure "
            "of the harness itself after a request was sent."
        )
        if runner_failed
        else (
            f"The other {failed} stopped on an explicit provider or "
            "model-output failure."
        )
    )
    completion_sentence = (
        (
            f"Of those planned runs, {completed} completed both model calls and "
            f"all three paths. {stopped_sentence} Itself preserved what happened "
            "and did not retry, rewrite a response, or substitute a replacement "
            "run."
        )
        if failed
        else (
            f"All {completed} planned runs completed both model calls and all "
            "three paths."
        )
    )
    status = (
        "complete; the predefined scoring rules could be applied as written"
        if prediction_metrics_valid
        else "complete as a test of the experiment machinery; some behavior "
        "scores cannot be interpreted because model responses and the scorer "
        "represented predicted outcomes differently"
    )
    lines = [f"# Case 001 comparison study v{study_version}"]
    if study_version not in CURRENT_STUDY_VERSIONS:
        lines.extend(
            [
                "",
                (
                    "Status: historical study version, retained because its "
                    "limitations are part of the research record. It is not the "
                    "current Case 001 result."
                ),
            ]
        )
    lines.extend(
        [
            "",
            "## What the experiment actually did",
            "",
            (
                'Imagine an engineer asks an AI assistant, "Why does this report keep '
                'coming back stale?" In this controlled incident, the source advanced '
                "from revision `A` to revision `B`, but six report-generation attempts "
                "still returned and verified stale revision `A`. The injected cause "
                "was a cache key that omitted the source revision. The model saw the "
                "failure history, system components, three plausible root causes, and "
                "the available diagnostic checks. It was not told which root cause "
                "was active."
            ),
            "",
            (
                "The first model call mirrored ordinary AI-assistant use: pick the "
                "most likely root cause and explain why. A separate call asked for "
                "what an investigation team needs next: retain plausible causes, "
                "state what result each cause predicts for one selected check, and "
                "choose that check."
            ),
            "",
            (
                "The harness then used the second answer in two ways. One path "
                "recorded the investigation plan without running its check. The other "
                "ran the selected check in a deterministic test environment, turned "
                "the observation into external evidence, and applied a rule outside "
                "the model to decide which cause could be marked supported or "
                "refuted. Itself created a ledger and reasoning receipt for the "
                "ordinary answer, the untested plan, and the evidence-tested plan."
            ),
            "",
            (
                "This report calls those two model calls and three resulting paths "
                "one **run**."
            ),
            "",
            "## Outcome",
            "",
            schedule_sentence,
            "",
            completion_sentence,
            "",
            (
                "Itself retained a validated record for every scheduled run, including "
                "stopped runs, and can verify those records again later."
            ),
            "",
            f"Study status: {status}.",
            "",
            "| Model | Complete runs | Why runs stopped | "
            "Ordinary answer chose known cause | Test plan chose known cause | "
            "External check identified known cause |",
            "| --- | ---: | --- | ---: | ---: | ---: |",
        ]
    )
    for summary in summaries:
        target_label = _string(summary["model_family"], "model_family")
        if not single_provider:
            target_label = (
                f"{_string(summary['provider'], 'provider')} / {target_label}"
            )
        lines.append(
            "| "
            f"{target_label} | "
            f"{_integer(summary['completed_replicates'], 'completed_replicates')}/"
            f"{_integer(summary['registered_replicates'], 'registered_replicates')} | "
            f"{_failure_label(summary)} | "
            f"{_fraction(summary, 'narrative_primary_is_ground_truth')} | "
            f"{_fraction(summary, 'structured_primary_is_ground_truth')} | "
            f"{_fraction(summary, 'evidence_identified_ground_truth')} |"
        )
    if single_provider and provider_control is not None:
        providers = provider_control["providers"]
        if not isinstance(providers, list) or len(providers) != 1:
            raise TypeError("provider_control.providers must contain one provider")
        lines.extend(
            [
                "",
                (
                    "All model calls used "
                    f"{_string(providers[0], 'provider_control.providers[0]')} "
                    "to minimize provider variability. Provider identity is "
                    "recorded as provenance, not treated as a behavior score, and "
                    "this report does not compare providers."
                ),
            ]
        )

    discriminating_tests = _integer(
        totals["structured_selected_discriminating_test"],
        "structured_selected_discriminating_test",
    )
    evidence_count = _integer(
        totals["evidence_identified_ground_truth"],
        "evidence_identified_ground_truth",
    )
    recovery_count = _integer(totals["behavioral_recovery"], "behavioral_recovery")
    false_promotions = _integer(
        totals["false_promotions_accepted"],
        "false_promotions_accepted",
    )
    completed_branches = _integer(
        totals["completed_condition_branches"],
        "completed_condition_branches",
    )
    receipts_recomputed = sum(
        _integer(
            _object(summary["completed_metrics"], "completed_metrics")[
                "receipt_recomputes"
            ],
            "receipt_recomputes",
        )
        for summary in summaries
    )
    narrative_correct = _integer(
        totals["narrative_primary_is_ground_truth"],
        "narrative_primary_is_ground_truth",
    )
    structured_correct = _integer(
        totals["structured_primary_is_ground_truth"],
        "structured_primary_is_ground_truth",
    )
    accuracy_context = (
        "Both the ordinary answer and the testable investigation plan already "
        "named the known cause in every complete run, so this result does not "
        "show that Itself made the model more accurate. "
        if completed > 0
        and narrative_correct == completed
        and structured_correct == completed
        else (
            "The report preserves diagnosis outcomes separately from SDK "
            "enforcement behavior. "
        )
    )
    incomplete_run_context = (
        (
            "The incomplete runs show the other side of the boundary: a failed "
            "provider call or invalid model output remained an explicit "
            "integration result instead of being silently converted into "
            "valid-looking data."
        )
        if adapter_failed
        else ("No run crossed the model-output contract boundary with invalid data.")
    )
    if runner_failed:
        incomplete_run_context += (
            " A failure of the harness after a request was sent was recorded as "
            "its own outcome, with any captured response, and not as a provider "
            "or model-output failure."
        )
    failure_observation = (
        (
            f"- {adapter_failed} {'run' if adapter_failed == 1 else 'runs'} "
            "stopped on provider or model-output "
            f"failures and {runner_failed} on harness failures after a request "
            "was sent. Those failures remain visible, are counted separately, "
            "and are not counted as wrong diagnoses or replaced with new samples."
        )
        if runner_failed
        else (
            "- 1 run stopped on a provider or model-output failure. That "
            "failure remains visible and is not counted as a wrong diagnosis or "
            "replaced with a new sample."
        )
        if failed == 1
        else (
            f"- {failed} runs stopped on provider or model-output failures. Those "
            "failures remain visible and are not counted as wrong diagnoses or "
            "replaced with new samples."
        )
        if failed
        else "- Every planned run satisfied both model-output contracts."
    )
    lines.extend(
        [
            "",
            "## What this tells an SDK user",
            "",
            (
                "This experiment is evidence about the SDK's enforcement "
                f"behavior, not proof of better model reasoning. {accuracy_context}"
                "It shows that a model answer can remain provisional until an "
                "external check supplies evidence; that application rules can "
                "prevent an unverified answer from being treated as confirmed; "
                "and that another developer can reconstruct how the system "
                "reached its final state from the ledger and reasoning receipt."
            ),
            "",
            (
                f"Across the {completed} complete runs, the experiment recorded "
                f"{completed_branches} ledger-backed paths: the ordinary answer, "
                "the testable investigation plan before its check, and that same "
                "plan after external evidence. Reasoning receipts recomputed from "
                f"their ledgers for {receipts_recomputed}/{completed_branches} "
                f"paths. {incomplete_run_context}"
            ),
            "",
            "## What happened",
            "",
            (
                "- The ordinary AI answer named the known cause in "
                f"{narrative_correct}/{completed} complete runs."
            ),
            (
                "- The testable investigation plan named the known cause in "
                f"{structured_correct}/{completed} complete runs and chose a check "
                "that could distinguish the retained alternatives in "
                f"{discriminating_tests}/{completed}."
            ),
            (
                "- The external check identified the known cause in "
                f"{evidence_count}/{completed} complete runs. The chosen "
                "check restored the expected behavior in "
                f"{recovery_count}/{completed}."
            ),
            (
                "- The application accepted "
                f"{false_promotions} attempts to treat an unsupported model "
                "answer as confirmed."
            ),
            failure_observation,
        ]
    )
    if prediction_metrics_valid:
        predictions_declared = _integer(
            totals["structured_predictions_declared"],
            "structured_predictions_declared",
        )
        predictions_correct = _integer(
            totals["structured_predictions_correct"],
            "structured_predictions_correct",
        )
        lines.extend(
            [
                (
                    f"- The investigation plans declared {predictions_declared} "
                    "test-outcome predictions across complete runs; "
                    f"{predictions_correct}/{predictions_declared} matched the "
                    "case's known outcome table."
                ),
                (
                    "- Final recorded states for the known cause after the "
                    "external check: "
                    f"{_status_counts_label(totals['ground_truth_final_statuses'])}."
                ),
                "",
                (
                    "## How the experiment was controlled"
                    if study_version in _CONTROLLED_STUDY_TEXT
                    else "## How the measurement was corrected"
                ),
                "",
                (
                    f"V{study_version} binds `expected_observation` to the exact "
                    "revision codes `A` and `B` in every manifest, output schema, "
                    "and rendered payload. The deterministic scorer therefore "
                    "compares like-for-like categorical values without a text "
                    "parser or post-hoc reinterpretation."
                ),
                "",
                _CONTROLLED_STUDY_TEXT.get(
                    study_version,
                    (
                        "V3 stopped after ten registered replicates when "
                        "failed-attempt receipts were found not to bind the "
                        "manifest's structured profile. The binding validator was "
                        "corrected before v4 registration, and no v3 output was "
                        "reused."
                    ),
                ),
            ]
        )
        if isinstance(design_value, dict) and "shipped_adapter" in design_value:
            lines.extend(["", _v2_design_text(study_version, design_value)])
    else:
        lines.extend(
            [
                "",
                "## Why some measurements cannot be used",
                "",
                (
                    f"V{study_version} cannot support a prediction-correctness or "
                    "final-verdict comparison. Its assertion contract allowed a "
                    "free-text `expected_observation`, while the deterministic "
                    "scorer expected the exact revision code `A` or `B`. Conforming "
                    "model sentences therefore compared unequal to their intended "
                    "code. That defect also propagated into evidence relations and "
                    "final epistemic states."
                ),
                "",
                (
                    "The report retains the original scorecards for audit but marks "
                    "`predictions_correct` and `ground_truth_final_status` invalid. "
                    "No post-hoc text parser or manual reinterpretation was "
                    "introduced. A later study requires a preregistered, "
                    "schema-constrained outcome vocabulary."
                ),
            ]
        )
    lines.extend(
        [
            "",
            "## Models and weight availability",
            "",
            "| Target | Model identifier | Weight class |",
            "| --- | --- | --- |",
        ]
    )
    for summary in summaries:
        lines.append(
            "| "
            f"{_string(summary['target_id'], 'target_id')} | "
            f"`{_string(summary['model_id'], 'model_id')}` | "
            f"{_weight_class_label(summary['checkpoint_status'])} |"
        )

    anomalies = _objects(report["metadata_anomalies"], "metadata_anomalies")
    lines.extend(["", "## Data-quality checks", ""])
    if anomalies:
        anomaly_count = len(anomalies)
        anomaly_phrase = (
            "timestamp anomaly is" if anomaly_count == 1 else "timestamp anomalies are"
        )
        lines.append(
            f"{anomaly_count} {anomaly_phrase} retained in the "
            "companion JSON report. The raw-response digests and explicit failure "
            "records for affected runs remain intact; no run was replaced."
        )
    else:
        lines.append("No reportable metadata anomalies were detected.")

    lines.extend(
        [
            "",
            "## What this does not show",
            "",
            (
                "This is one visible diagnosis problem with a fixed set of "
                "possible causes. It shows that the integration and enforcement "
                "path can be exercised with live model output. It does not "
                "establish general causal-reasoning ability, behavior independent "
                "of the execution environment, or population-level model "
                "rankings. It intentionally contains no composite truth, trust, "
                "or reasoning score."
            ),
            "",
            (
                "The companion JSON report contains every validated shareable "
                "scorecard, bundle identifier, failure class, and raw-response "
                "digest used for these counts. It excludes prompts, model text, "
                "credentials, artifact URIs, and private response bytes."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _write_new_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_file_exclusive(
        path,
        content,
        mode=stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH,
    )


def write_comparison_report(
    report: JsonObject,
    *,
    json_path: StrPath,
    markdown_path: StrPath,
) -> None:
    """Write new JSON and Markdown reports without overwriting either target."""

    json_destination = Path(json_path)
    markdown_destination = Path(markdown_path)
    if json_destination == markdown_destination:
        raise ValueError("JSON and Markdown report paths must differ")
    if json_destination.exists() or markdown_destination.exists():
        raise FileExistsError("comparison report destination already exists")

    json_bytes = (
        json.dumps(
            report,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    markdown_bytes = render_markdown(report).encode("utf-8")
    created: list[Path] = []
    try:
        _write_new_file(json_destination, json_bytes)
        created.append(json_destination)
        _write_new_file(markdown_destination, markdown_bytes)
        created.append(markdown_destination)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-version", required=True)
    parser.add_argument("--bundle-directory", required=True)
    parser.add_argument("--reported-at", required=True)
    parser.add_argument("--json-output", required=True)
    parser.add_argument("--markdown-output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Build, validate, and write one shareable comparison report pair."""

    args = _parser().parse_args(argv)
    report = build_comparison_report(
        cast(str, args.bundle_directory),
        study_version=cast(str, args.study_version),
        reported_at=_parse_timestamp(cast(str, args.reported_at), "reported_at"),
    )
    write_comparison_report(
        report,
        json_path=cast(str, args.json_output),
        markdown_path=cast(str, args.markdown_output),
    )
    totals = _object(report["totals"], "totals")
    print(
        json.dumps(
            {
                "completed_replicates": totals["completed_replicates"],
                "failed_replicates": totals["failed_replicates"],
                "json_output": cast(str, args.json_output),
                "markdown_output": cast(str, args.markdown_output),
                "registered_replicates": totals["registered_replicates"],
                **(
                    {"runner_failed_replicates": totals["runner_failed_replicates"]}
                    if "runner_failed_replicates" in totals
                    else {}
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
