# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import cast

import pytest

from experiments.cases.cache_key_comparison import (
    COMPARATIVE_TASK,
    NARRATIVE_ATTEMPT_ID,
    STRUCTURED_ATTEMPT_ID,
    ComparisonCondition,
    build_comparison_request,
    project_comparative_results,
    run_comparative_case,
)
from experiments.cases.cache_key_diagnostician import (
    CacheKeyFixtureDiagnostician,
    CacheKeyNarrativeFixtureDiagnostician,
)
from experiments.cases.cache_key_omission import (
    CaseEnvironment,
    Intervention,
    Mechanism,
)
from experiments.cases.cache_key_scripted_run import (
    DIAGNOSTIC_ARTIFACT_ID,
    HYPOTHESIS_IDS,
    TEST_EVIDENCE_ID,
    TEST_RUN_ID,
)
from experiments.diagnostician import DiagnosisRequest, DiagnosisResult
from experiments.narrative_diagnostician import NarrativeResult
from itself import ClaimStatus, JsonObject
from itself.receipts import ReasoningReceiptValidator


def _narrative_requests() -> list[DiagnosisRequest]:
    return []


def _structured_requests() -> list[DiagnosisRequest]:
    return []


@dataclass(slots=True)
class RecordingNarrativeDiagnostician:
    requests: list[DiagnosisRequest] = field(default_factory=_narrative_requests)

    def diagnose(self, request: DiagnosisRequest, /) -> NarrativeResult:
        self.requests.append(request)
        return CacheKeyNarrativeFixtureDiagnostician().diagnose(request)


@dataclass(slots=True)
class RecordingStructuredDiagnostician:
    requests: list[DiagnosisRequest] = field(default_factory=_structured_requests)

    def diagnose(self, request: DiagnosisRequest, /) -> DiagnosisResult:
        self.requests.append(request)
        return CacheKeyFixtureDiagnostician().diagnose(request)


def _record_kinds(records: tuple[JsonObject, ...]) -> list[str]:
    return [cast(str, record["kind"]) for record in records]


def _record_by_id(records: tuple[JsonObject, ...], record_id: str) -> JsonObject:
    return next(record for record in records if record.get("id") == record_id)


def test_both_model_attempts_receive_the_same_neutral_request_once() -> None:
    narrative = RecordingNarrativeDiagnostician()
    structured = RecordingStructuredDiagnostician()

    result = run_comparative_case(narrative, structured)

    assert narrative.requests == [result.request]
    assert structured.requests == [result.request]
    assert narrative.requests[0] is structured.requests[0]
    assert result.request.task == COMPARATIVE_TASK
    assert len(result.request.hypothesis_options) == 3
    assert len(result.request.test_options) == 4


def test_captured_results_can_be_projected_without_new_model_attempts() -> None:
    original = run_comparative_case()

    projected = project_comparative_results(
        original.request,
        original.narrative_result,
        original.structured_result,
    )

    assert projected.narrative.ledger.records == original.narrative.ledger.records
    assert projected.narrative.scorecard == original.narrative.scorecard
    assert (
        projected.structured_without_testing.ledger.records
        == original.structured_without_testing.ledger.records
    )
    assert (
        projected.structured_without_testing.scorecard
        == original.structured_without_testing.scorecard
    )
    assert (
        projected.evidence_enforced.ledger.records
        == original.evidence_enforced.ledger.records
    )
    assert projected.evidence_enforced.scorecard == original.evidence_enforced.scorecard


def test_captured_result_projection_rejects_request_drift() -> None:
    original = run_comparative_case()

    with pytest.raises(ValueError, match="frozen case-001 input"):
        project_comparative_results(
            replace(original.request, task="A changed task"),
            original.narrative_result,
            original.structured_result,
        )


def test_comparison_request_is_identical_for_every_hidden_mechanism() -> None:
    requests = {
        json.dumps(
            build_comparison_request(
                CaseEnvironment(mechanism).diagnostic_context()
            ).to_json_object(),
            sort_keys=True,
        )
        for mechanism in Mechanism
    }

    assert len(requests) == 1


def test_narrative_branch_stops_at_one_proposed_hypothesis() -> None:
    branch = run_comparative_case().narrative
    kinds = _record_kinds(branch.ledger.records)

    assert kinds == ["artifact_reference", "hypothesis", "evidence"]
    assert set(branch.ledger.snapshot.current_states.values()) == {ClaimStatus.PROPOSED}
    assert "prediction" not in kinds
    assert "test" not in kinds
    assert "verdict" not in kinds
    assert "status_transition" not in kinds


def test_structured_branch_stops_at_testable_planned_state() -> None:
    branch = run_comparative_case().structured_without_testing
    kinds = _record_kinds(branch.ledger.records)

    assert len(branch.ledger) == 12
    assert set(branch.ledger.snapshot.current_states.values()) == {ClaimStatus.TESTABLE}
    assert kinds.count("prediction") == 3
    assert kinds.count("test") == 1
    assert kinds.count("evidence") == 1
    assert "verdict" not in kinds
    assert TEST_RUN_ID not in branch.ledger.snapshot.record_ids
    assert TEST_EVIDENCE_ID not in branch.ledger.snapshot.record_ids


def test_evidence_branch_forks_without_mutating_structured_branch() -> None:
    result = run_comparative_case()
    structured = result.structured_without_testing
    evidence = result.evidence_enforced

    assert len(structured.ledger) == 12
    assert len(evidence.ledger) == 23
    assert evidence.ledger.records[: len(structured.ledger)] == (
        structured.ledger.records
    )
    assert set(evidence.ledger.snapshot.current_states.values()) == {
        ClaimStatus.SUPPORTED,
        ClaimStatus.REFUTED,
    }
    assert (
        evidence.ledger.snapshot.current_states[
            HYPOTHESIS_IDS[Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION]
        ]
        is ClaimStatus.SUPPORTED
    )


def test_structured_branches_bind_the_same_result_and_raw_artifact() -> None:
    result = run_comparative_case()
    structured_artifact = _record_by_id(
        result.structured_without_testing.ledger.records,
        DIAGNOSTIC_ARTIFACT_ID,
    )
    evidence_artifact = _record_by_id(
        result.evidence_enforced.ledger.records,
        DIAGNOSTIC_ARTIFACT_ID,
    )

    assert structured_artifact == evidence_artifact
    assert structured_artifact["digest"] == {
        "algorithm": "sha256",
        "value": result.structured_result.raw_output_artifact.sha256,
    }
    assert (
        result.structured_without_testing.scorecard.source_attempt_id
        == result.evidence_enforced.scorecard.source_attempt_id
        == STRUCTURED_ATTEMPT_ID
    )


def test_common_scorecards_preserve_not_applicable_values() -> None:
    result = run_comparative_case()
    narrative = result.narrative.scorecard
    structured = result.structured_without_testing.scorecard
    evidence = result.evidence_enforced.scorecard

    assert narrative.condition is ComparisonCondition.NARRATIVE
    assert narrative.source_attempt_id == NARRATIVE_ATTEMPT_ID
    assert narrative.model_primary_is_ground_truth
    assert narrative.model_retained_ground_truth
    assert narrative.retained_hypothesis_count == 1
    assert not narrative.test_selected
    assert narrative.selected_test_id is None
    assert narrative.selected_test_discriminates is None
    assert narrative.predictions_declared is None
    assert narrative.predictions_correct is None
    assert narrative.external_tests_executed == 0
    assert narrative.external_evidence_identifies_ground_truth is None
    assert narrative.behavioral_recovery is None
    assert narrative.ground_truth_final_status == "proposed"
    assert narrative.ledger_record_count == 3

    assert structured.condition is ComparisonCondition.STRUCTURED_WITHOUT_TESTING
    assert structured.model_primary_is_ground_truth
    assert structured.model_retained_ground_truth
    assert structured.retained_hypothesis_count == 3
    assert structured.test_selected
    assert structured.selected_test_id == Intervention.BYPASS_CACHING_PROXY.value
    assert structured.selected_test_discriminates is True
    assert structured.predictions_declared == 3
    assert structured.predictions_correct == 3
    assert structured.external_tests_executed == 0
    assert structured.external_evidence_identifies_ground_truth is None
    assert structured.behavioral_recovery is None
    assert structured.ground_truth_final_status == "testable"
    assert structured.ledger_record_count == 12

    assert evidence.condition is ComparisonCondition.EVIDENCE_ENFORCED
    assert evidence.source_attempt_id == structured.source_attempt_id
    assert evidence.external_tests_executed == 1
    assert evidence.external_evidence_identifies_ground_truth is True
    assert evidence.behavioral_recovery is True
    assert evidence.ground_truth_final_status == "supported"
    assert evidence.false_promotion_attempts == 1
    assert evidence.false_promotions_accepted == 0
    assert evidence.ledger_record_count == 23


def test_wrong_narrative_selection_remains_proposed_without_ground_truth() -> None:
    result = run_comparative_case(
        CacheKeyNarrativeFixtureDiagnostician(
            selected_hypothesis=Mechanism.WORKER_READS_STALE_SOURCE
        )
    )
    scorecard = result.narrative.scorecard

    assert not scorecard.model_primary_is_ground_truth
    assert not scorecard.model_retained_ground_truth
    assert scorecard.ground_truth_final_status == "absent"
    assert set(result.narrative.ledger.snapshot.current_states.values()) == {
        ClaimStatus.PROPOSED
    }


def test_external_evidence_can_correct_shared_wrong_primary() -> None:
    result = run_comparative_case(
        structured_diagnostician=CacheKeyFixtureDiagnostician(
            primary_hypothesis=Mechanism.WORKER_READS_STALE_SOURCE
        )
    )

    assert not result.structured_without_testing.scorecard.model_primary_is_ground_truth
    assert result.structured_without_testing.scorecard.ground_truth_final_status == (
        "testable"
    )
    assert result.evidence_enforced.scorecard.ground_truth_final_status == "supported"


def test_non_discriminating_shared_test_stays_inconclusive_after_execution() -> None:
    result = run_comparative_case(
        structured_diagnostician=CacheKeyFixtureDiagnostician(
            selected_test=Intervention.UNCHANGED_REPLAY
        )
    )

    assert (
        result.structured_without_testing.scorecard.selected_test_discriminates is False
    )
    assert (
        result.evidence_enforced.scorecard.external_evidence_identifies_ground_truth
        is False
    )
    assert result.evidence_enforced.scorecard.ground_truth_final_status == (
        "inconclusive"
    )


def test_every_branch_receipt_recomputes_against_its_ledger() -> None:
    result = run_comparative_case()

    for branch in (
        result.narrative,
        result.structured_without_testing,
        result.evidence_enforced,
    ):
        ReasoningReceiptValidator().validate_against_ledger(
            branch.receipt,
            branch.ledger,
        )
        assert branch.scorecard.receipt_recomputes
