# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Cross-record integrity validation and deterministic epistemic-state replay."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, cast

import rfc8785

from .state import (
    ActorRole,
    ActorType,
    ClaimStatus,
    TransitionError,
    TransitionRequest,
    validate_transition,
)
from .types import JsonObject
from .validation import ProtocolValidator


class IntegrityCode(StrEnum):
    """Stable identifiers for deterministic bundle-integrity failures."""

    SCHEMA_INVALID = "schema_invalid"
    DUPLICATE_ID = "duplicate_id"
    UNRESOLVED_REFERENCE = "unresolved_reference"
    REFERENCE_KIND_MISMATCH = "reference_kind_mismatch"
    REFERENCE_NOT_AVAILABLE = "reference_not_available"
    SUBJECT_NOT_AVAILABLE = "subject_not_available"
    STATE_MISMATCH = "state_mismatch"
    INVALID_TRANSITION = "invalid_transition"
    EVIDENCE_RELATION_MISSING = "evidence_relation_missing"
    TRANSITION_SUBJECT_MISMATCH = "transition_subject_mismatch"
    VERDICT_MISMATCH = "verdict_mismatch"
    SCOPE_MISMATCH = "scope_mismatch"


@dataclass(frozen=True, slots=True)
class IntegrityIssue:
    """One deterministic integrity violation in a protocol-record bundle."""

    code: IntegrityCode
    record_id: str
    message: str
    reference: str | None = None

    def __str__(self) -> str:
        reference = f" [{self.reference}]" if self.reference is not None else ""
        return f"{self.code.value}: {self.record_id}{reference}: {self.message}"


@dataclass(frozen=True, slots=True)
class BundleSnapshot:
    """Deterministic state derived by replaying an ordered valid bundle."""

    record_ids: tuple[str, ...]
    current_states: Mapping[str, ClaimStatus]

    @property
    def record_count(self) -> int:
        """Return the number of records in the replayed bundle."""

        return len(self.record_ids)


class BundleIntegrityError(ValueError):
    """Raised when records are valid individually but incoherent as a bundle."""

    def __init__(self, issues: Sequence[IntegrityIssue]) -> None:
        self.issues: tuple[IntegrityIssue, ...] = tuple(issues)
        super().__init__("\n".join(str(issue) for issue in self.issues))


@dataclass(frozen=True, slots=True)
class _ReferenceField:
    name: str
    allowed_kinds: frozenset[str]
    allow_external: bool = True


_ARTIFACT: Final = frozenset({"artifact_reference"})
_CLAIM_OR_HYPOTHESIS: Final = frozenset({"claim", "hypothesis"})
_EVIDENCE: Final = frozenset({"evidence"})
_PREDICTION: Final = frozenset({"prediction"})
_TEST: Final = frozenset({"test"})
_TRANSITION: Final = frozenset({"status_transition"})
_VERDICT: Final = frozenset({"verdict"})
_ANY_RECORD: Final = frozenset(
    {
        "artifact_reference",
        "claim",
        "hypothesis",
        "prediction",
        "test",
        "evidence",
        "verdict",
        "decision",
        "status_transition",
    }
)

_REFERENCE_FIELDS: Final[dict[str, tuple[_ReferenceField, ...]]] = {
    "artifact_reference": (_ReferenceField("derived_from_refs", _ARTIFACT),),
    "claim": (
        _ReferenceField("evidence_refs", _EVIDENCE),
        _ReferenceField("contradicting_evidence_refs", _EVIDENCE),
        _ReferenceField("dependency_refs", _ANY_RECORD),
        _ReferenceField("transition_refs", _TRANSITION),
    ),
    "hypothesis": (
        _ReferenceField("explains_evidence_refs", _EVIDENCE),
        _ReferenceField("alternative_hypothesis_refs", frozenset({"hypothesis"})),
        _ReferenceField("prediction_refs", _PREDICTION),
        _ReferenceField("dependency_refs", _ANY_RECORD),
    ),
    "prediction": (
        _ReferenceField("hypothesis_ref", frozenset({"hypothesis"})),
        _ReferenceField("test_ref", _TEST),
    ),
    "test": (
        _ReferenceField("subject_refs", _CLAIM_OR_HYPOTHESIS),
        _ReferenceField("prediction_refs", _PREDICTION),
        _ReferenceField("plan_ref", _TEST),
        _ReferenceField("evidence_refs", _EVIDENCE),
    ),
    "evidence": (
        _ReferenceField("artifact_refs", _ARTIFACT),
        _ReferenceField("test_ref", _TEST),
    ),
    "verdict": (
        _ReferenceField("subject_ref", _CLAIM_OR_HYPOTHESIS),
        _ReferenceField("evidence_refs", _EVIDENCE),
        _ReferenceField("policy_ref", _ARTIFACT),
    ),
    "decision": (
        _ReferenceField("policy_ref", _ARTIFACT),
        _ReferenceField("relied_on_claim_refs", frozenset({"claim"})),
        _ReferenceField("unresolved_claim_refs", frozenset({"claim"})),
    ),
    "status_transition": (
        _ReferenceField("subject_ref", _CLAIM_OR_HYPOTHESIS, allow_external=False),
        _ReferenceField("evidence_refs", _EVIDENCE, allow_external=False),
        _ReferenceField("verdict_ref", _VERDICT, allow_external=False),
        _ReferenceField("policy_ref", _ARTIFACT, allow_external=False),
    ),
}


def _record_id(record: JsonObject, position: int) -> str:
    value = record.get("id")
    return value if isinstance(value, str) else f"@record[{position}]"


def _required_string(record: JsonObject, field: str) -> str:
    value = record[field]
    if not isinstance(value, str):
        raise TypeError(f"schema-valid field {field!r} was not a string")
    return value


def _optional_string(record: JsonObject, field: str) -> str | None:
    value = record.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"schema-valid field {field!r} was not a string")
    return value


def _object(record: JsonObject, field: str) -> JsonObject:
    value = record[field]
    if not isinstance(value, dict):
        raise TypeError(f"schema-valid field {field!r} was not an object")
    return value


def _objects(record: JsonObject, field: str) -> tuple[JsonObject, ...]:
    value = record[field]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError(f"schema-valid field {field!r} was not an object array")
    return tuple(cast(JsonObject, item) for item in value)


def _references(record: JsonObject, field: str) -> tuple[str, ...]:
    value = record.get(field)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(cast(str, item) for item in value)
    raise TypeError(
        f"schema-valid field {field!r} was not a reference or reference array"
    )


class BundleValidator:
    """Validate references and replay state across ordered protocol records."""

    def __init__(self, protocol_validator: ProtocolValidator | None = None) -> None:
        self._protocol_validator = protocol_validator or ProtocolValidator()

    def errors(
        self,
        records: Iterable[JsonObject],
        *,
        external_refs: Collection[str] = (),
    ) -> list[IntegrityIssue]:
        """Return every deterministic integrity issue found in an ordered bundle."""

        _, issues = self._analyze(tuple(records), frozenset(external_refs))
        return issues

    def validate(
        self,
        records: Iterable[JsonObject],
        *,
        external_refs: Collection[str] = (),
    ) -> BundleSnapshot:
        """Return replayed state or raise BundleIntegrityError for an invalid bundle."""

        snapshot, issues = self._analyze(tuple(records), frozenset(external_refs))
        if issues:
            raise BundleIntegrityError(issues)
        if snapshot is None:
            raise RuntimeError("bundle analysis produced neither a snapshot nor issues")
        return snapshot

    def _analyze(
        self,
        records: tuple[JsonObject, ...],
        external_refs: frozenset[str],
    ) -> tuple[BundleSnapshot | None, list[IntegrityIssue]]:
        issues = self._schema_issues(records)
        if issues:
            return None, issues

        duplicate_issues = self._duplicate_issues(records)
        if duplicate_issues:
            return None, duplicate_issues

        index = {_required_string(record, "id"): record for record in records}
        positions = {
            _required_string(record, "id"): position
            for position, record in enumerate(records)
        }
        issues.extend(self._reference_issues(records, index, external_refs))
        issues.extend(self._verdict_issues(records, index, positions))
        states, replay_issues = self._replay(records, index, positions)
        issues.extend(replay_issues)

        snapshot = BundleSnapshot(
            record_ids=tuple(_required_string(record, "id") for record in records),
            current_states=MappingProxyType(states),
        )
        return snapshot, issues

    def _schema_issues(self, records: tuple[JsonObject, ...]) -> list[IntegrityIssue]:
        issues: list[IntegrityIssue] = []
        for position, record in enumerate(records):
            for message in self._protocol_validator.errors(record):
                issues.append(
                    IntegrityIssue(
                        code=IntegrityCode.SCHEMA_INVALID,
                        record_id=_record_id(record, position),
                        message=message,
                    )
                )
        return issues

    @staticmethod
    def _duplicate_issues(records: tuple[JsonObject, ...]) -> list[IntegrityIssue]:
        first_positions: dict[str, int] = {}
        issues: list[IntegrityIssue] = []
        for position, record in enumerate(records):
            record_id = _required_string(record, "id")
            first_position = first_positions.setdefault(record_id, position)
            if first_position != position:
                issues.append(
                    IntegrityIssue(
                        code=IntegrityCode.DUPLICATE_ID,
                        record_id=record_id,
                        message=(
                            f"duplicates the record at position {first_position}; "
                            f"encountered again at position {position}"
                        ),
                    )
                )
        return issues

    def _reference_issues(
        self,
        records: tuple[JsonObject, ...],
        index: Mapping[str, JsonObject],
        external_refs: frozenset[str],
    ) -> list[IntegrityIssue]:
        issues: list[IntegrityIssue] = []
        for record in records:
            record_id = _required_string(record, "id")
            kind = _required_string(record, "kind")
            for spec in _REFERENCE_FIELDS[kind]:
                for reference in _references(record, spec.name):
                    issues.extend(
                        self._check_reference(
                            record_id,
                            reference,
                            spec,
                            index,
                            external_refs,
                        )
                    )

            if kind == "evidence":
                for relation in _objects(record, "relations"):
                    reference = _required_string(relation, "subject_ref")
                    issues.extend(
                        self._check_reference(
                            record_id,
                            reference,
                            _ReferenceField(
                                "relations.subject_ref", _CLAIM_OR_HYPOTHESIS
                            ),
                            index,
                            external_refs,
                        )
                    )

            if kind in {"claim", "hypothesis"}:
                for transition_ref in _references(record, "transition_refs"):
                    transition = index.get(transition_ref)
                    if (
                        transition is not None
                        and _required_string(transition, "kind") == "status_transition"
                        and _required_string(transition, "subject_ref") != record_id
                    ):
                        issues.append(
                            IntegrityIssue(
                                code=IntegrityCode.TRANSITION_SUBJECT_MISMATCH,
                                record_id=record_id,
                                reference=transition_ref,
                                message="referenced transition targets a different subject",
                            )
                        )
        return issues

    @staticmethod
    def _check_reference(
        record_id: str,
        reference: str,
        spec: _ReferenceField,
        index: Mapping[str, JsonObject],
        external_refs: frozenset[str],
    ) -> list[IntegrityIssue]:
        target = index.get(reference)
        if target is None:
            if reference in external_refs and spec.allow_external:
                return []
            return [
                IntegrityIssue(
                    code=IntegrityCode.UNRESOLVED_REFERENCE,
                    record_id=record_id,
                    reference=reference,
                    message=f"{spec.name} does not resolve within the bundle",
                )
            ]
        target_kind = _required_string(target, "kind")
        if target_kind not in spec.allowed_kinds:
            allowed = ", ".join(sorted(spec.allowed_kinds))
            return [
                IntegrityIssue(
                    code=IntegrityCode.REFERENCE_KIND_MISMATCH,
                    record_id=record_id,
                    reference=reference,
                    message=(
                        f"{spec.name} targets {target_kind}; expected one of: {allowed}"
                    ),
                )
            ]
        return []

    @staticmethod
    def _scope(record: JsonObject) -> JsonObject:
        return _object(record, "scope")

    @staticmethod
    def _same_json(left: JsonObject, right: JsonObject) -> bool:
        return rfc8785.dumps(left) == rfc8785.dumps(right)

    def _verdict_issues(
        self,
        records: tuple[JsonObject, ...],
        index: Mapping[str, JsonObject],
        positions: Mapping[str, int],
    ) -> list[IntegrityIssue]:
        issues: list[IntegrityIssue] = []
        for position, verdict in enumerate(records):
            if _required_string(verdict, "kind") != "verdict":
                continue

            verdict_id = _required_string(verdict, "id")
            subject_ref = _required_string(verdict, "subject_ref")
            evidence_refs = _references(verdict, "evidence_refs")
            policy_ref = _optional_string(verdict, "policy_ref")
            material_refs = (subject_ref,) + evidence_refs
            if policy_ref is not None:
                material_refs += (policy_ref,)
            for reference in material_refs:
                reference_position = positions.get(reference)
                if reference_position is not None and reference_position >= position:
                    issues.append(
                        IntegrityIssue(
                            code=IntegrityCode.REFERENCE_NOT_AVAILABLE,
                            record_id=verdict_id,
                            reference=reference,
                            message="verdict material must appear before the verdict",
                        )
                    )

            subject = index.get(subject_ref)
            if (
                subject is None
                or _required_string(subject, "kind") not in _CLAIM_OR_HYPOTHESIS
            ):
                continue
            verdict_scope = self._scope(verdict)
            if not self._same_json(verdict_scope, self._scope(subject)):
                issues.append(
                    IntegrityIssue(
                        code=IntegrityCode.SCOPE_MISMATCH,
                        record_id=verdict_id,
                        reference=subject_ref,
                        message="verdict scope must exactly match its subject scope",
                    )
                )

            outcome = ClaimStatus(_required_string(verdict, "outcome"))
            observed_relations: set[str] = set()
            for evidence_ref in evidence_refs:
                evidence = index.get(evidence_ref)
                if evidence is None or _required_string(evidence, "kind") != "evidence":
                    continue
                if not self._same_json(self._scope(evidence), verdict_scope):
                    issues.append(
                        IntegrityIssue(
                            code=IntegrityCode.SCOPE_MISMATCH,
                            record_id=verdict_id,
                            reference=evidence_ref,
                            message=(
                                "verdict evidence scope must exactly match "
                                "the verdict scope"
                            ),
                        )
                    )
                relations = {
                    _required_string(relation, "relation")
                    for relation in _objects(evidence, "relations")
                    if _required_string(relation, "subject_ref") == subject_ref
                }
                if not relations:
                    issues.append(
                        IntegrityIssue(
                            code=IntegrityCode.EVIDENCE_RELATION_MISSING,
                            record_id=verdict_id,
                            reference=evidence_ref,
                            message=f"verdict evidence does not relate to {subject_ref}",
                        )
                    )
                    continue
                observed_relations.update(relations)
                expected_relation = {
                    ClaimStatus.SUPPORTED: "supports",
                    ClaimStatus.CORROBORATED_WITHIN_SCOPE: "supports",
                    ClaimStatus.REFUTED: "contradicts",
                }.get(outcome)
                if expected_relation is not None and expected_relation not in relations:
                    issues.append(
                        IntegrityIssue(
                            code=IntegrityCode.EVIDENCE_RELATION_MISSING,
                            record_id=verdict_id,
                            reference=evidence_ref,
                            message=(
                                f"{outcome.value} verdict requires evidence "
                                f"that {expected_relation} the subject"
                            ),
                        )
                    )

            if outcome is ClaimStatus.MIXED_EVIDENCE and not {
                "supports",
                "contradicts",
            }.issubset(observed_relations):
                issues.append(
                    IntegrityIssue(
                        code=IntegrityCode.EVIDENCE_RELATION_MISSING,
                        record_id=verdict_id,
                        reference=subject_ref,
                        message=(
                            "mixed_evidence verdict requires both supporting "
                            "and contradicting evidence relations"
                        ),
                    )
                )
        return issues

    def _replay(
        self,
        records: tuple[JsonObject, ...],
        index: Mapping[str, JsonObject],
        positions: Mapping[str, int],
    ) -> tuple[dict[str, ClaimStatus], list[IntegrityIssue]]:
        states: dict[str, ClaimStatus] = {}
        issues: list[IntegrityIssue] = []

        for position, record in enumerate(records):
            kind = _required_string(record, "kind")
            record_id = _required_string(record, "id")
            if kind in {"claim", "hypothesis"}:
                states[record_id] = ClaimStatus(_required_string(record, "status"))
                continue
            if kind != "status_transition":
                continue

            subject_ref = _required_string(record, "subject_ref")
            current_status = states.get(subject_ref)
            can_apply = True
            if current_status is None:
                issues.append(
                    IntegrityIssue(
                        code=IntegrityCode.SUBJECT_NOT_AVAILABLE,
                        record_id=record_id,
                        reference=subject_ref,
                        message="transition subject has not appeared earlier in the bundle",
                    )
                )
                can_apply = False

            from_status = ClaimStatus(_required_string(record, "from_status"))
            to_status = ClaimStatus(_required_string(record, "to_status"))
            if current_status is not None and from_status is not current_status:
                issues.append(
                    IntegrityIssue(
                        code=IntegrityCode.STATE_MISMATCH,
                        record_id=record_id,
                        reference=subject_ref,
                        message=(
                            f"declares from_status={from_status.value}, but replayed state "
                            f"is {current_status.value}"
                        ),
                    )
                )
                can_apply = False

            evidence_refs = _references(record, "evidence_refs")
            verdict_ref = _optional_string(record, "verdict_ref")
            policy_ref = _optional_string(record, "policy_ref")
            available_refs = evidence_refs
            if verdict_ref is not None:
                available_refs += (verdict_ref,)
            if policy_ref is not None:
                available_refs += (policy_ref,)
            for reference in available_refs:
                reference_position = positions.get(reference)
                if reference_position is not None and reference_position >= position:
                    issues.append(
                        IntegrityIssue(
                            code=IntegrityCode.REFERENCE_NOT_AVAILABLE,
                            record_id=record_id,
                            reference=reference,
                            message="authorizing material must appear before the transition",
                        )
                    )
                    can_apply = False

            authorized_by = _object(record, "authorized_by")
            request = TransitionRequest(
                subject_ref=subject_ref,
                from_status=from_status,
                to_status=to_status,
                authorized_by=_required_string(authorized_by, "id"),
                actor_type=ActorType(_required_string(authorized_by, "actor_type")),
                actor_role=ActorRole(_required_string(authorized_by, "role")),
                evidence_refs=evidence_refs,
                verdict_ref=verdict_ref,
                policy_ref=policy_ref,
            )
            try:
                validate_transition(request)
            except TransitionError as error:
                issues.append(
                    IntegrityIssue(
                        code=IntegrityCode.INVALID_TRANSITION,
                        record_id=record_id,
                        reference=subject_ref,
                        message=str(error),
                    )
                )
                can_apply = False

            if verdict_ref is not None:
                verdict = index.get(verdict_ref)
                if verdict is None or _required_string(verdict, "kind") != "verdict":
                    can_apply = False
                else:
                    verdict_evidence = frozenset(_references(verdict, "evidence_refs"))
                    mismatches: list[str] = []
                    if _required_string(verdict, "subject_ref") != subject_ref:
                        mismatches.append("subject")
                    if _required_string(verdict, "outcome") != to_status.value:
                        mismatches.append("outcome")
                    if verdict_evidence != frozenset(evidence_refs):
                        mismatches.append("evidence")
                    if _optional_string(verdict, "policy_ref") != policy_ref:
                        mismatches.append("policy")
                    if mismatches:
                        issues.append(
                            IntegrityIssue(
                                code=IntegrityCode.VERDICT_MISMATCH,
                                record_id=record_id,
                                reference=verdict_ref,
                                message=(
                                    "transition does not match verdict fields: "
                                    + ", ".join(mismatches)
                                ),
                            )
                        )
                        can_apply = False

            if can_apply:
                states[subject_ref] = to_status

        return states, issues
