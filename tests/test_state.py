# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import pytest

from itself import (
    ActorRole,
    ActorType,
    ClaimStatus,
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
