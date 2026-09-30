# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

from itself import (
    BundleIntegrityError,
    BundleValidator,
    ClaimStatus,
    IntegrityCode,
    JsonObject,
    JsonValue,
    Ledger,
)

ROOT = Path(__file__).resolve().parents[1]
VALID_BUNDLES = sorted((ROOT / "conformance" / "bundles" / "valid").glob("*.json"))
INVALID_BUNDLES = sorted((ROOT / "conformance" / "bundles" / "invalid").glob("*.json"))

EXPECTED_CODES = {
    "blank-transition-subject.json": IntegrityCode.INVALID_TRANSITION,
    "dangling-reference.json": IntegrityCode.UNRESOLVED_REFERENCE,
    "duplicate-id.json": IntegrityCode.DUPLICATE_ID,
    "invalid-transition.json": IntegrityCode.INVALID_TRANSITION,
    "proto-scope-mismatch.json": IntegrityCode.SCOPE_MISMATCH,
    "state-drift.json": IntegrityCode.STATE_MISMATCH,
    "test-plan-kind-mismatch.json": IntegrityCode.REFERENCE_KIND_MISMATCH,
    "transition-subject-mismatch.json": IntegrityCode.TRANSITION_SUBJECT_MISMATCH,
    "unrelated-evidence.json": IntegrityCode.EVIDENCE_RELATION_MISSING,
    "verdict-after-transition.json": IntegrityCode.REFERENCE_NOT_AVAILABLE,
    "wrong-reference-kind.json": IntegrityCode.REFERENCE_KIND_MISMATCH,
}


def _load_bundle(path: Path) -> list[JsonObject]:
    value = cast(JsonValue, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError(f"bundle fixture {path} must contain a JSON object array")
    return cast(list[JsonObject], value)


@pytest.mark.parametrize("fixture", VALID_BUNDLES, ids=lambda path: path.name)
def test_valid_bundle_replays_deterministically(fixture: Path) -> None:
    records = _load_bundle(fixture)
    first = BundleValidator().validate(records)
    second = BundleValidator().validate(records)

    assert first.record_count == len(records)
    assert first.record_ids == second.record_ids
    assert first.current_states == second.current_states


def test_evidence_backed_history_reaches_scoped_corroboration() -> None:
    records = _load_bundle(
        ROOT / "conformance" / "bundles" / "valid" / "evidence-backed-history.json"
    )
    snapshot = BundleValidator().validate(records)

    assert (
        snapshot.current_states["claim-cache-cause"]
        is ClaimStatus.CORROBORATED_WITHIN_SCOPE
    )


def _record(records: list[JsonObject], record_id: str) -> JsonObject:
    return next(record for record in records if record["id"] == record_id)


def test_reversed_evidence_relation_invalidates_verdict() -> None:
    records = deepcopy(
        _load_bundle(
            ROOT / "conformance" / "bundles" / "valid" / "evidence-backed-history.json"
        )
    )
    evidence = _record(records, "evidence-cache-ablation")
    relations = cast(list[JsonObject], evidence["relations"])
    relations[0]["relation"] = "contradicts"

    issues = BundleValidator().errors(records)

    assert any(
        issue.code is IntegrityCode.EVIDENCE_RELATION_MISSING for issue in issues
    )


def test_changed_evidence_scope_invalidates_verdict() -> None:
    records = deepcopy(
        _load_bundle(
            ROOT / "conformance" / "bundles" / "valid" / "evidence-backed-history.json"
        )
    )
    evidence = _record(records, "evidence-cache-ablation")
    scope = cast(JsonObject, evidence["scope"])
    scope["description"] = "Unrelated deployment scope"

    issues = BundleValidator().errors(records)

    assert any(issue.code is IntegrityCode.SCOPE_MISMATCH for issue in issues)


def test_scope_comparison_preserves_distinct_json_scalar_types() -> None:
    records = deepcopy(
        _load_bundle(
            ROOT / "conformance" / "bundles" / "valid" / "evidence-backed-history.json"
        )
    )
    for record in records:
        scope = record.get("scope")
        if isinstance(scope, dict):
            scope["dimensions"] = {"controlled": True}
    evidence = _record(records, "evidence-cache-ablation")
    cast(JsonObject, evidence["scope"])["dimensions"] = {"controlled": 1}

    issues = BundleValidator().errors(records)

    assert any(issue.code is IntegrityCode.SCOPE_MISMATCH for issue in issues)


def test_transition_must_match_its_cited_verdict() -> None:
    records = deepcopy(
        _load_bundle(
            ROOT / "conformance" / "bundles" / "valid" / "evidence-backed-history.json"
        )
    )
    transition = _record(records, "transition-cache-supported")
    transition["verdict_ref"] = "verdict-cache-corroborated"

    issues = BundleValidator().errors(records)
    mismatch = next(
        issue for issue in issues if issue.code is IntegrityCode.VERDICT_MISMATCH
    )

    assert "outcome, evidence, policy" in mismatch.message


@pytest.mark.parametrize("fixture", INVALID_BUNDLES, ids=lambda path: path.name)
def test_invalid_bundle_reports_stable_integrity_code(fixture: Path) -> None:
    issues = BundleValidator().errors(_load_bundle(fixture))

    assert len(issues) == 1
    assert issues[0].code is EXPECTED_CODES[fixture.name]


def test_validate_raises_with_structured_issues() -> None:
    records = _load_bundle(
        ROOT / "conformance" / "bundles" / "invalid" / "state-drift.json"
    )

    with pytest.raises(BundleIntegrityError) as raised:
        BundleValidator().validate(records)

    assert raised.value.issues[0].code is IntegrityCode.STATE_MISMATCH
    assert "claim-state-drift" in str(raised.value)


def test_explicit_external_reference_is_permitted() -> None:
    records = _load_bundle(
        ROOT / "conformance" / "bundles" / "invalid" / "dangling-reference.json"
    )

    snapshot = BundleValidator().validate(
        records,
        external_refs={"evidence-external"},
    )

    assert (
        snapshot.current_states["claim-with-dangling-evidence"] is ClaimStatus.PROPOSED
    )


def test_external_reference_cannot_mask_local_wrong_kind() -> None:
    records = _load_bundle(
        ROOT / "conformance" / "bundles" / "invalid" / "wrong-reference-kind.json"
    )

    issues = BundleValidator().errors(
        records,
        external_refs={"artifact-not-evidence"},
    )

    assert [issue.code for issue in issues] == [IntegrityCode.REFERENCE_KIND_MISMATCH]


def test_transition_authorizing_verdict_must_be_local() -> None:
    records = _load_bundle(
        ROOT / "conformance" / "bundles" / "invalid" / "verdict-after-transition.json"
    )[:-1]

    issues = BundleValidator().errors(
        records,
        external_refs={"verdict-created-later"},
    )

    assert any(issue.code is IntegrityCode.UNRESOLVED_REFERENCE for issue in issues)


def test_empty_bundle_has_empty_snapshot() -> None:
    snapshot = BundleValidator().validate([])

    assert snapshot.record_count == 0
    assert not snapshot.current_states


def test_bundle_validation_does_not_mutate_records() -> None:
    records = _load_bundle(VALID_BUNDLES[0])
    before = json.dumps(records, sort_keys=True)

    BundleValidator().validate(records)

    assert json.dumps(records, sort_keys=True) == before


def test_fully_schema_checked_prefix_still_replays_every_record() -> None:
    records = _load_bundle(VALID_BUNDLES[0])

    snapshot = BundleValidator().validate(
        records,
        schema_checked_prefix=len(records),
    )

    assert snapshot == BundleValidator().validate(records)


@pytest.mark.parametrize(
    "prefix",
    [-1, 3, True, 1.0, "1", None],
    ids=["negative", "past-end", "bool", "float", "string", "none"],
)
def test_invalid_schema_checked_prefix_is_rejected(prefix: object) -> None:
    records = _load_bundle(VALID_BUNDLES[0])[:2]
    validator = BundleValidator()

    with pytest.raises(ValueError, match="schema_checked_prefix must be an integer"):
        validator.validate(records, schema_checked_prefix=cast(int, prefix))
    with pytest.raises(ValueError, match="schema_checked_prefix must be an integer"):
        validator.errors(records, schema_checked_prefix=cast(int, prefix))


def test_bundle_fixture_sets_are_not_empty() -> None:
    assert VALID_BUNDLES
    assert INVALID_BUNDLES
    assert {fixture.name for fixture in INVALID_BUNDLES} == set(EXPECTED_CODES)


HISTORY = ROOT / "conformance" / "bundles" / "valid" / "evidence-backed-history.json"


def test_default_validator_accepts_any_declared_authorizer() -> None:
    validator = BundleValidator()

    assert validator.trusted_authorizers is None
    assert validator.errors(_load_bundle(HISTORY)) == []


def test_trusted_authorizers_accept_listed_actors() -> None:
    validator = BundleValidator(
        trusted_authorizers=["default-transition-policy", "reviewer-alex"]
    )

    snapshot = validator.validate(_load_bundle(HISTORY))

    assert validator.trusted_authorizers == frozenset(
        {"default-transition-policy", "reviewer-alex"}
    )
    assert (
        snapshot.current_states["claim-cache-cause"]
        is ClaimStatus.CORROBORATED_WITHIN_SCOPE
    )


def test_trusted_authorizers_reject_other_evidence_backed_authorizers() -> None:
    issues = BundleValidator(trusted_authorizers={"default-transition-policy"}).errors(
        _load_bundle(HISTORY)
    )

    assert [(issue.code, issue.record_id, issue.reference) for issue in issues] == [
        (
            IntegrityCode.UNTRUSTED_AUTHORIZER,
            "transition-cache-corroborated",
            "reviewer-alex",
        )
    ]


def test_trusted_authorizers_leave_other_transitions_alone() -> None:
    issues = BundleValidator(trusted_authorizers=()).errors(_load_bundle(HISTORY))

    # The model's move to testable and the runner's move to under_test are not
    # evidence-backed, so only the two promotions are refused. A refused
    # transition does not apply, so the later one also starts from the wrong
    # state.
    untrusted = [
        issue for issue in issues if issue.code is IntegrityCode.UNTRUSTED_AUTHORIZER
    ]
    assert sorted(issue.reference or "" for issue in untrusted) == [
        "default-transition-policy",
        "reviewer-alex",
    ]
    assert {issue.code for issue in issues} == {
        IntegrityCode.UNTRUSTED_AUTHORIZER,
        IntegrityCode.STATE_MISMATCH,
    }


@pytest.mark.parametrize("value", ["reviewer-alex", [""], ["  "], [3], None])
def test_trusted_authorizers_must_be_actor_ids(value: object) -> None:
    if value is None:
        assert BundleValidator(trusted_authorizers=None).trusted_authorizers is None
        return
    with pytest.raises(ValueError, match="trusted_authorizers"):
        BundleValidator(trusted_authorizers=value)  # type: ignore[arg-type]


def test_ledger_refuses_a_promotion_by_an_untrusted_actor() -> None:
    records = _load_bundle(HISTORY)
    position = [record["id"] for record in records].index("transition-cache-supported")
    ledger = Ledger(
        records[:position],
        validator=BundleValidator(trusted_authorizers={"reviewer-alex"}),
    )

    with pytest.raises(BundleIntegrityError) as raised:
        ledger.append(records[position])

    assert [issue.code for issue in raised.value.issues] == [
        IntegrityCode.UNTRUSTED_AUTHORIZER
    ]
    assert len(ledger) == position
