# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Protocol-instrumented scripted run for causal-diagnosis case 001."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from experiments.diagnostician import (
    DiagnosisRequest,
    DiagnosisResult,
    Diagnostician,
    DiagnosticianContractValidator,
    DiagnosticianIdentity,
    DiagnosticPrediction,
    DiagnosticTestOption,
)
from itself import (
    ActorRole,
    ActorType,
    BundleIntegrityError,
    ClaimStatus,
    IntegrityCode,
    JsonlLedgerStore,
    JsonObject,
    Ledger,
    StrPath,
)

from .cache_key_diagnostician import (
    CASE_ID,
    CacheKeyFixtureDiagnostician,
    build_cache_key_request,
)
from .cache_key_omission import (
    BehavioralOracle,
    CaseEnvironment,
    CausalOracle,
    Intervention,
    Mechanism,
    Observation,
    Patch,
    Revision,
)

PROTOCOL_VERSION: Final = "0.1.0-alpha.3"
CASE_SCOPE: Final = "Case 001, cache-key omission, replaceable diagnostician run"
DIAGNOSTIC_ARTIFACT_ID: Final = "artifact-diagnostician-output"
TEST_PLAN_ID: Final = "test-selected-intervention-plan"
TEST_RUN_ID: Final = "test-selected-intervention-run-1"
TEST_EVIDENCE_ID: Final = "evidence-selected-intervention-run-1"
_RUN_START: Final = datetime(2026, 7, 22, 13, 0, tzinfo=UTC)

HYPOTHESIS_IDS: Final[Mapping[Mechanism, str]] = {
    Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION: "hypothesis-cache-key",
    Mechanism.WORKER_READS_STALE_SOURCE: "hypothesis-worker-source",
    Mechanism.VERIFIER_READS_STALE_ARTIFACT: "hypothesis-verifier-artifact",
}


@dataclass(frozen=True, slots=True)
class ScriptedRunMetrics:
    """Non-composite measurements from the deterministic reference run."""

    ground_truth_identified: bool
    ground_truth_retained: bool
    evidence_identifies_ground_truth: bool
    false_promotion_attempts: int
    false_promotions_accepted: int
    predictions_evaluated: int
    predictions_correct: int
    tests_executed: int
    discriminating_tests: int
    behavioral_recovery: bool
    negative_control_behavioral_recovery: bool
    negative_control_addresses_cause: bool
    ledger_record_count: int

    def to_json_object(self) -> JsonObject:
        """Return a JSON-compatible scorecard without a composite score."""

        return {
            "ground_truth_identified": self.ground_truth_identified,
            "ground_truth_retained": self.ground_truth_retained,
            "evidence_identifies_ground_truth": self.evidence_identifies_ground_truth,
            "false_promotion_attempts": self.false_promotion_attempts,
            "false_promotions_accepted": self.false_promotions_accepted,
            "predictions_evaluated": self.predictions_evaluated,
            "predictions_correct": self.predictions_correct,
            "tests_executed": self.tests_executed,
            "discriminating_tests": self.discriminating_tests,
            "behavioral_recovery": self.behavioral_recovery,
            "negative_control_behavioral_recovery": (
                self.negative_control_behavioral_recovery
            ),
            "negative_control_addresses_cause": (self.negative_control_addresses_cause),
            "ledger_record_count": self.ledger_record_count,
        }


@dataclass(frozen=True, slots=True)
class ScriptedRunResult:
    """Artifacts and measurements produced by the deterministic reference run."""

    request: DiagnosisRequest
    diagnosis: DiagnosisResult
    ledger: Ledger
    selected_hypothesis: Mechanism
    intervention: Intervention
    observation: Observation
    metrics: ScriptedRunMetrics


@dataclass(frozen=True, slots=True)
class PreparedStructuredCase:
    """Validated structured assertion projected to the planned-test boundary."""

    request: DiagnosisRequest
    diagnosis: DiagnosisResult
    ledger: Ledger
    retained_mechanisms: tuple[Mechanism, ...]
    selected_hypothesis: Mechanism
    intervention: Intervention
    test_option: DiagnosticTestOption
    selected_predictions: tuple[DiagnosticPrediction, ...]


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
    actor: JsonObject = {
        "id": actor_id,
        "actor_type": actor_type.value,
        "role": role.value,
    }
    if implementation_ref is not None:
        actor["implementation_ref"] = implementation_ref
    return actor


def _diagnostician_actor(identity: DiagnosticianIdentity) -> JsonObject:
    return _actor(
        identity.actor_id,
        identity.actor_type,
        ActorRole.PROPOSER,
        implementation_ref=f"{identity.adapter_id}@{identity.adapter_version}",
    )


def _scope() -> JsonObject:
    return {
        "description": CASE_SCOPE,
        "dimensions": {
            "case_id": CASE_ID,
            "environment_version": "1",
            "source_revision": Revision.B.value,
        },
    }


def _artifact_record(diagnosis: DiagnosisResult, position: int) -> JsonObject:
    artifact = diagnosis.raw_output_artifact
    return {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "artifact_reference",
        "id": DIAGNOSTIC_ARTIFACT_ID,
        "created_at": _created_at(position),
        "created_by": _diagnostician_actor(diagnosis.diagnostician),
        "uri": artifact.uri,
        "media_type": artifact.media_type,
        "title": "Captured diagnostician output",
        "digest": {
            "algorithm": "sha256",
            "value": artifact.sha256,
        },
        "captured_at": artifact.captured_at,
    }


def _hypothesis_record(
    mechanism: Mechanism,
    retained_mechanisms: Sequence[Mechanism],
    diagnosis: DiagnosisResult,
    request: DiagnosisRequest,
    position: int,
) -> JsonObject:
    option = next(
        option for option in request.hypothesis_options if option.id == mechanism.value
    )
    return {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "hypothesis",
        "id": HYPOTHESIS_IDS[mechanism],
        "created_at": _created_at(position),
        "created_by": _diagnostician_actor(diagnosis.diagnostician),
        "statement": option.statement,
        "status": ClaimStatus.PROPOSED.value,
        "scope": _scope(),
        "alternative_hypothesis_refs": [
            HYPOTHESIS_IDS[other]
            for other in retained_mechanisms
            if other is not mechanism
        ],
        "dependency_refs": [DIAGNOSTIC_ARTIFACT_ID],
    }


def _baseline_evidence(
    observations: Sequence[Observation],
    retained_mechanisms: Sequence[Mechanism],
    position: int,
) -> JsonObject:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "evidence",
        "id": "evidence-baseline-failures",
        "created_at": _created_at(position),
        "created_by": _actor(
            "case-001-environment",
            ActorType.SOFTWARE,
            ActorRole.EVALUATOR,
        ),
        "evidence_type": "observation",
        "relations": [
            {
                "subject_ref": HYPOTHESIS_IDS[mechanism],
                "relation": "contextualizes",
                "public_note": "The repeated failure is compatible with this hypothesis.",
            }
            for mechanism in retained_mechanisms
        ],
        "authority": {
            "authority_type": "deterministic_tool",
            "actor_ref": "case-001-environment",
            "basis": "Six deterministic unchanged replays from reset baseline state",
        },
        "scope": _scope(),
        "result": {
            "observations": [
                observation.to_json_object() for observation in observations
            ]
        },
    }


def _transition_record(
    *,
    transition_id: str,
    position: int,
    subject_ref: str,
    from_status: ClaimStatus,
    to_status: ClaimStatus,
    actor_id: str,
    actor_type: ActorType,
    actor_role: ActorRole,
    reason: str,
    evidence_refs: Sequence[str] = (),
    verdict_ref: str | None = None,
) -> JsonObject:
    record: JsonObject = {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "status_transition",
        "id": transition_id,
        "created_at": _created_at(position),
        "created_by": _actor(actor_id, actor_type, actor_role),
        "subject_ref": subject_ref,
        "from_status": from_status.value,
        "to_status": to_status.value,
        "authorized_by": _actor(actor_id, actor_type, actor_role),
        "reason": reason,
    }
    if evidence_refs:
        record["evidence_refs"] = list(evidence_refs)
    if verdict_ref is not None:
        record["verdict_ref"] = verdict_ref
    return record


def _prediction_id(prediction: DiagnosticPrediction) -> str:
    return f"prediction-{prediction.hypothesis_id}-{prediction.test_id}"


def _prediction_record(
    prediction: DiagnosticPrediction,
    diagnosis: DiagnosisResult,
    position: int,
) -> JsonObject:
    mechanism = Mechanism(prediction.hypothesis_id)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "prediction",
        "id": _prediction_id(prediction),
        "created_at": _created_at(position),
        "created_by": _diagnostician_actor(diagnosis.diagnostician),
        "hypothesis_ref": HYPOTHESIS_IDS[mechanism],
        "condition": "hypothesis_true",
        "expected_observation": prediction.expected_observation,
        "falsified_when": prediction.falsified_when,
        "scope": _scope(),
    }


def _test_design(intervention: Intervention) -> str:
    if intervention is Intervention.UNCHANGED_REPLAY:
        return "replay"
    return "controlled_ablation"


def _planned_test_record(
    *,
    test_option: DiagnosticTestOption,
    intervention: Intervention,
    retained_mechanisms: Sequence[Mechanism],
    selected_predictions: Sequence[DiagnosticPrediction],
    diagnosis: DiagnosisResult,
    position: int,
) -> JsonObject:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "test",
        "id": TEST_PLAN_ID,
        "created_at": _created_at(position),
        "created_by": _diagnostician_actor(diagnosis.diagnostician),
        "question": test_option.question,
        "design": _test_design(intervention),
        "status": "planned",
        "subject_refs": [
            HYPOTHESIS_IDS[mechanism] for mechanism in retained_mechanisms
        ],
        "prediction_refs": [
            _prediction_id(prediction) for prediction in selected_predictions
        ],
        "oracle": {
            "adapter": "case-001-behavioral-oracle",
            "version": "1",
            "authority_type": "deterministic_tool",
            "declared_scope": "Equality of expected and observed report revisions",
        },
        "scope": _scope(),
    }


def _premature_promotion_record(
    selected_hypothesis: Mechanism,
    position: int,
) -> JsonObject:
    return _transition_record(
        transition_id="transition-premature-support-attempt",
        position=position,
        subject_ref=HYPOTHESIS_IDS[selected_hypothesis],
        from_status=ClaimStatus.UNDER_TEST,
        to_status=ClaimStatus.SUPPORTED,
        actor_id="case-001-promotion-policy",
        actor_type=ActorType.SOFTWARE,
        actor_role=ActorRole.EVALUATOR,
        evidence_refs=(TEST_EVIDENCE_ID,),
        verdict_ref=f"verdict-{selected_hypothesis.value}",
        reason="The explanation appears plausible before any test result exists.",
    )


def _completed_test_record(
    *,
    test_option: DiagnosticTestOption,
    intervention: Intervention,
    retained_mechanisms: Sequence[Mechanism],
    selected_predictions: Sequence[DiagnosticPrediction],
    diagnosis: DiagnosisResult,
    position: int,
) -> JsonObject:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "test",
        "id": TEST_RUN_ID,
        "created_at": _created_at(position),
        "created_by": _actor(
            "case-001-runner",
            ActorType.SOFTWARE,
            ActorRole.OPERATOR,
        ),
        "question": test_option.question,
        "design": _test_design(intervention),
        "status": "completed",
        "plan_ref": TEST_PLAN_ID,
        "subject_refs": [
            HYPOTHESIS_IDS[mechanism] for mechanism in retained_mechanisms
        ],
        "prediction_refs": [
            _prediction_id(prediction) for prediction in selected_predictions
        ],
        "oracle": {
            "adapter": "case-001-behavioral-oracle",
            "version": "1",
            "authority_type": "deterministic_tool",
            "declared_scope": "Equality of expected and observed report revisions",
        },
        "scope": _scope(),
        "evidence_refs": [TEST_EVIDENCE_ID],
        "cost": {
            "model_tokens": diagnosis.usage.model_tokens,
            "wall_time_ms": diagnosis.usage.wall_time_ms,
            "human_minutes": diagnosis.usage.human_minutes,
        },
    }


def _evidence_relation(
    mechanism: Mechanism,
    prediction: DiagnosticPrediction,
    observation: Observation,
    matching_hypotheses: frozenset[Mechanism],
) -> tuple[str, ...]:
    declared_match = (
        prediction.expected_observation == observation.observed_revision.value
    )
    if mechanism not in matching_hypotheses:
        return ("contradicts",)
    if not declared_match:
        return ("supports", "contradicts")
    if len(matching_hypotheses) == 1:
        return ("supports",)
    return ("contextualizes",)


def _test_evidence_record(
    *,
    observation: Observation,
    predictions: Mapping[Mechanism, DiagnosticPrediction],
    matching_hypotheses: frozenset[Mechanism],
    position: int,
) -> JsonObject:
    result = observation.to_json_object()
    result["matching_hypotheses"] = [
        mechanism.value
        for mechanism in sorted(matching_hypotheses, key=lambda value: value.value)
    ]
    return {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "evidence",
        "id": TEST_EVIDENCE_ID,
        "created_at": _created_at(position),
        "created_by": _actor(
            "case-001-environment",
            ActorType.SOFTWARE,
            ActorRole.EVALUATOR,
        ),
        "evidence_type": "deterministic_test",
        "relations": [
            {
                "subject_ref": HYPOTHESIS_IDS[mechanism],
                "relation": relation,
                "public_note": (
                    f"Observed revision {observation.observed_revision.value}; "
                    f"predeclared prediction was {prediction.expected_observation}."
                ),
            }
            for mechanism, prediction in predictions.items()
            for relation in _evidence_relation(
                mechanism,
                prediction,
                observation,
                matching_hypotheses,
            )
        ],
        "authority": {
            "authority_type": "deterministic_tool",
            "actor_ref": "case-001-environment",
            "basis": (
                f"Reset-state controlled intervention {observation.intervention.value}"
            ),
            "independent_of_subject": True,
        },
        "scope": _scope(),
        "test_ref": TEST_RUN_ID,
        "result": result,
    }


def _outcome(
    mechanism: Mechanism,
    prediction: DiagnosticPrediction,
    observation: Observation,
    matching_hypotheses: frozenset[Mechanism],
) -> ClaimStatus:
    if mechanism not in matching_hypotheses:
        return ClaimStatus.REFUTED
    if prediction.expected_observation != observation.observed_revision.value:
        return ClaimStatus.MIXED_EVIDENCE
    if len(matching_hypotheses) == 1:
        return ClaimStatus.SUPPORTED
    return ClaimStatus.INCONCLUSIVE


def _verdict_record(
    mechanism: Mechanism,
    outcome: ClaimStatus,
    position: int,
) -> JsonObject:
    rationale = {
        ClaimStatus.SUPPORTED: (
            "The result matches the predeclared prediction and uniquely isolates "
            "this hypothesis."
        ),
        ClaimStatus.REFUTED: (
            "The result is incompatible with this hypothesis under the controlled "
            "case oracle."
        ),
        ClaimStatus.INCONCLUSIVE: (
            "The result remains compatible with this and at least one alternative "
            "hypothesis."
        ),
        ClaimStatus.MIXED_EVIDENCE: (
            "The hypothesis remains compatible with the environment result, but its "
            "predeclared prediction was incorrect."
        ),
    }.get(outcome)
    if rationale is None:
        raise ValueError(f"unsupported scripted verdict outcome {outcome.value}")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "verdict",
        "id": f"verdict-{mechanism.value}",
        "created_at": _created_at(position),
        "created_by": _actor(
            "case-001-causal-oracle",
            ActorType.SOFTWARE,
            ActorRole.EVALUATOR,
        ),
        "subject_ref": HYPOTHESIS_IDS[mechanism],
        "outcome": outcome.value,
        "evidence_refs": [TEST_EVIDENCE_ID],
        "authority": {
            "authority_type": "domain_evaluator",
            "actor_ref": "case-001-causal-oracle",
            "basis": "Preregistered intervention matrix for case 001",
            "independent_of_subject": True,
        },
        "scope": _scope(),
        "public_rationale": rationale,
    }


def prepare_structured_case(
    request: DiagnosisRequest,
    diagnosis: DiagnosisResult,
    baseline_observations: Sequence[Observation],
) -> PreparedStructuredCase:
    """Record a validated assertion through predictions and a planned test only."""

    DiagnosticianContractValidator().validate_result(request, diagnosis)
    assertion = diagnosis.assertion
    retained_mechanisms = tuple(
        Mechanism(hypothesis_id) for hypothesis_id in assertion.retained_hypothesis_ids
    )
    selected_hypothesis = Mechanism(assertion.primary_hypothesis_id)
    intervention = Intervention(assertion.selected_test_id)
    test_option = next(
        option for option in request.test_options if option.id == intervention.value
    )
    selected_predictions = tuple(
        prediction
        for prediction in assertion.predictions
        if prediction.test_id == intervention.value
    )

    initial_records = (
        _artifact_record(diagnosis, 0),
        *(
            _hypothesis_record(
                mechanism,
                retained_mechanisms,
                diagnosis,
                request,
                1 + position,
            )
            for position, mechanism in enumerate(retained_mechanisms)
        ),
    )
    ledger = Ledger(initial_records)
    ledger.append(
        _baseline_evidence(
            baseline_observations,
            retained_mechanisms,
            len(ledger),
        )
    )

    start = len(ledger)
    ledger.extend(
        _transition_record(
            transition_id=f"transition-{mechanism.value}-testable",
            position=start + position,
            subject_ref=HYPOTHESIS_IDS[mechanism],
            from_status=ClaimStatus.PROPOSED,
            to_status=ClaimStatus.TESTABLE,
            actor_id=diagnosis.diagnostician.actor_id,
            actor_type=diagnosis.diagnostician.actor_type,
            actor_role=ActorRole.PROPOSER,
            reason="The retained hypothesis has a falsifiable selected-test prediction.",
        )
        for position, mechanism in enumerate(retained_mechanisms)
    )

    start = len(ledger)
    ledger.extend(
        _prediction_record(prediction, diagnosis, start + position)
        for position, prediction in enumerate(assertion.predictions)
    )
    ledger.append(
        _planned_test_record(
            test_option=test_option,
            intervention=intervention,
            retained_mechanisms=retained_mechanisms,
            selected_predictions=selected_predictions,
            diagnosis=diagnosis,
            position=len(ledger),
        )
    )
    return PreparedStructuredCase(
        request=request,
        diagnosis=diagnosis,
        ledger=ledger,
        retained_mechanisms=retained_mechanisms,
        selected_hypothesis=selected_hypothesis,
        intervention=intervention,
        test_option=test_option,
        selected_predictions=selected_predictions,
    )


def complete_evidence_enforced_case(
    prepared: PreparedStructuredCase,
    *,
    ground_truth: Mechanism = Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION,
) -> ScriptedRunResult:
    """Fork a prepared assertion and complete its external evidence path."""

    environment = CaseEnvironment(ground_truth)
    causal_oracle = CausalOracle(ground_truth)
    request = prepared.request
    diagnosis = prepared.diagnosis
    retained_mechanisms = prepared.retained_mechanisms
    selected_hypothesis = prepared.selected_hypothesis
    intervention = prepared.intervention
    test_option = prepared.test_option
    selected_predictions = prepared.selected_predictions
    predictions_by_mechanism = {
        Mechanism(prediction.hypothesis_id): prediction
        for prediction in selected_predictions
    }
    ledger = Ledger(prepared.ledger.records)

    start = len(ledger)
    ledger.extend(
        _transition_record(
            transition_id=f"transition-{mechanism.value}-under-test",
            position=start + position,
            subject_ref=HYPOTHESIS_IDS[mechanism],
            from_status=ClaimStatus.TESTABLE,
            to_status=ClaimStatus.UNDER_TEST,
            actor_id="case-001-runner",
            actor_type=ActorType.SOFTWARE,
            actor_role=ActorRole.OPERATOR,
            reason="The validated, predeclared intervention is starting.",
        )
        for position, mechanism in enumerate(retained_mechanisms)
    )

    false_promotion_attempts = 1
    false_promotions_accepted = 0
    record_count_before_rejection = len(ledger)
    try:
        ledger.append(_premature_promotion_record(selected_hypothesis, len(ledger)))
    except BundleIntegrityError as error:
        expected_rejection = any(
            issue.code is IntegrityCode.UNRESOLVED_REFERENCE
            and issue.reference == TEST_EVIDENCE_ID
            for issue in error.issues
        )
        if not expected_rejection:
            raise
    else:
        false_promotions_accepted = 1
    if len(ledger) != record_count_before_rejection:
        raise RuntimeError("rejected promotion mutated the ledger")

    observation = environment.execute(intervention)
    results = {Intervention.UNCHANGED_REPLAY: Revision.A}
    results[intervention] = observation.observed_revision
    matching_hypotheses = causal_oracle.matching_hypotheses(results)
    ledger.extend(
        (
            _completed_test_record(
                test_option=test_option,
                intervention=intervention,
                retained_mechanisms=retained_mechanisms,
                selected_predictions=selected_predictions,
                diagnosis=diagnosis,
                position=len(ledger),
            ),
            _test_evidence_record(
                observation=observation,
                predictions=predictions_by_mechanism,
                matching_hypotheses=matching_hypotheses,
                position=len(ledger) + 1,
            ),
        )
    )

    outcomes = {
        mechanism: _outcome(
            mechanism,
            predictions_by_mechanism[mechanism],
            observation,
            matching_hypotheses,
        )
        for mechanism in retained_mechanisms
    }
    start = len(ledger)
    ledger.extend(
        _verdict_record(mechanism, outcomes[mechanism], start + position)
        for position, mechanism in enumerate(retained_mechanisms)
    )
    start = len(ledger)
    ledger.extend(
        _transition_record(
            transition_id=f"transition-{mechanism.value}-{outcomes[mechanism].value}",
            position=start + position,
            subject_ref=HYPOTHESIS_IDS[mechanism],
            from_status=ClaimStatus.UNDER_TEST,
            to_status=outcomes[mechanism],
            actor_id="case-001-promotion-policy",
            actor_type=ActorType.SOFTWARE,
            actor_role=ActorRole.EVALUATOR,
            evidence_refs=(TEST_EVIDENCE_ID,),
            verdict_ref=f"verdict-{mechanism.value}",
            reason="The result was evaluated against preregistered predictions.",
        )
        for position, mechanism in enumerate(retained_mechanisms)
    )

    negative_control = environment.apply_patch(Patch.FORCE_EXPECTED_OUTPUT)
    predictions_correct = sum(
        prediction.expected_observation
        == causal_oracle.predict(mechanism, intervention).value
        for mechanism, prediction in predictions_by_mechanism.items()
    )
    metrics = ScriptedRunMetrics(
        ground_truth_identified=selected_hypothesis is ground_truth,
        ground_truth_retained=ground_truth in retained_mechanisms,
        evidence_identifies_ground_truth=causal_oracle.identifies_ground_truth(results),
        false_promotion_attempts=false_promotion_attempts,
        false_promotions_accepted=false_promotions_accepted,
        predictions_evaluated=len(predictions_by_mechanism),
        predictions_correct=predictions_correct,
        tests_executed=1,
        discriminating_tests=int(
            causal_oracle.discriminates(
                intervention,
                selected_hypothesis,
                tuple(
                    mechanism
                    for mechanism in retained_mechanisms
                    if mechanism is not selected_hypothesis
                ),
            )
        ),
        behavioral_recovery=BehavioralOracle.passes(observation),
        negative_control_behavioral_recovery=BehavioralOracle.passes(negative_control),
        negative_control_addresses_cause=causal_oracle.patch_addresses_cause(
            Patch.FORCE_EXPECTED_OUTPUT
        ),
        ledger_record_count=len(ledger),
    )
    return ScriptedRunResult(
        request=request,
        diagnosis=diagnosis,
        ledger=ledger,
        selected_hypothesis=selected_hypothesis,
        intervention=intervention,
        observation=observation,
        metrics=metrics,
    )


def run_scripted_case(
    diagnostician: Diagnostician | None = None,
) -> ScriptedRunResult:
    """Execute one validated diagnostician assertion in the deterministic case."""

    ground_truth = Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION
    environment = CaseEnvironment(ground_truth)
    context = environment.diagnostic_context()
    request = build_cache_key_request(context)
    diagnosis = DiagnosticianContractValidator().invoke(
        diagnostician or CacheKeyFixtureDiagnostician(),
        request,
    )
    prepared = prepare_structured_case(
        request,
        diagnosis,
        context.baseline_observations,
    )
    return complete_evidence_enforced_case(prepared, ground_truth=ground_truth)


def write_scripted_case(
    path: StrPath,
    diagnostician: Diagnostician | None = None,
) -> ScriptedRunResult:
    """Run the case and atomically write its complete JSON Lines ledger."""

    result = run_scripted_case(diagnostician)
    JsonlLedgerStore(path).extend(result.ledger.records)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Write one deterministic case ledger and print its JSON scorecard."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    result = write_scripted_case(args.output)
    print(json.dumps(result.metrics.to_json_object(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
