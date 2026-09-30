# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Typed construction helpers for Claim and Evidence Protocol records."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from functools import cache
from types import MappingProxyType
from typing import ClassVar, Final, cast

from .state import (
    ActorRole,
    ActorType,
    ClaimStatus,
    TransitionError,
    TransitionRequest,
    validate_transition,
)
from .types import JsonObject, JsonScalar, JsonValue
from .validation import ProtocolValidationError, ProtocolValidator

PROTOCOL_VERSION: Final = "0.1.0-alpha.3"


class RecordConstructionError(ValueError):
    """Raised when typed inputs cannot produce a valid protocol record."""


class DigestAlgorithm(StrEnum):
    """Digest algorithms permitted by artifact-reference records."""

    SHA256 = "sha256"
    SHA512 = "sha512"
    BLAKE3 = "blake3"
    OTHER = "other"


class AuthorityType(StrEnum):
    """Declared authority categories for evidence, verdicts, and oracles."""

    DETERMINISTIC_TOOL = "deterministic_tool"
    DOMAIN_EVALUATOR = "domain_evaluator"
    MODEL_EVALUATOR = "model_evaluator"
    HUMAN_REVIEWER = "human_reviewer"
    POLICY_ENGINE = "policy_engine"
    EXTERNAL_SOURCE = "external_source"


class ClaimType(StrEnum):
    """Semantic categories available to claim records."""

    FACTUAL = "factual"
    CAUSAL = "causal"
    PREDICTIVE = "predictive"
    INTERPRETIVE = "interpretive"
    NORMATIVE = "normative"


class PredictionCondition(StrEnum):
    """Conditions under which a prediction declares its observation."""

    HYPOTHESIS_TRUE = "hypothesis_true"
    HYPOTHESIS_FALSE = "hypothesis_false"


class TestDesign(StrEnum):
    """Controlled vocabulary for protocol test designs."""

    __test__: ClassVar[bool] = False

    CONTROLLED_ABLATION = "controlled_ablation"
    FACTORIAL = "factorial"
    DETERMINISTIC_CHECK = "deterministic_check"
    REPLAY = "replay"
    QUERY = "query"
    SIMULATION = "simulation"
    SOURCE_CORROBORATION = "source_corroboration"
    HUMAN_REVIEW = "human_review"
    OTHER = "other"


class TestStatus(StrEnum):
    """Lifecycle states for immutable planned and executed test records."""

    __test__: ClassVar[bool] = False

    PLANNED = "planned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class EvidenceRelationType(StrEnum):
    """Permitted evidence-to-subject relations."""

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXTUALIZES = "contextualizes"


class EvidenceType(StrEnum):
    """Source and production categories for evidence records."""

    OBSERVATION = "observation"
    DETERMINISTIC_TEST = "deterministic_test"
    BENCHMARK_RESULT = "benchmark_result"
    CALCULATION = "calculation"
    DATABASE_QUERY = "database_query"
    SIMULATION = "simulation"
    SOURCE_RECORD = "source_record"
    MODEL_EVALUATION = "model_evaluation"
    HUMAN_REVIEW = "human_review"
    POLICY_CHECK = "policy_check"
    OTHER = "other"


class VerdictOutcome(StrEnum):
    """Scoped outcomes available to verdict records."""

    SUPPORTED = "supported"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"
    MIXED_EVIDENCE = "mixed_evidence"
    CORROBORATED_WITHIN_SCOPE = "corroborated_within_scope"


class DecisionDisposition(StrEnum):
    """Accountable dispositions available to decision records."""

    APPROVED = "approved"
    REJECTED = "rejected"
    DEFERRED = "deferred"
    ESCALATED = "escalated"
    EXECUTED = "executed"


def _nonempty(value: str, field_name: str) -> None:
    if not value.strip():
        raise RecordConstructionError(f"{field_name} must not be empty")


def _timestamp(value: datetime, field_name: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RecordConstructionError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _string_values(values: Sequence[str], field_name: str) -> list[JsonValue]:
    # A str is itself a Sequence[str], so type checkers accept one here.
    if isinstance(values, str):
        raise RecordConstructionError(
            f"{field_name} must be a sequence of strings, not a single string"
        )
    return [value for value in values]


def _object_values(values: Sequence[JsonObject]) -> list[JsonValue]:
    return [value for value in values]


def _empty_dimensions() -> Mapping[str, JsonScalar]:
    return {}


@cache
def _validator() -> ProtocolValidator:
    return ProtocolValidator()


def _validated_record(record: JsonObject) -> JsonObject:
    try:
        encoded = json.dumps(
            record,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        value = cast(JsonValue, json.loads(encoded))
    except (TypeError, ValueError) as error:
        raise RecordConstructionError(
            f"record contains a value that cannot be represented as strict JSON: {error}"
        ) from error
    if not isinstance(value, dict):
        raise TypeError("constructed protocol record was not an object")
    try:
        _validator().validate(value)
    except ProtocolValidationError as error:
        raise RecordConstructionError(str(error)) from error
    return value


@dataclass(frozen=True, slots=True)
class Actor:
    """A typed actor declaration used in provenance and authorization fields."""

    actor_id: str
    actor_type: ActorType
    role: ActorRole
    implementation_ref: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.actor_id, "actor_id")
        try:
            object.__setattr__(self, "actor_type", ActorType(self.actor_type))
            object.__setattr__(self, "role", ActorRole(self.role))
        except ValueError as error:
            raise RecordConstructionError(str(error)) from error
        if self.implementation_ref is not None:
            _nonempty(self.implementation_ref, "implementation_ref")

    def to_json_object(self) -> JsonObject:
        """Return the actor's protocol wire representation."""

        value: JsonObject = {
            "id": self.actor_id,
            "actor_type": self.actor_type.value,
            "role": self.role.value,
        }
        if self.implementation_ref is not None:
            value["implementation_ref"] = self.implementation_ref
        return value


@dataclass(frozen=True, slots=True)
class Scope:
    """The explicit boundary within which a record may be interpreted."""

    description: str
    dimensions: Mapping[str, JsonScalar] = field(default_factory=_empty_dimensions)
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        _nonempty(self.description, "scope.description")
        object.__setattr__(
            self,
            "dimensions",
            MappingProxyType(dict(self.dimensions)),
        )
        if self.valid_from is not None:
            _timestamp(self.valid_from, "scope.valid_from")
        if self.valid_until is not None:
            _timestamp(self.valid_until, "scope.valid_until")

    def to_json_object(self) -> JsonObject:
        """Return the scope's protocol wire representation."""

        value: JsonObject = {"description": self.description}
        if self.dimensions:
            value["dimensions"] = dict(self.dimensions)
        if self.valid_from is not None:
            value["valid_from"] = _timestamp(self.valid_from, "scope.valid_from")
        if self.valid_until is not None:
            value["valid_until"] = _timestamp(
                self.valid_until,
                "scope.valid_until",
            )
        return value


@dataclass(frozen=True, slots=True)
class RecordHeader:
    """Shared provenance fields for one immutable protocol record."""

    record_id: str
    created_at: datetime
    created_by: Actor

    def __post_init__(self) -> None:
        _nonempty(self.record_id, "record_id")
        _timestamp(self.created_at, "created_at")

    def to_json_object(self) -> JsonObject:
        """Return the shared protocol record header fields."""

        return {
            "protocol_version": PROTOCOL_VERSION,
            "id": self.record_id,
            "created_at": _timestamp(self.created_at, "created_at"),
            "created_by": self.created_by.to_json_object(),
        }


@dataclass(frozen=True, slots=True)
class Digest:
    """A declared digest for an externally retained artifact."""

    algorithm: DigestAlgorithm
    value: str

    def __post_init__(self) -> None:
        _nonempty(self.value, "digest.value")

    def to_json_object(self) -> JsonObject:
        """Return the digest's protocol wire representation."""

        return {
            "algorithm": self.algorithm.value,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class Authority:
    """The declared issuer and basis of evidence or a verdict."""

    authority_type: AuthorityType
    actor_ref: str
    basis: str
    independent_of_subject: bool | None = None

    def __post_init__(self) -> None:
        _nonempty(self.actor_ref, "authority.actor_ref")
        _nonempty(self.basis, "authority.basis")

    def to_json_object(self) -> JsonObject:
        """Return the authority's protocol wire representation."""

        value: JsonObject = {
            "authority_type": self.authority_type.value,
            "actor_ref": self.actor_ref,
            "basis": self.basis,
        }
        if self.independent_of_subject is not None:
            value["independent_of_subject"] = self.independent_of_subject
        return value


@dataclass(frozen=True, slots=True)
class Oracle:
    """A declared test oracle or evaluator adapter."""

    adapter: str
    version: str
    authority_type: AuthorityType
    declared_scope: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.adapter, "oracle.adapter")
        _nonempty(self.version, "oracle.version")
        if self.declared_scope is not None:
            _nonempty(self.declared_scope, "oracle.declared_scope")

    def to_json_object(self) -> JsonObject:
        """Return the oracle's protocol wire representation."""

        value: JsonObject = {
            "adapter": self.adapter,
            "version": self.version,
            "authority_type": self.authority_type.value,
        }
        if self.declared_scope is not None:
            value["declared_scope"] = self.declared_scope
        return value


@dataclass(frozen=True, slots=True)
class EvidenceRelation:
    """A scoped relation between one evidence record and one subject."""

    subject_ref: str
    relation: EvidenceRelationType
    public_note: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.subject_ref, "evidence_relation.subject_ref")
        if self.public_note is not None:
            _nonempty(self.public_note, "evidence_relation.public_note")

    def to_json_object(self) -> JsonObject:
        """Return the evidence relation's protocol wire representation."""

        value: JsonObject = {
            "subject_ref": self.subject_ref,
            "relation": self.relation.value,
        }
        if self.public_note is not None:
            value["public_note"] = self.public_note
        return value


@dataclass(frozen=True, slots=True)
class TestCost:
    """Optional observable resource use for one test execution."""

    __test__: ClassVar[bool] = False

    model_tokens: int | None = None
    wall_time_ms: int | None = None
    human_minutes: float | None = None

    def to_json_object(self) -> JsonObject:
        """Return only the declared test-cost fields."""

        value: JsonObject = {}
        if self.model_tokens is not None:
            value["model_tokens"] = self.model_tokens
        if self.wall_time_ms is not None:
            value["wall_time_ms"] = self.wall_time_ms
        if self.human_minutes is not None:
            value["human_minutes"] = self.human_minutes
        return value


def _record(header: RecordHeader, kind: str) -> JsonObject:
    value = header.to_json_object()
    value["kind"] = kind
    return value


def artifact_reference_record(
    header: RecordHeader,
    *,
    uri: str,
    media_type: str,
    title: str | None = None,
    digest: Digest | None = None,
    captured_at: datetime | None = None,
    derived_from_refs: Sequence[str] = (),
) -> JsonObject:
    """Construct and schema-validate one artifact-reference record."""

    value = _record(header, "artifact_reference")
    value["uri"] = uri
    value["media_type"] = media_type
    if title is not None:
        value["title"] = title
    if digest is not None:
        value["digest"] = digest.to_json_object()
    if captured_at is not None:
        value["captured_at"] = _timestamp(captured_at, "captured_at")
    if derived_from_refs:
        value["derived_from_refs"] = _string_values(
            derived_from_refs, "derived_from_refs"
        )
    return _validated_record(value)


def claim_record(
    header: RecordHeader,
    *,
    proposition: str,
    claim_type: ClaimType,
    scope: Scope,
    status: ClaimStatus = ClaimStatus.PROPOSED,
    evidence_refs: Sequence[str] = (),
    contradicting_evidence_refs: Sequence[str] = (),
    dependency_refs: Sequence[str] = (),
    transition_refs: Sequence[str] = (),
    invalidation_conditions: Sequence[str] = (),
) -> JsonObject:
    """Construct and schema-validate one claim record."""

    value = _record(header, "claim")
    value.update(
        {
            "proposition": proposition,
            "claim_type": claim_type.value,
            "status": status.value,
            "scope": scope.to_json_object(),
        }
    )
    for field_name, references in (
        ("evidence_refs", evidence_refs),
        ("contradicting_evidence_refs", contradicting_evidence_refs),
        ("dependency_refs", dependency_refs),
        ("transition_refs", transition_refs),
        ("invalidation_conditions", invalidation_conditions),
    ):
        if references:
            value[field_name] = _string_values(references, field_name)
    return _validated_record(value)


def hypothesis_record(
    header: RecordHeader,
    *,
    statement: str,
    scope: Scope,
    status: ClaimStatus = ClaimStatus.PROPOSED,
    explains_evidence_refs: Sequence[str] = (),
    alternative_hypothesis_refs: Sequence[str] = (),
    prediction_refs: Sequence[str] = (),
    dependency_refs: Sequence[str] = (),
) -> JsonObject:
    """Construct and schema-validate one hypothesis record."""

    value = _record(header, "hypothesis")
    value.update(
        {
            "statement": statement,
            "status": status.value,
            "scope": scope.to_json_object(),
        }
    )
    for field_name, references in (
        ("explains_evidence_refs", explains_evidence_refs),
        ("alternative_hypothesis_refs", alternative_hypothesis_refs),
        ("prediction_refs", prediction_refs),
        ("dependency_refs", dependency_refs),
    ):
        if references:
            value[field_name] = _string_values(references, field_name)
    return _validated_record(value)


def prediction_record(
    header: RecordHeader,
    *,
    hypothesis_ref: str,
    condition: PredictionCondition,
    expected_observation: str,
    scope: Scope,
    falsified_when: str | None = None,
    test_ref: str | None = None,
) -> JsonObject:
    """Construct and schema-validate one falsifiable prediction record."""

    value = _record(header, "prediction")
    value.update(
        {
            "hypothesis_ref": hypothesis_ref,
            "condition": condition.value,
            "expected_observation": expected_observation,
            "scope": scope.to_json_object(),
        }
    )
    if falsified_when is not None:
        value["falsified_when"] = falsified_when
    if test_ref is not None:
        value["test_ref"] = test_ref
    return _validated_record(value)


def protocol_test_record(
    header: RecordHeader,
    *,
    question: str,
    design: TestDesign,
    status: TestStatus,
    oracle: Oracle,
    scope: Scope,
    subject_refs: Sequence[str] = (),
    prediction_refs: Sequence[str] = (),
    plan_ref: str | None = None,
    evidence_refs: Sequence[str] = (),
    cost: TestCost | None = None,
) -> JsonObject:
    """Construct and schema-validate one planned or executed test record.

    A ``TestStatus.COMPLETED`` test must cite the evidence it produced in
    ``evidence_refs`` and should name its plan in ``plan_ref``. The evidence
    usually cites this test back through ``test_ref``, so append the two
    records in one ``Ledger.extend`` or ``JsonlLedgerStore.extend`` batch.
    """

    value = _record(header, "test")
    value.update(
        {
            "question": question,
            "design": design.value,
            "status": status.value,
            "oracle": oracle.to_json_object(),
            "scope": scope.to_json_object(),
        }
    )
    if subject_refs:
        value["subject_refs"] = _string_values(subject_refs, "subject_refs")
    if prediction_refs:
        value["prediction_refs"] = _string_values(prediction_refs, "prediction_refs")
    if plan_ref is not None:
        value["plan_ref"] = plan_ref
    if evidence_refs:
        value["evidence_refs"] = _string_values(evidence_refs, "evidence_refs")
    if cost is not None:
        value["cost"] = cost.to_json_object()
    return _validated_record(value)


def evidence_record(
    header: RecordHeader,
    *,
    evidence_type: EvidenceType,
    relations: Sequence[EvidenceRelation],
    authority: Authority,
    scope: Scope,
    artifact_refs: Sequence[str] = (),
    test_ref: str | None = None,
    result: Mapping[str, JsonValue] | None = None,
    expires_at: datetime | None = None,
) -> JsonObject:
    """Construct and schema-validate one evidence record."""

    value = _record(header, "evidence")
    value.update(
        {
            "evidence_type": evidence_type.value,
            "relations": _object_values(
                tuple(relation.to_json_object() for relation in relations)
            ),
            "authority": authority.to_json_object(),
            "scope": scope.to_json_object(),
        }
    )
    if artifact_refs:
        value["artifact_refs"] = _string_values(artifact_refs, "artifact_refs")
    if test_ref is not None:
        value["test_ref"] = test_ref
    if result is not None:
        value["result"] = deepcopy(dict(result))
    if expires_at is not None:
        value["expires_at"] = _timestamp(expires_at, "expires_at")
    return _validated_record(value)


def verdict_record(
    header: RecordHeader,
    *,
    subject_ref: str,
    outcome: VerdictOutcome,
    evidence_refs: Sequence[str],
    authority: Authority,
    scope: Scope,
    public_rationale: str,
    policy_ref: str | None = None,
) -> JsonObject:
    """Construct and schema-validate one evidence-bound verdict record."""

    value = _record(header, "verdict")
    value.update(
        {
            "subject_ref": subject_ref,
            "outcome": outcome.value,
            "evidence_refs": _string_values(evidence_refs, "evidence_refs"),
            "authority": authority.to_json_object(),
            "scope": scope.to_json_object(),
            "public_rationale": public_rationale,
        }
    )
    if policy_ref is not None:
        value["policy_ref"] = policy_ref
    return _validated_record(value)


def decision_record(
    header: RecordHeader,
    *,
    question: str,
    disposition: DecisionDisposition,
    authorized_by: Actor,
    scope: Scope,
    policy_ref: str | None = None,
    relied_on_claim_refs: Sequence[str] = (),
    unresolved_claim_refs: Sequence[str] = (),
    reconsider_when: Sequence[str] = (),
    public_rationale: str | None = None,
) -> JsonObject:
    """Construct and schema-validate one accountable decision record."""

    value = _record(header, "decision")
    value.update(
        {
            "question": question,
            "disposition": disposition.value,
            "authorized_by": authorized_by.to_json_object(),
            "scope": scope.to_json_object(),
        }
    )
    if policy_ref is not None:
        value["policy_ref"] = policy_ref
    if relied_on_claim_refs:
        value["relied_on_claim_refs"] = _string_values(
            relied_on_claim_refs, "relied_on_claim_refs"
        )
    if unresolved_claim_refs:
        value["unresolved_claim_refs"] = _string_values(
            unresolved_claim_refs, "unresolved_claim_refs"
        )
    if reconsider_when:
        value["reconsider_when"] = _string_values(reconsider_when, "reconsider_when")
    if public_rationale is not None:
        value["public_rationale"] = public_rationale
    return _validated_record(value)


def status_transition_record(
    header: RecordHeader,
    *,
    subject_ref: str,
    from_status: ClaimStatus,
    to_status: ClaimStatus,
    authorized_by: Actor,
    reason: str,
    evidence_refs: Sequence[str] = (),
    verdict_ref: str | None = None,
    policy_ref: str | None = None,
) -> JsonObject:
    """Construct and validate one transition under the reference state policy."""

    try:
        validate_transition(
            TransitionRequest(
                subject_ref=subject_ref,
                from_status=from_status,
                to_status=to_status,
                authorized_by=authorized_by.actor_id,
                actor_type=authorized_by.actor_type,
                actor_role=authorized_by.role,
                evidence_refs=tuple(evidence_refs),
                verdict_ref=verdict_ref,
                policy_ref=policy_ref,
            )
        )
    except TransitionError as error:
        raise RecordConstructionError(str(error)) from error

    value = _record(header, "status_transition")
    value.update(
        {
            "subject_ref": subject_ref,
            "from_status": from_status.value,
            "to_status": to_status.value,
            "authorized_by": authorized_by.to_json_object(),
            "reason": reason,
        }
    )
    if evidence_refs:
        value["evidence_refs"] = _string_values(evidence_refs, "evidence_refs")
    if verdict_ref is not None:
        value["verdict_ref"] = verdict_ref
    if policy_ref is not None:
        value["policy_ref"] = policy_ref
    return _validated_record(value)
