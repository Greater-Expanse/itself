# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from experiments.cases.cache_key_diagnostician import (
    CacheKeyFixtureDiagnostician,
    build_cache_key_request,
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
    TEST_PLAN_ID,
    TEST_RUN_ID,
    complete_evidence_enforced_case,
    main,
    prepare_structured_case,
    run_scripted_case,
    write_scripted_case,
)
from itself import ClaimStatus, JsonlLedgerStore, JsonObject


def _record_by_id(records: tuple[JsonObject, ...], record_id: str) -> JsonObject:
    return next(record for record in records if record.get("id") == record_id)


def test_scripted_run_reaches_evidence_backed_states() -> None:
    result = run_scripted_case()
    states = result.ledger.snapshot.current_states

    assert (
        states[HYPOTHESIS_IDS[Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION]]
        is ClaimStatus.SUPPORTED
    )
    assert (
        states[HYPOTHESIS_IDS[Mechanism.WORKER_READS_STALE_SOURCE]]
        is ClaimStatus.REFUTED
    )
    assert (
        states[HYPOTHESIS_IDS[Mechanism.VERIFIER_READS_STALE_ARTIFACT]]
        is ClaimStatus.REFUTED
    )


def test_prepared_structured_case_stops_at_shared_planned_test_boundary() -> None:
    environment = CaseEnvironment(Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION)
    context = environment.diagnostic_context()
    request = build_cache_key_request(context)
    diagnosis = CacheKeyFixtureDiagnostician().diagnose(request)

    prepared = prepare_structured_case(
        request,
        diagnosis,
        context.baseline_observations,
    )
    completed = complete_evidence_enforced_case(prepared)

    assert len(prepared.ledger) == 12
    assert set(prepared.ledger.snapshot.current_states.values()) == {
        ClaimStatus.TESTABLE
    }
    assert TEST_PLAN_ID in prepared.ledger.snapshot.record_ids
    assert TEST_RUN_ID not in prepared.ledger.snapshot.record_ids
    assert TEST_EVIDENCE_ID not in prepared.ledger.snapshot.record_ids
    assert prepared.ledger.records == completed.ledger.records[: len(prepared.ledger)]
    assert len(prepared.ledger) == 12
    assert len(completed.ledger) == 23


def test_scripted_run_rejects_promotion_before_evidence() -> None:
    result = run_scripted_case()
    record_ids = result.ledger.snapshot.record_ids

    assert result.metrics.false_promotion_attempts == 1
    assert result.metrics.false_promotions_accepted == 0
    assert "transition-premature-support-attempt" not in record_ids


def test_completed_test_preserves_plan_lineage() -> None:
    records = run_scripted_case().ledger.records
    execution = _record_by_id(records, TEST_RUN_ID)
    evidence = _record_by_id(records, TEST_EVIDENCE_ID)

    assert execution["plan_ref"] == TEST_PLAN_ID
    assert execution["evidence_refs"] == [TEST_EVIDENCE_ID]
    assert evidence["test_ref"] == TEST_RUN_ID


def test_evidence_precedes_every_evidence_backed_transition() -> None:
    records = run_scripted_case().ledger.records
    positions = {
        cast(str, record["id"]): position for position, record in enumerate(records)
    }
    evidence_position = positions[TEST_EVIDENCE_ID]

    for record in records:
        if record.get("kind") != "status_transition" or not record.get("evidence_refs"):
            continue
        assert evidence_position < positions[cast(str, record["id"])]


def test_scorecard_separates_behavior_from_cause() -> None:
    metrics = run_scripted_case().metrics

    assert metrics.ground_truth_identified
    assert metrics.ground_truth_retained
    assert metrics.evidence_identifies_ground_truth
    assert metrics.predictions_evaluated == 3
    assert metrics.predictions_correct == 3
    assert metrics.tests_executed == 1
    assert metrics.discriminating_tests == 1
    assert metrics.behavioral_recovery
    assert metrics.negative_control_behavioral_recovery
    assert not metrics.negative_control_addresses_cause
    assert metrics.ledger_record_count == 23


def test_scripted_run_is_byte_deterministic() -> None:
    first = json.dumps(run_scripted_case().ledger.records, sort_keys=True)
    second = json.dumps(run_scripted_case().ledger.records, sort_keys=True)

    assert first == second


def test_written_ledger_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "case-001.jsonl"

    result = write_scripted_case(path)
    loaded = JsonlLedgerStore(path).load()

    assert loaded.records == result.ledger.records
    assert loaded.snapshot.current_states == result.ledger.snapshot.current_states


def test_scripted_run_records_all_competing_predictions() -> None:
    records = run_scripted_case().ledger.records
    predictions = [record for record in records if record.get("kind") == "prediction"]

    assert len(predictions) == 3
    assert {
        cast(str, prediction["hypothesis_ref"]) for prediction in predictions
    } == set(HYPOTHESIS_IDS.values())


def test_script_cli_writes_ledger_and_prints_scorecard(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "case-001.jsonl"

    result = main(["--output", str(path)])

    captured = capsys.readouterr()
    scorecard = cast(JsonObject, json.loads(captured.out))
    assert result == 0
    assert path.is_file()
    assert scorecard["ground_truth_identified"] is True
    assert scorecard["ledger_record_count"] == 23


def test_selected_intervention_is_the_preregistered_discriminating_test() -> None:
    result = run_scripted_case()

    assert result.selected_hypothesis is Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION
    assert result.intervention is Intervention.BYPASS_CACHING_PROXY
    assert result.observation.passed


def test_diagnostician_output_is_provenance_not_evidence() -> None:
    result = run_scripted_case()
    records = result.ledger.records
    artifact = _record_by_id(records, DIAGNOSTIC_ARTIFACT_ID)
    hypotheses = [record for record in records if record.get("kind") == "hypothesis"]
    transitions = [
        record for record in records if record.get("kind") == "status_transition"
    ]

    assert records[0] == artifact
    assert artifact["kind"] == "artifact_reference"
    assert artifact["digest"] == {
        "algorithm": "sha256",
        "value": result.diagnosis.raw_output_artifact.sha256,
    }
    assert all(
        hypothesis["dependency_refs"] == [DIAGNOSTIC_ARTIFACT_ID]
        for hypothesis in hypotheses
    )
    for record in transitions:
        evidence_refs = record.get("evidence_refs")
        assert not isinstance(evidence_refs, list) or (
            DIAGNOSTIC_ARTIFACT_ID not in evidence_refs
        )


def test_non_discriminating_adapter_result_stays_inconclusive() -> None:
    result = run_scripted_case(
        CacheKeyFixtureDiagnostician(selected_test=Intervention.UNCHANGED_REPLAY)
    )

    assert set(result.ledger.snapshot.current_states.values()) == {
        ClaimStatus.INCONCLUSIVE
    }
    assert result.metrics.discriminating_tests == 0
    assert not result.metrics.evidence_identifies_ground_truth
    assert not result.metrics.behavioral_recovery


def test_external_evidence_can_correct_a_wrong_primary_assertion() -> None:
    result = run_scripted_case(
        CacheKeyFixtureDiagnostician(
            primary_hypothesis=Mechanism.WORKER_READS_STALE_SOURCE,
        )
    )

    assert not result.metrics.ground_truth_identified
    assert result.metrics.evidence_identifies_ground_truth
    assert (
        result.ledger.snapshot.current_states[
            HYPOTHESIS_IDS[Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION]
        ]
        is ClaimStatus.SUPPORTED
    )
