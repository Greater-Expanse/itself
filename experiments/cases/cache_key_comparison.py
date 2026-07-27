# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Paired three-condition comparison for causal-diagnosis case 001."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

from experiments.diagnostician import (
    DiagnosisRequest,
    DiagnosisResult,
    Diagnostician,
    DiagnosticianContractValidator,
)
from experiments.narrative_diagnostician import (
    NarrativeContractValidator,
    NarrativeDiagnostician,
    NarrativeResult,
)
from itself import (
    ActorRole,
    ActorType,
    ClaimStatus,
    JsonObject,
    Ledger,
    build_reasoning_receipt,
)
from itself.receipts import ReasoningReceiptValidator

from .cache_key_diagnostician import (
    CASE_ID,
    CacheKeyFixtureDiagnostician,
    CacheKeyNarrativeFixtureDiagnostician,
    build_cache_key_request,
)
from .cache_key_omission import (
    CaseEnvironment,
    CausalOracle,
    DiagnosticContext,
    Intervention,
    Mechanism,
    Revision,
)
from .cache_key_scripted_run import (
    CASE_SCOPE,
    HYPOTHESIS_IDS,
    PROTOCOL_VERSION,
    PreparedStructuredCase,
    ScriptedRunResult,
    complete_evidence_enforced_case,
    prepare_structured_case,
)

COMPARATIVE_TASK: Final = "Identify the cause of the repeated stale report output."
NARRATIVE_ATTEMPT_ID: Final = "narrative-attempt-1"
STRUCTURED_ATTEMPT_ID: Final = "structured-attempt-1"
NARRATIVE_ARTIFACT_ID: Final = "artifact-narrative-diagnostician-output"
_RUN_START: Final = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)

COMPARISON_METRICS: Final = (
    "condition",
    "source_attempt_id",
    "model_primary_is_ground_truth",
    "model_retained_ground_truth",
    "retained_hypothesis_count",
    "expressed_confidence",
    "test_selected",
    "selected_test_id",
    "selected_test_discriminates",
    "predictions_declared",
    "predictions_correct",
    "external_tests_executed",
    "external_evidence_identifies_ground_truth",
    "behavioral_recovery",
    "ground_truth_final_status",
    "false_promotion_attempts",
    "false_promotions_accepted",
    "ledger_record_count",
    "model_input_tokens",
    "model_output_tokens",
    "wall_time_ms",
    "receipt_recomputes",
)


class ComparisonCondition(StrEnum):
    """Registered branches in the case-001 comparative design."""

    NARRATIVE = "narrative_diagnosis"
    STRUCTURED_WITHOUT_TESTING = "structured_without_testing"
    EVIDENCE_ENFORCED = "evidence_enforced"


@dataclass(frozen=True, slots=True)
class ComparisonScorecard:
    """Common non-composite measurements for one comparison branch."""

    condition: ComparisonCondition
    source_attempt_id: str
    model_primary_is_ground_truth: bool
    model_retained_ground_truth: bool
    retained_hypothesis_count: int
    expressed_confidence: float | None
    test_selected: bool
    selected_test_id: str | None
    selected_test_discriminates: bool | None
    predictions_declared: int | None
    predictions_correct: int | None
    external_tests_executed: int
    external_evidence_identifies_ground_truth: bool | None
    behavioral_recovery: bool | None
    ground_truth_final_status: str
    false_promotion_attempts: int
    false_promotions_accepted: int
    ledger_record_count: int
    model_input_tokens: int
    model_output_tokens: int
    wall_time_ms: int
    receipt_recomputes: bool

    def to_json_object(self) -> JsonObject:
        """Return the exact common scorecard representation."""

        return {
            "condition": self.condition.value,
            "source_attempt_id": self.source_attempt_id,
            "model_primary_is_ground_truth": self.model_primary_is_ground_truth,
            "model_retained_ground_truth": self.model_retained_ground_truth,
            "retained_hypothesis_count": self.retained_hypothesis_count,
            "expressed_confidence": self.expressed_confidence,
            "test_selected": self.test_selected,
            "selected_test_id": self.selected_test_id,
            "selected_test_discriminates": self.selected_test_discriminates,
            "predictions_declared": self.predictions_declared,
            "predictions_correct": self.predictions_correct,
            "external_tests_executed": self.external_tests_executed,
            "external_evidence_identifies_ground_truth": (
                self.external_evidence_identifies_ground_truth
            ),
            "behavioral_recovery": self.behavioral_recovery,
            "ground_truth_final_status": self.ground_truth_final_status,
            "false_promotion_attempts": self.false_promotion_attempts,
            "false_promotions_accepted": self.false_promotions_accepted,
            "ledger_record_count": self.ledger_record_count,
            "model_input_tokens": self.model_input_tokens,
            "model_output_tokens": self.model_output_tokens,
            "wall_time_ms": self.wall_time_ms,
            "receipt_recomputes": self.receipt_recomputes,
        }


@dataclass(frozen=True, slots=True)
class ComparisonBranchResult:
    """Ledger, scorecard, and receipt for one condition branch."""

    condition: ComparisonCondition
    ledger: Ledger
    scorecard: ComparisonScorecard
    receipt: JsonObject


@dataclass(frozen=True, slots=True)
class ComparativeRunResult:
    """Two model results projected into three explicitly dependent branches."""

    request: DiagnosisRequest
    narrative_result: NarrativeResult
    structured_result: DiagnosisResult
    narrative: ComparisonBranchResult
    structured_without_testing: ComparisonBranchResult
    evidence_enforced: ComparisonBranchResult


def build_comparison_request(context: DiagnosticContext) -> DiagnosisRequest:
    """Build the shared neutral-task request used by both model attempts."""

    return replace(build_cache_key_request(context), task=COMPARATIVE_TASK)


def _created_at(position: int) -> str:
    value = _RUN_START + timedelta(seconds=position)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _actor(
    actor_id: str,
    actor_type: ActorType,
    role: ActorRole,
    *,
    implementation_ref: str | None = None,
) -> JsonObject:
    value: JsonObject = {
        "id": actor_id,
        "actor_type": actor_type.value,
        "role": role.value,
    }
    if implementation_ref is not None:
        value["implementation_ref"] = implementation_ref
    return value


def _scope() -> JsonObject:
    return {
        "description": CASE_SCOPE,
        "dimensions": {
            "case_id": CASE_ID,
            "environment_version": "1",
            "source_revision": Revision.B.value,
        },
    }


def _narrative_ledger(
    request: DiagnosisRequest,
    result: NarrativeResult,
    context: DiagnosticContext,
) -> Ledger:
    selected = Mechanism(result.assertion.selected_hypothesis_id)
    hypothesis_id = HYPOTHESIS_IDS[selected]
    option = next(
        option for option in request.hypothesis_options if option.id == selected.value
    )
    actor = _actor(
        result.diagnostician.actor_id,
        result.diagnostician.actor_type,
        ActorRole.PROPOSER,
        implementation_ref=(
            f"{result.diagnostician.adapter_id}@{result.diagnostician.adapter_version}"
        ),
    )
    artifact = result.raw_output_artifact
    records: tuple[JsonObject, ...] = (
        {
            "protocol_version": PROTOCOL_VERSION,
            "kind": "artifact_reference",
            "id": NARRATIVE_ARTIFACT_ID,
            "created_at": _created_at(0),
            "created_by": actor,
            "uri": artifact.uri,
            "media_type": artifact.media_type,
            "title": "Captured narrative diagnostician output",
            "digest": {
                "algorithm": "sha256",
                "value": artifact.sha256,
            },
            "captured_at": artifact.captured_at,
        },
        {
            "protocol_version": PROTOCOL_VERSION,
            "kind": "hypothesis",
            "id": hypothesis_id,
            "created_at": _created_at(1),
            "created_by": actor,
            "statement": option.statement,
            "status": ClaimStatus.PROPOSED.value,
            "scope": _scope(),
            "dependency_refs": [NARRATIVE_ARTIFACT_ID],
        },
        {
            "protocol_version": PROTOCOL_VERSION,
            "kind": "evidence",
            "id": "evidence-baseline-failures",
            "created_at": _created_at(2),
            "created_by": _actor(
                "case-001-environment",
                ActorType.SOFTWARE,
                ActorRole.EVALUATOR,
            ),
            "evidence_type": "observation",
            "relations": [
                {
                    "subject_ref": hypothesis_id,
                    "relation": "contextualizes",
                    "public_note": (
                        "The repeated failure is compatible with the selected "
                        "narrative hypothesis."
                    ),
                }
            ],
            "authority": {
                "authority_type": "deterministic_tool",
                "actor_ref": "case-001-environment",
                "basis": "Six deterministic unchanged replays from reset state",
            },
            "scope": _scope(),
            "result": {
                "observations": [
                    observation.to_json_object()
                    for observation in context.baseline_observations
                ]
            },
        },
    )
    return Ledger(records)


def _receipt(ledger: Ledger) -> JsonObject:
    receipt = build_reasoning_receipt(ledger)
    ReasoningReceiptValidator().validate_against_ledger(receipt, ledger)
    return receipt


def _ground_truth_status(ledger: Ledger, ground_truth: Mechanism) -> str:
    status = ledger.snapshot.current_states.get(HYPOTHESIS_IDS[ground_truth])
    return "absent" if status is None else status.value


def _narrative_scorecard(
    result: NarrativeResult,
    ledger: Ledger,
    *,
    ground_truth: Mechanism,
) -> ComparisonScorecard:
    selected = Mechanism(result.assertion.selected_hypothesis_id)
    usage = result.usage
    return ComparisonScorecard(
        condition=ComparisonCondition.NARRATIVE,
        source_attempt_id=NARRATIVE_ATTEMPT_ID,
        model_primary_is_ground_truth=selected is ground_truth,
        model_retained_ground_truth=selected is ground_truth,
        retained_hypothesis_count=1,
        expressed_confidence=result.assertion.expressed_confidence,
        test_selected=False,
        selected_test_id=None,
        selected_test_discriminates=None,
        predictions_declared=None,
        predictions_correct=None,
        external_tests_executed=0,
        external_evidence_identifies_ground_truth=None,
        behavioral_recovery=None,
        ground_truth_final_status=_ground_truth_status(ledger, ground_truth),
        false_promotion_attempts=0,
        false_promotions_accepted=0,
        ledger_record_count=len(ledger),
        model_input_tokens=usage.model_input_tokens,
        model_output_tokens=usage.model_output_tokens,
        wall_time_ms=usage.wall_time_ms,
        receipt_recomputes=True,
    )


def _prediction_count(prepared: PreparedStructuredCase) -> tuple[int, int]:
    predictions = prepared.diagnosis.assertion.predictions
    correct = sum(
        prediction.expected_observation
        == CausalOracle.predict(
            Mechanism(prediction.hypothesis_id),
            Intervention(prediction.test_id),
        ).value
        for prediction in predictions
    )
    return len(predictions), correct


def _structured_scorecard(
    prepared: PreparedStructuredCase,
    ledger: Ledger,
    *,
    condition: ComparisonCondition,
    completed: ScriptedRunResult | None,
    ground_truth: Mechanism,
) -> ComparisonScorecard:
    if condition is ComparisonCondition.NARRATIVE:
        raise ValueError("structured scorecard cannot use the narrative condition")
    declared, correct = _prediction_count(prepared)
    retained = prepared.retained_mechanisms
    usage = prepared.diagnosis.usage
    selected_discriminates = CausalOracle.discriminates(
        prepared.intervention,
        prepared.selected_hypothesis,
        tuple(
            mechanism
            for mechanism in retained
            if mechanism is not prepared.selected_hypothesis
        ),
    )
    return ComparisonScorecard(
        condition=condition,
        source_attempt_id=STRUCTURED_ATTEMPT_ID,
        model_primary_is_ground_truth=prepared.selected_hypothesis is ground_truth,
        model_retained_ground_truth=ground_truth in retained,
        retained_hypothesis_count=len(retained),
        expressed_confidence=prepared.diagnosis.assertion.expressed_confidence,
        test_selected=True,
        selected_test_id=prepared.intervention.value,
        selected_test_discriminates=selected_discriminates,
        predictions_declared=declared,
        predictions_correct=correct,
        external_tests_executed=0 if completed is None else 1,
        external_evidence_identifies_ground_truth=(
            None
            if completed is None
            else completed.metrics.evidence_identifies_ground_truth
        ),
        behavioral_recovery=(
            None if completed is None else completed.metrics.behavioral_recovery
        ),
        ground_truth_final_status=_ground_truth_status(ledger, ground_truth),
        false_promotion_attempts=(
            0 if completed is None else completed.metrics.false_promotion_attempts
        ),
        false_promotions_accepted=(
            0 if completed is None else completed.metrics.false_promotions_accepted
        ),
        ledger_record_count=len(ledger),
        model_input_tokens=usage.model_input_tokens,
        model_output_tokens=usage.model_output_tokens,
        wall_time_ms=usage.wall_time_ms,
        receipt_recomputes=True,
    )


def run_comparative_case(
    narrative_diagnostician: NarrativeDiagnostician | None = None,
    structured_diagnostician: Diagnostician | None = None,
) -> ComparativeRunResult:
    """Run two diagnosticians and project one structured result into B and C."""

    ground_truth = Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION
    context = CaseEnvironment(ground_truth).diagnostic_context()
    request = build_comparison_request(context)
    narrative_result = NarrativeContractValidator().invoke(
        narrative_diagnostician or CacheKeyNarrativeFixtureDiagnostician(),
        request,
    )
    structured_result = DiagnosticianContractValidator().invoke(
        structured_diagnostician or CacheKeyFixtureDiagnostician(),
        request,
    )
    return project_comparative_results(
        request,
        narrative_result,
        structured_result,
    )


def project_comparative_results(
    request: DiagnosisRequest,
    narrative_result: NarrativeResult,
    structured_result: DiagnosisResult,
) -> ComparativeRunResult:
    """Deterministically project two captured attempts into three branches."""

    ground_truth = Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION
    context = CaseEnvironment(ground_truth).diagnostic_context()
    expected_request = build_comparison_request(context)
    if request.to_json_object() != expected_request.to_json_object():
        raise ValueError("comparison request does not match frozen case-001 input")
    NarrativeContractValidator().validate_result(request, narrative_result)
    DiagnosticianContractValidator().validate_result(request, structured_result)
    prepared = prepare_structured_case(
        request,
        structured_result,
        context.baseline_observations,
    )
    narrative_ledger = _narrative_ledger(request, narrative_result, context)
    structured_ledger = Ledger(prepared.ledger.records)
    completed = complete_evidence_enforced_case(prepared, ground_truth=ground_truth)

    narrative_receipt = _receipt(narrative_ledger)
    structured_receipt = _receipt(structured_ledger)
    evidence_receipt = _receipt(completed.ledger)
    narrative_branch = ComparisonBranchResult(
        condition=ComparisonCondition.NARRATIVE,
        ledger=narrative_ledger,
        scorecard=_narrative_scorecard(
            narrative_result,
            narrative_ledger,
            ground_truth=ground_truth,
        ),
        receipt=narrative_receipt,
    )
    structured_branch = ComparisonBranchResult(
        condition=ComparisonCondition.STRUCTURED_WITHOUT_TESTING,
        ledger=structured_ledger,
        scorecard=_structured_scorecard(
            prepared,
            structured_ledger,
            condition=ComparisonCondition.STRUCTURED_WITHOUT_TESTING,
            completed=None,
            ground_truth=ground_truth,
        ),
        receipt=structured_receipt,
    )
    evidence_branch = ComparisonBranchResult(
        condition=ComparisonCondition.EVIDENCE_ENFORCED,
        ledger=completed.ledger,
        scorecard=_structured_scorecard(
            prepared,
            completed.ledger,
            condition=ComparisonCondition.EVIDENCE_ENFORCED,
            completed=completed,
            ground_truth=ground_truth,
        ),
        receipt=evidence_receipt,
    )
    return ComparativeRunResult(
        request=request,
        narrative_result=narrative_result,
        structured_result=structured_result,
        narrative=narrative_branch,
        structured_without_testing=structured_branch,
        evidence_enforced=evidence_branch,
    )
