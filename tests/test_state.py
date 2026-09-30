# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from typing import Any

import pytest

from itself import (
    ActorRole,
    ActorType,
    ClaimStatus,
    JsonValue,
    TransitionError,
    TransitionRequest,
    validate_transition,
)


def request(
    from_status: ClaimStatus,
    to_status: ClaimStatus,
    *,
    actor_type: ActorType = ActorType.SOFTWARE,
    actor_role: ActorRole = ActorRole.EVALUATOR,
    evidence_refs: tuple[str, ...] = (),
    verdict_ref: str | None = None,
    policy_ref: str | None = None,
) -> TransitionRequest:
    return TransitionRequest(
        subject_ref="claim-1",
        from_status=from_status,
        to_status=to_status,
        authorized_by="actor-1",
        actor_type=actor_type,
        actor_role=actor_role,
        evidence_refs=evidence_refs,
        verdict_ref=verdict_ref,
        policy_ref=policy_ref,
    )


def test_proposer_can_mark_an_assertion_testable() -> None:
    validate_transition(
        request(
            ClaimStatus.PROPOSED,
            ClaimStatus.TESTABLE,
            actor_role=ActorRole.PROPOSER,
        )
    )


def test_evaluator_can_support_an_under_test_claim_with_evidence() -> None:
    validate_transition(
        request(
            ClaimStatus.UNDER_TEST,
            ClaimStatus.SUPPORTED,
            evidence_refs=("evidence-1",),
            verdict_ref="verdict-1",
        )
    )


def test_evidence_backed_transition_requires_evidence() -> None:
    with pytest.raises(
        TransitionError, match="requires at least one evidence reference"
    ):
        validate_transition(request(ClaimStatus.UNDER_TEST, ClaimStatus.SUPPORTED))


def test_evidence_backed_transition_requires_verdict() -> None:
    with pytest.raises(TransitionError, match="requires a verdict_ref"):
        validate_transition(
            request(
                ClaimStatus.UNDER_TEST,
                ClaimStatus.SUPPORTED,
                evidence_refs=("evidence-1",),
            )
        )


def test_proposer_cannot_authorize_evidence_backed_transition() -> None:
    with pytest.raises(TransitionError, match="cannot authorize"):
        validate_transition(
            request(
                ClaimStatus.UNDER_TEST,
                ClaimStatus.SUPPORTED,
                actor_role=ActorRole.PROPOSER,
                evidence_refs=("evidence-1",),
                verdict_ref="verdict-1",
            )
        )


def test_model_evaluator_cannot_authorize_evidence_backed_transition() -> None:
    with pytest.raises(TransitionError, match="model actor cannot authorize"):
        validate_transition(
            request(
                ClaimStatus.UNDER_TEST,
                ClaimStatus.SUPPORTED,
                actor_type=ActorType.MODEL,
                actor_role=ActorRole.EVALUATOR,
                evidence_refs=("evidence-model-opinion",),
                verdict_ref="verdict-model-opinion",
            )
        )


def test_corroboration_requires_policy() -> None:
    with pytest.raises(TransitionError, match="requires a policy_ref"):
        validate_transition(
            request(
                ClaimStatus.SUPPORTED,
                ClaimStatus.CORROBORATED_WITHIN_SCOPE,
                evidence_refs=("evidence-1", "evidence-2"),
                verdict_ref="verdict-1",
            )
        )


def test_corroboration_with_policy_is_allowed() -> None:
    validate_transition(
        request(
            ClaimStatus.SUPPORTED,
            ClaimStatus.CORROBORATED_WITHIN_SCOPE,
            evidence_refs=("evidence-1", "evidence-2"),
            verdict_ref="verdict-1",
            policy_ref="policy-1",
        )
    )


def test_invalid_transition_is_rejected() -> None:
    with pytest.raises(TransitionError, match="is not allowed"):
        validate_transition(request(ClaimStatus.PROPOSED, ClaimStatus.SUPPORTED))


def test_stale_claim_can_be_reopened() -> None:
    validate_transition(request(ClaimStatus.STALE, ClaimStatus.UNDER_TEST))


def decoded_request(**fields: JsonValue) -> TransitionRequest:
    """Build a request from JSON-decoded values, which are plain strings."""

    decoded: dict[str, Any] = json.loads(
        json.dumps({"subject_ref": "claim-1", "authorized_by": "actor-1", **fields})
    )
    return TransitionRequest(**decoded)


def test_decoded_request_values_become_enum_members() -> None:
    decoded = decoded_request(
        from_status="under_test",
        to_status="supported",
        actor_type="software",
        actor_role="evaluator",
        evidence_refs=["evidence-1"],
        verdict_ref="verdict-1",
    )

    assert decoded.from_status is ClaimStatus.UNDER_TEST
    assert decoded.to_status is ClaimStatus.SUPPORTED
    assert decoded.actor_type is ActorType.SOFTWARE
    assert decoded.actor_role is ActorRole.EVALUATOR
    assert decoded.evidence_refs == ("evidence-1",)


def test_decoded_model_promotion_is_rejected() -> None:
    with pytest.raises(TransitionError, match="model actor cannot authorize"):
        validate_transition(
            decoded_request(
                from_status="under_test",
                to_status="supported",
                actor_type="model",
                actor_role="evaluator",
                evidence_refs=["evidence-model-opinion"],
                verdict_ref="verdict-model-opinion",
            )
        )


def test_decoded_disallowed_transition_is_rejected() -> None:
    with pytest.raises(TransitionError, match="proposed -> supported is not allowed"):
        validate_transition(
            decoded_request(
                from_status="proposed",
                to_status="supported",
                actor_type="software",
                actor_role="evaluator",
            )
        )


def test_decoded_corroboration_requires_policy() -> None:
    with pytest.raises(TransitionError, match="requires a policy_ref"):
        validate_transition(
            decoded_request(
                from_status="supported",
                to_status="corroborated_within_scope",
                actor_type="software",
                actor_role="evaluator",
                evidence_refs=["evidence-1", "evidence-2"],
                verdict_ref="verdict-1",
            )
        )


@pytest.mark.parametrize(
    "field", ["from_status", "to_status", "actor_type", "actor_role"]
)
def test_unknown_decoded_value_is_rejected(field: str) -> None:
    fields: dict[str, JsonValue] = {
        "from_status": "proposed",
        "to_status": "testable",
        "actor_type": "software",
        "actor_role": "proposer",
    }
    fields[field] = "unknown"

    with pytest.raises(ValueError, match="'unknown' is not a valid"):
        decoded_request(**fields)


def test_single_string_evidence_reference_is_rejected() -> None:
    with pytest.raises(ValueError, match="evidence_refs must be a sequence"):
        decoded_request(
            from_status="under_test",
            to_status="supported",
            actor_type="software",
            actor_role="evaluator",
            evidence_refs="evidence-1",
            verdict_ref="verdict-1",
        )
