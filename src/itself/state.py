# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Reference epistemic-state transition policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import cast


class ClaimStatus(StrEnum):
    PROPOSED = "proposed"
    TESTABLE = "testable"
    BLOCKED = "blocked"
    UNDER_TEST = "under_test"
    SUPPORTED = "supported"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"
    MIXED_EVIDENCE = "mixed_evidence"
    CORROBORATED_WITHIN_SCOPE = "corroborated_within_scope"
    STALE = "stale"
    INVALIDATED = "invalidated"
    WITHDRAWN = "withdrawn"


class ActorRole(StrEnum):
    PROPOSER = "proposer"
    EVALUATOR = "evaluator"
    REVIEWER = "reviewer"
    AUTHORITY = "authority"
    OPERATOR = "operator"


class ActorType(StrEnum):
    HUMAN = "human"
    MODEL = "model"
    SOFTWARE = "software"
    ORGANIZATION = "organization"


_ALLOWED_TRANSITIONS: dict[ClaimStatus, frozenset[ClaimStatus]] = {
    ClaimStatus.PROPOSED: frozenset(
        {ClaimStatus.TESTABLE, ClaimStatus.BLOCKED, ClaimStatus.WITHDRAWN}
    ),
    ClaimStatus.TESTABLE: frozenset(
        {ClaimStatus.UNDER_TEST, ClaimStatus.BLOCKED, ClaimStatus.WITHDRAWN}
    ),
    ClaimStatus.BLOCKED: frozenset({ClaimStatus.TESTABLE, ClaimStatus.WITHDRAWN}),
    ClaimStatus.UNDER_TEST: frozenset(
        {
            ClaimStatus.SUPPORTED,
            ClaimStatus.REFUTED,
            ClaimStatus.INCONCLUSIVE,
            ClaimStatus.MIXED_EVIDENCE,
            ClaimStatus.BLOCKED,
        }
    ),
    ClaimStatus.SUPPORTED: frozenset(
        {
            ClaimStatus.CORROBORATED_WITHIN_SCOPE,
            ClaimStatus.UNDER_TEST,
            ClaimStatus.MIXED_EVIDENCE,
            ClaimStatus.STALE,
            ClaimStatus.INVALIDATED,
        }
    ),
    ClaimStatus.REFUTED: frozenset(
        {ClaimStatus.UNDER_TEST, ClaimStatus.STALE, ClaimStatus.INVALIDATED}
    ),
    ClaimStatus.INCONCLUSIVE: frozenset(
        {ClaimStatus.UNDER_TEST, ClaimStatus.STALE, ClaimStatus.INVALIDATED}
    ),
    ClaimStatus.MIXED_EVIDENCE: frozenset(
        {
            ClaimStatus.UNDER_TEST,
            ClaimStatus.SUPPORTED,
            ClaimStatus.REFUTED,
            ClaimStatus.INCONCLUSIVE,
            ClaimStatus.STALE,
            ClaimStatus.INVALIDATED,
        }
    ),
    ClaimStatus.CORROBORATED_WITHIN_SCOPE: frozenset(
        {
            ClaimStatus.UNDER_TEST,
            ClaimStatus.MIXED_EVIDENCE,
            ClaimStatus.STALE,
            ClaimStatus.INVALIDATED,
        }
    ),
    ClaimStatus.STALE: frozenset({ClaimStatus.UNDER_TEST, ClaimStatus.INVALIDATED}),
    ClaimStatus.INVALIDATED: frozenset({ClaimStatus.UNDER_TEST}),
    ClaimStatus.WITHDRAWN: frozenset(),
}

_EVIDENCE_BACKED_STATES = frozenset(
    {
        ClaimStatus.SUPPORTED,
        ClaimStatus.REFUTED,
        ClaimStatus.INCONCLUSIVE,
        ClaimStatus.MIXED_EVIDENCE,
        ClaimStatus.CORROBORATED_WITHIN_SCOPE,
    }
)

_PROMOTION_ROLES = frozenset(
    {ActorRole.EVALUATOR, ActorRole.REVIEWER, ActorRole.AUTHORITY}
)


@dataclass(frozen=True, slots=True)
class TransitionRequest:
    subject_ref: str
    from_status: ClaimStatus
    to_status: ClaimStatus
    authorized_by: str
    actor_type: ActorType
    actor_role: ActorRole
    evidence_refs: tuple[str, ...] = ()
    verdict_ref: str | None = None
    policy_ref: str | None = None

    def __post_init__(self) -> None:
        # Values decoded from JSON arrive as plain strings and lists. Normalize
        # them so policy checks compare enum members, and reject unknown names.
        object.__setattr__(self, "from_status", ClaimStatus(self.from_status))
        object.__setattr__(self, "to_status", ClaimStatus(self.to_status))
        object.__setattr__(self, "actor_type", ActorType(self.actor_type))
        object.__setattr__(self, "actor_role", ActorRole(self.actor_role))
        if isinstance(cast(object, self.evidence_refs), str):
            raise ValueError(
                "evidence_refs must be a sequence of references, not a single string"
            )
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))


class TransitionError(ValueError):
    """Raised when a requested epistemic-state transition violates policy."""


def validate_transition(request: TransitionRequest) -> None:
    """Validate one transition against the current protocol policy."""

    if not request.subject_ref.strip():
        raise TransitionError("subject_ref is required")
    if not request.authorized_by.strip():
        raise TransitionError("authorized_by is required")
    if request.to_status not in _ALLOWED_TRANSITIONS[request.from_status]:
        raise TransitionError(
            f"transition {request.from_status.value} -> {request.to_status.value} is not allowed"
        )
    if request.to_status in _EVIDENCE_BACKED_STATES:
        if not request.evidence_refs:
            raise TransitionError(
                f"transition to {request.to_status.value} requires at least one evidence reference"
            )
        if not request.verdict_ref:
            raise TransitionError(
                f"transition to {request.to_status.value} requires a verdict_ref"
            )
        if request.actor_role not in _PROMOTION_ROLES:
            raise TransitionError(
                f"role {request.actor_role.value} cannot authorize an evidence-backed transition"
            )
        if request.actor_type is ActorType.MODEL:
            raise TransitionError(
                "a model actor cannot authorize an evidence-backed transition"
            )
    if (
        request.to_status is ClaimStatus.CORROBORATED_WITHIN_SCOPE
        and not request.policy_ref
    ):
        raise TransitionError("corroboration requires a policy_ref")
