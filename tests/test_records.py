# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from itself import (
    Actor,
    ActorRole,
    ActorType,
    Authority,
    AuthorityType,
    BundleIntegrityError,
    ClaimStatus,
    ClaimType,
    DecisionDisposition,
    Digest,
    DigestAlgorithm,
    EvidenceRelation,
    EvidenceRelationType,
    EvidenceType,
    IntegrityCode,
    JsonObject,
    JsonValue,
    Ledger,
    Oracle,
    PredictionCondition,
    ProtocolValidator,
    RecordConstructionError,
    RecordHeader,
    Scope,
    TestCost,
    TestDesign,
    TestStatus,
    VerdictOutcome,
    artifact_reference_record,
    claim_record,
    decision_record,
    evidence_record,
    hypothesis_record,
    prediction_record,
    protocol_test_record,
    status_transition_record,
    verdict_record,
)

START = datetime(2026, 7, 23, 12, tzinfo=UTC)
SCOPE = Scope(
    description="One controlled incident-diagnosis run",
    dimensions={"case_id": "typed-records", "environment_version": 1},
)
MODEL = Actor(
    actor_id="diagnostician",
    actor_type=ActorType.MODEL,
    role=ActorRole.PROPOSER,
    implementation_ref="example/model@1",
)
OPERATOR = Actor(
    actor_id="runner",
    actor_type=ActorType.SOFTWARE,
    role=ActorRole.OPERATOR,
)
EVALUATOR = Actor(
    actor_id="deterministic-oracle",
    actor_type=ActorType.SOFTWARE,
    role=ActorRole.EVALUATOR,
)
AUTHORITY = Authority(
    authority_type=AuthorityType.DETERMINISTIC_TOOL,
    actor_ref=EVALUATOR.actor_id,
    basis="Exact comparison in the declared test environment",
    independent_of_subject=True,
)
ORACLE = Oracle(
    adapter="exact-match",
    version="1",
    authority_type=AuthorityType.DETERMINISTIC_TOOL,
    declared_scope="Equality of the observed and expected value",
)


def _header(record_id: str, second: int, actor: Actor = MODEL) -> RecordHeader:
    return RecordHeader(
        record_id=record_id,
        created_at=START + timedelta(seconds=second),
        created_by=actor,
    )


def _typed_lifecycle() -> list[JsonObject]:
    return [
        artifact_reference_record(
            _header("artifact-model-assertion", 0),
            uri="artifacts/model-assertion.json",
            media_type="application/json",
            title="Structured model assertion",
            digest=Digest(DigestAlgorithm.SHA256, "a" * 64),
            captured_at=START,
        ),
        hypothesis_record(
            _header("hypothesis-cache", 1),
            statement="A stale cache entry caused the observed result.",
            scope=SCOPE,
            dependency_refs=("artifact-model-assertion",),
            prediction_refs=("prediction-cache-bypass",),
        ),
        status_transition_record(
            _header("transition-cache-testable", 2),
            subject_ref="hypothesis-cache",
            from_status=ClaimStatus.PROPOSED,
            to_status=ClaimStatus.TESTABLE,
            authorized_by=MODEL,
            reason="The hypothesis has a falsifiable prediction.",
        ),
        prediction_record(
            _header("prediction-cache-bypass", 3),
            hypothesis_ref="hypothesis-cache",
            condition=PredictionCondition.HYPOTHESIS_TRUE,
            expected_observation="fresh",
            falsified_when="Bypassing the cache still returns stale.",
            test_ref="test-cache-plan",
            scope=SCOPE,
        ),
        protocol_test_record(
            _header("test-cache-plan", 4),
            question="What is returned when the cache is bypassed?",
            design=TestDesign.CONTROLLED_ABLATION,
            status=TestStatus.PLANNED,
            oracle=ORACLE,
            scope=SCOPE,
            subject_refs=("hypothesis-cache",),
            prediction_refs=("prediction-cache-bypass",),
        ),
        status_transition_record(
            _header("transition-cache-under-test", 5, OPERATOR),
            subject_ref="hypothesis-cache",
            from_status=ClaimStatus.TESTABLE,
            to_status=ClaimStatus.UNDER_TEST,
            authorized_by=OPERATOR,
            reason="The declared controlled test is starting.",
        ),
        protocol_test_record(
            _header("test-cache-run", 6, OPERATOR),
            question="What is returned when the cache is bypassed?",
            design=TestDesign.CONTROLLED_ABLATION,
            status=TestStatus.COMPLETED,
            oracle=ORACLE,
            scope=SCOPE,
            subject_refs=("hypothesis-cache",),
            prediction_refs=("prediction-cache-bypass",),
            plan_ref="test-cache-plan",
            evidence_refs=("evidence-cache-run",),
            cost=TestCost(
                model_tokens=0,
                wall_time_ms=4,
                human_minutes=0.0,
            ),
        ),
        evidence_record(
            _header("evidence-cache-run", 7, EVALUATOR),
            evidence_type=EvidenceType.DETERMINISTIC_TEST,
            relations=(
                EvidenceRelation(
                    subject_ref="hypothesis-cache",
                    relation=EvidenceRelationType.SUPPORTS,
                    public_note="The isolated bypass returned the fresh value.",
                ),
                EvidenceRelation(
                    subject_ref="claim-cache-cause",
                    relation=EvidenceRelationType.SUPPORTS,
                    public_note="The same observation bears on the derived claim.",
                ),
            ),
            authority=AUTHORITY,
            scope=SCOPE,
            test_ref="test-cache-run",
            result={"observed": "fresh", "expected": "fresh"},
        ),
        verdict_record(
            _header("verdict-cache", 8, EVALUATOR),
            subject_ref="hypothesis-cache",
            outcome=VerdictOutcome.SUPPORTED,
            evidence_refs=("evidence-cache-run",),
            authority=AUTHORITY,
            scope=SCOPE,
            public_rationale="The declared intervention produced its prediction.",
        ),
        status_transition_record(
            _header("transition-cache-supported", 9, EVALUATOR),
            subject_ref="hypothesis-cache",
            from_status=ClaimStatus.UNDER_TEST,
            to_status=ClaimStatus.SUPPORTED,
            authorized_by=EVALUATOR,
            reason="The deterministic test supported the scoped hypothesis.",
            evidence_refs=("evidence-cache-run",),
            verdict_ref="verdict-cache",
        ),
        claim_record(
            _header("claim-cache-cause", 10, EVALUATOR),
            proposition=(
                "The stale cache mechanism caused this incident within the "
                "declared environment."
            ),
            claim_type=ClaimType.CAUSAL,
            scope=SCOPE,
            evidence_refs=("evidence-cache-run",),
            dependency_refs=("hypothesis-cache",),
        ),
        status_transition_record(
            _header("transition-claim-testable", 11, EVALUATOR),
            subject_ref="claim-cache-cause",
            from_status=ClaimStatus.PROPOSED,
            to_status=ClaimStatus.TESTABLE,
            authorized_by=EVALUATOR,
            reason="The derived claim has a falsifiable causal statement.",
        ),
        status_transition_record(
            _header("transition-claim-under-test", 12, EVALUATOR),
            subject_ref="claim-cache-cause",
            from_status=ClaimStatus.TESTABLE,
            to_status=ClaimStatus.UNDER_TEST,
            authorized_by=EVALUATOR,
            reason="The recorded test is being applied to the derived claim.",
        ),
        verdict_record(
            _header("verdict-claim-cache", 13, EVALUATOR),
            subject_ref="claim-cache-cause",
            outcome=VerdictOutcome.SUPPORTED,
            evidence_refs=("evidence-cache-run",),
            authority=AUTHORITY,
            scope=SCOPE,
            public_rationale="The intervention supports the derived claim.",
        ),
        status_transition_record(
            _header("transition-claim-supported", 14, EVALUATOR),
            subject_ref="claim-cache-cause",
            from_status=ClaimStatus.UNDER_TEST,
            to_status=ClaimStatus.SUPPORTED,
            authorized_by=EVALUATOR,
            reason="The deterministic test supported the scoped claim.",
            evidence_refs=("evidence-cache-run",),
            verdict_ref="verdict-claim-cache",
        ),
        decision_record(
            _header("decision-close-incident", 15, EVALUATOR),
            question="May the incident be closed under the tested scope?",
            disposition=DecisionDisposition.APPROVED,
            authorized_by=EVALUATOR,
            scope=SCOPE,
            relied_on_claim_refs=("claim-cache-cause",),
            public_rationale="The scoped causal hypothesis is supported.",
        ),
    ]


def test_typed_constructors_build_a_coherent_complete_lifecycle() -> None:
    ledger = Ledger(_typed_lifecycle())

    assert ledger.snapshot.record_count == 16
    assert ledger.snapshot.current_states == {
        "hypothesis-cache": ClaimStatus.SUPPORTED,
        "claim-cache-cause": ClaimStatus.SUPPORTED,
    }
    assert tuple(record["kind"] for record in ledger.records) == (
        "artifact_reference",
        "hypothesis",
        "status_transition",
        "prediction",
        "test",
        "status_transition",
        "test",
        "evidence",
        "verdict",
        "status_transition",
        "claim",
        "status_transition",
        "status_transition",
        "verdict",
        "status_transition",
        "decision",
    )


def test_claim_constructor_covers_claim_specific_fields() -> None:
    record = claim_record(
        _header("claim-release-ready", 0),
        proposition="The release candidate passed its declared quality gates.",
        claim_type=ClaimType.FACTUAL,
        scope=SCOPE,
        evidence_refs=("evidence-ci",),
        contradicting_evidence_refs=("evidence-open-defect",),
        dependency_refs=("artifact-ci-log",),
        transition_refs=("transition-release-supported",),
        invalidation_conditions=("A required CI job is rerun and fails.",),
    )

    assert record["kind"] == "claim"
    assert record["claim_type"] == "factual"
    assert record["status"] == "proposed"
    assert record["invalidation_conditions"] == [
        "A required CI job is rerun and fails."
    ]


def test_completed_test_requires_evidence() -> None:
    with pytest.raises(RecordConstructionError):
        protocol_test_record(
            _header("test-incomplete", 0),
            question="Did the check pass?",
            design=TestDesign.DETERMINISTIC_CHECK,
            status=TestStatus.COMPLETED,
            oracle=ORACLE,
            scope=SCOPE,
        )


def _check_test(
    record_id: str,
    second: int,
    status: TestStatus,
    *,
    plan_ref: str | None = None,
    evidence_refs: Sequence[str] = (),
) -> JsonObject:
    return protocol_test_record(
        _header(record_id, second, OPERATOR),
        question="Does the declared check pass?",
        design=TestDesign.DETERMINISTIC_CHECK,
        status=status,
        oracle=ORACLE,
        scope=SCOPE,
        subject_refs=("claim-check",),
        prediction_refs=("prediction-check",),
        plan_ref=plan_ref,
        evidence_refs=evidence_refs,
    )


def test_completed_test_without_evidence_names_the_missing_field() -> None:
    planned = _check_test("test-check-plan", 4, TestStatus.PLANNED)
    completed = dict(planned, id="test-check-run", status="completed")

    assert ProtocolValidator().errors(completed) == [
        "$: 'evidence_refs' is a required property"
    ]
    with pytest.raises(RecordConstructionError) as raised:
        _check_test("test-check-run", 6, TestStatus.COMPLETED)
    assert str(raised.value) == "$: 'evidence_refs' is a required property"


def test_single_string_reference_list_is_rejected() -> None:
    with pytest.raises(
        RecordConstructionError,
        match="evidence_refs must be a sequence of strings, not a single string",
    ):
        _check_test(
            "test-check-run",
            6,
            TestStatus.COMPLETED,
            plan_ref="test-check-plan",
            evidence_refs="ev-1",
        )


def test_completed_test_and_its_evidence_enter_the_ledger_in_one_batch() -> None:
    ledger = Ledger(
        [
            claim_record(
                _header("claim-check", 0),
                proposition="The declared check passes.",
                claim_type=ClaimType.FACTUAL,
                scope=SCOPE,
            ),
            hypothesis_record(
                _header("hypothesis-check", 1),
                statement="The change preserves the checked behavior.",
                scope=SCOPE,
            ),
            status_transition_record(
                _header("transition-check-testable", 2),
                subject_ref="claim-check",
                from_status=ClaimStatus.PROPOSED,
                to_status=ClaimStatus.TESTABLE,
                authorized_by=MODEL,
                reason="The claim names a deterministic check.",
            ),
            prediction_record(
                _header("prediction-check", 3),
                hypothesis_ref="hypothesis-check",
                condition=PredictionCondition.HYPOTHESIS_TRUE,
                expected_observation="The check exits with status zero.",
                scope=SCOPE,
            ),
            _check_test("test-check-plan", 4, TestStatus.PLANNED),
            status_transition_record(
                _header("transition-check-under-test", 5, OPERATOR),
                subject_ref="claim-check",
                from_status=ClaimStatus.TESTABLE,
                to_status=ClaimStatus.UNDER_TEST,
                authorized_by=OPERATOR,
                reason="The planned check is running.",
            ),
        ]
    )
    run = _check_test(
        "test-check-run",
        6,
        TestStatus.COMPLETED,
        plan_ref="test-check-plan",
        evidence_refs=("evidence-check",),
    )
    evidence = evidence_record(
        _header("evidence-check", 7, EVALUATOR),
        evidence_type=EvidenceType.DETERMINISTIC_TEST,
        relations=(
            EvidenceRelation(
                subject_ref="claim-check",
                relation=EvidenceRelationType.SUPPORTS,
            ),
        ),
        authority=AUTHORITY,
        scope=SCOPE,
        test_ref="test-check-run",
        result={"exit_code": 0},
    )

    # Each record cites the other, so neither can enter the ledger alone.
    for record in (run, evidence):
        with pytest.raises(BundleIntegrityError) as raised:
            ledger.append(record)
        assert [issue.code for issue in raised.value.issues] == [
            IntegrityCode.UNRESOLVED_REFERENCE
        ]

    ledger.extend((run, evidence))
    ledger.extend(
        (
            verdict_record(
                _header("verdict-check", 8, EVALUATOR),
                subject_ref="claim-check",
                outcome=VerdictOutcome.SUPPORTED,
                evidence_refs=("evidence-check",),
                authority=AUTHORITY,
                scope=SCOPE,
                public_rationale="The declared check passed.",
            ),
            status_transition_record(
                _header("transition-check-supported", 9, EVALUATOR),
                subject_ref="claim-check",
                from_status=ClaimStatus.UNDER_TEST,
                to_status=ClaimStatus.SUPPORTED,
                authorized_by=EVALUATOR,
                reason="The deterministic check supported the scoped claim.",
                evidence_refs=("evidence-check",),
                verdict_ref="verdict-check",
            ),
        )
    )

    assert ledger.snapshot.current_states["claim-check"] is ClaimStatus.SUPPORTED


def test_corroborated_verdict_requires_policy_reference() -> None:
    with pytest.raises(RecordConstructionError):
        verdict_record(
            _header("verdict-corroborated", 0, EVALUATOR),
            subject_ref="claim-release",
            outcome=VerdictOutcome.CORROBORATED_WITHIN_SCOPE,
            evidence_refs=("evidence-review",),
            authority=AUTHORITY,
            scope=SCOPE,
            public_rationale="Two independent sources agree.",
        )


def test_model_cannot_construct_evidence_backed_promotion() -> None:
    model_evaluator = Actor(
        actor_id="model-evaluator",
        actor_type=ActorType.MODEL,
        role=ActorRole.EVALUATOR,
    )
    with pytest.raises(
        RecordConstructionError,
        match="model actor cannot authorize",
    ):
        status_transition_record(
            _header("transition-self-promote", 0),
            subject_ref="hypothesis-cache",
            from_status=ClaimStatus.UNDER_TEST,
            to_status=ClaimStatus.SUPPORTED,
            authorized_by=model_evaluator,
            reason="The model judged its own assertion correct.",
            evidence_refs=("evidence-model-output",),
            verdict_ref="verdict-model-output",
        )


def test_actor_normalizes_decoded_type_and_role() -> None:
    decoded: dict[str, Any] = json.loads('{"actor_type": "model", "role": "evaluator"}')
    actor = Actor("model-evaluator", decoded["actor_type"], decoded["role"])

    assert actor.actor_type is ActorType.MODEL
    assert actor.role is ActorRole.EVALUATOR
    with pytest.raises(
        RecordConstructionError,
        match="model actor cannot authorize",
    ):
        status_transition_record(
            _header("transition-decoded-model", 0, actor),
            subject_ref="hypothesis-cache",
            from_status=ClaimStatus.UNDER_TEST,
            to_status=ClaimStatus.SUPPORTED,
            authorized_by=actor,
            reason="A decoded model actor judged its own assertion correct.",
            evidence_refs=("evidence-model-output",),
            verdict_ref="verdict-model-output",
        )


def test_actor_rejects_unknown_type() -> None:
    decoded: dict[str, Any] = json.loads('{"actor_type": "robot", "role": "evaluator"}')

    with pytest.raises(RecordConstructionError, match="'robot' is not a valid"):
        Actor("robot-1", decoded["actor_type"], decoded["role"])


def test_naive_record_timestamp_is_rejected() -> None:
    with pytest.raises(RecordConstructionError, match="timezone-aware"):
        RecordHeader(
            record_id="claim-naive-time",
            created_at=datetime(2026, 7, 23, 12),
            created_by=MODEL,
        )


def test_evidence_result_is_defensively_copied_and_strict_json() -> None:
    result: JsonObject = {"observations": [{"value": "fresh"}]}
    record = evidence_record(
        _header("evidence-copy", 0, EVALUATOR),
        evidence_type=EvidenceType.OBSERVATION,
        relations=(
            EvidenceRelation(
                subject_ref="hypothesis-cache",
                relation=EvidenceRelationType.CONTEXTUALIZES,
            ),
        ),
        authority=AUTHORITY,
        scope=SCOPE,
        result=result,
    )
    observations = cast(list[JsonValue], result["observations"])
    observation = cast(JsonObject, observations[0])
    observation["value"] = "mutated"

    captured = cast(
        list[JsonObject], cast(JsonObject, record["result"])["observations"]
    )
    assert captured[0]["value"] == "fresh"

    with pytest.raises(RecordConstructionError, match="strict JSON"):
        evidence_record(
            _header("evidence-nan", 1, EVALUATOR),
            evidence_type=EvidenceType.CALCULATION,
            relations=(
                EvidenceRelation(
                    subject_ref="hypothesis-cache",
                    relation=EvidenceRelationType.CONTEXTUALIZES,
                ),
            ),
            authority=AUTHORITY,
            scope=SCOPE,
            result={"score": float("nan")},
        )
