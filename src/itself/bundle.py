# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Cross-record integrity validation and deterministic epistemic-state replay."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, cast

import rfc8785

from ._formats import RFC3339_DATE_TIME
from .state import (
    EVIDENCE_BACKED_STATUSES,
    ActorRole,
    ActorType,
    ClaimStatus,
    TransitionError,
    TransitionRequest,
    validate_transition,
)

# JsonValue is unused here, but typing.get_type_hints needs it to resolve the
# recursive JsonObject alias in this module's annotations.
from .types import (
    JsonObject,
    JsonValue,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)
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
    UNTRUSTED_AUTHORIZER = "untrusted_authorizer"
    ACTOR_TYPE_CONFLICT = "actor_type_conflict"
    UNSUPPORTED_RELIANCE = "unsupported_reliance"
    EXPIRED_SUPPORT = "expired_support"


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
    # Records reach the schema check unchecked, so one may not be an object.
    runtime_record = cast(object, record)
    value = (
        cast(JsonObject, runtime_record).get("id")
        if isinstance(runtime_record, dict)
        else None
    )
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


def _checked_prefix(value: int, record_count: int) -> int:
    runtime_value = cast(object, value)
    if (
        isinstance(runtime_value, bool)
        or not isinstance(runtime_value, int)
        or not 0 <= runtime_value <= record_count
    ):
        raise ValueError(
            f"a schema-checked prefix must be an integer from 0 to {record_count}"
        )
    return runtime_value


def _trusted_authorizers(value: object) -> frozenset[str]:
    if isinstance(value, str) or not isinstance(value, Collection):
        raise ValueError("trusted_authorizers must be a collection of actor ids")
    authorizers = frozenset(cast(Collection[object], value))
    if not all(
        isinstance(actor_id, str) and actor_id.strip() for actor_id in authorizers
    ):
        raise ValueError("trusted_authorizers must contain non-empty actor ids")
    return cast(frozenset[str], authorizers)


_SUPPORTED_STATUSES: Final = frozenset(
    {ClaimStatus.SUPPORTED, ClaimStatus.CORROBORATED_WITHIN_SCOPE}
)
_RELYING_DISPOSITIONS: Final = frozenset({"approved", "executed"})
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND: Final = datetime.resolution


def _days_from_civil(year: int, month: int, day: int) -> int:
    # Days since 1970-01-01 in the proleptic Gregorian calendar, for any year
    # RFC 3339 allows, including those datetime cannot represent.
    if month <= 2:
        year -= 1
    era = year // 400
    year_of_era = year - era * 400
    day_of_year = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    day_of_era = year_of_era * 365 + year_of_era // 4 - year_of_era // 100
    return era * 146_097 + day_of_era + day_of_year - 719_468


def _at_or_before(value: str, field: str, instant: datetime) -> bool:
    """Return whether an RFC 3339 date-time is at or before ``instant``."""

    match = RFC3339_DATE_TIME.fullmatch(value)
    if match is None:
        raise TypeError(f"schema-valid field {field!r} was not an RFC 3339 date-time")
    year, month, day, hour, minute, second = (
        int(match[group]) for group in range(1, 7)
    )
    fraction = match[7] or ""
    microsecond = int(fraction[:6].ljust(6, "0"))
    # A fraction beyond microseconds, or a leap second, puts the value after
    # the microsecond it is truncated to.
    later = fraction[6:].strip("0") != ""
    if second == 60:
        second, microsecond, later = 59, 999_999, True
    offset_minutes = 0
    if match[8] is not None:
        sign = 1 if match[8] == "+" else -1
        offset_minutes = sign * (int(match[9]) * 60 + int(match[10]))
    seconds = (
        (_days_from_civil(year, month, day) * 24 + hour) * 60 + minute - offset_minutes
    ) * 60 + second
    value_microseconds = seconds * 1_000_000 + microsecond
    instant_microseconds = (instant - _EPOCH) // _MICROSECOND
    if later:
        return value_microseconds < instant_microseconds
    return value_microseconds <= instant_microseconds


def _actors(record: JsonObject) -> tuple[JsonObject, ...]:
    """Return every actor object a schema-valid record declares, in field order."""

    actors = [_object(record, "created_by")]
    if _required_string(record, "kind") in {"decision", "status_transition"}:
        actors.append(_object(record, "authorized_by"))
    return tuple(actors)


@dataclass(frozen=True, slots=True)
class ValidationPolicy:
    """Opt-in checks of the reference validators beyond ``v0alpha2`` conformance.

    The protocol leaves these questions to the deployment, so an empty policy,
    the default, applies none of them, and a policy never changes what a
    conforming validator must accept.

    ``trusted_authorizers`` lists the only actor ids that may authorize a
    transition into or out of an evidence-backed status; identity is
    established outside the protocol.  With a list, an actor id declared with
    more than one ``actor_type`` is also refused.  ``require_supported_reliance``
    refuses an approved or executed decision that relies on a subject whose
    replayed status at the decision is not supported or corroborated within
    scope; ``relied_on_claim_refs`` is optional, so a decision that names no
    claims is not refused.  ``as_of``, a timezone-aware datetime, refuses a
    supported subject whose scope, or the evidence behind its current status,
    has expired by then.
    """

    trusted_authorizers: Collection[str] | None = None
    require_supported_reliance: bool = False
    as_of: datetime | None = None

    def __post_init__(self) -> None:
        if self.trusted_authorizers is not None:
            object.__setattr__(
                self,
                "trusted_authorizers",
                _trusted_authorizers(self.trusted_authorizers),
            )
        if not isinstance(cast(object, self.require_supported_reliance), bool):
            raise ValueError("require_supported_reliance must be a boolean")
        as_of = cast(object, self.as_of)
        if as_of is not None and (
            not isinstance(as_of, datetime)
            or as_of.tzinfo is None
            or as_of.utcoffset() is None
        ):
            raise ValueError("as_of must be a timezone-aware datetime")


def _policy(
    trusted_authorizers: Collection[str] | None,
    policy: ValidationPolicy | None,
) -> ValidationPolicy:
    if policy is not None:
        if trusted_authorizers is not None:
            raise ValueError("pass trusted_authorizers or policy, not both")
        if not isinstance(cast(object, policy), ValidationPolicy):
            raise ValueError("policy must be a ValidationPolicy")
        return policy
    return ValidationPolicy(trusted_authorizers=trusted_authorizers)


class BundleValidator:
    """Validate references and replay state across ordered protocol records.

    Records declare their own actors, so by default any identifier may
    authorize an evidence-backed transition if its declared type and role
    allow it.  ``policy`` adds the opt-in checks of ``ValidationPolicy``, such
    as a list of trusted authorizers; ``trusted_authorizers`` is shorthand for
    a policy with only that list, and the two cannot be combined.
    """

    def __init__(
        self,
        protocol_validator: ProtocolValidator | None = None,
        *,
        trusted_authorizers: Collection[str] | None = None,
        policy: ValidationPolicy | None = None,
    ) -> None:
        self._protocol_validator = protocol_validator or ProtocolValidator()
        self._policy = _policy(trusted_authorizers, policy)
        self._trusted_authorizers = cast(
            frozenset[str] | None, self._policy.trusted_authorizers
        )

    @property
    def trusted_authorizers(self) -> frozenset[str] | None:
        """The only actor ids allowed to authorize evidence-backed transitions."""

        return self._trusted_authorizers

    @property
    def policy(self) -> ValidationPolicy:
        """The opt-in checks this validator applies; empty unless one was given."""

        return self._policy

    def errors(
        self,
        records: Iterable[JsonObject],
        *,
        external_refs: Collection[str] = (),
    ) -> list[IntegrityIssue]:
        """Return every deterministic integrity issue found in an ordered bundle."""

        _, issues = self._analyze(tuple(records), frozenset(external_refs), 0)
        return issues

    def validate(
        self,
        records: Iterable[JsonObject],
        *,
        external_refs: Collection[str] = (),
    ) -> BundleSnapshot:
        """Return replayed state or raise BundleIntegrityError for an invalid bundle."""

        return self._validated(tuple(records), frozenset(external_refs), 0)

    def _validated(
        self,
        records: tuple[JsonObject, ...],
        external_refs: frozenset[str],
        schema_checked: int,
    ) -> BundleSnapshot:
        # Only Ledger passes a nonzero ``schema_checked``: its held records are
        # private copies this validator already accepted, so they skip only
        # the per-record schema check, and every cross-record check still
        # covers the whole sequence.
        snapshot, issues = self._analyze(
            records,
            external_refs,
            _checked_prefix(schema_checked, len(records)),
        )
        if issues:
            raise BundleIntegrityError(issues)
        if snapshot is None:
            raise RuntimeError("bundle analysis produced neither a snapshot nor issues")
        return snapshot

    def _analyze(
        self,
        records: tuple[JsonObject, ...],
        external_refs: frozenset[str],
        schema_checked_prefix: int,
    ) -> tuple[BundleSnapshot | None, list[IntegrityIssue]]:
        issues = self._schema_issues(records, schema_checked_prefix)
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
        states, producers, replay_issues = self._replay(
            records, index, positions, external_refs
        )
        issues.extend(replay_issues)
        if self._trusted_authorizers is not None:
            issues.extend(self._actor_type_issues(records))
        if self._policy.as_of is not None:
            issues.extend(
                self._expired_support_issues(
                    states, producers, index, self._policy.as_of
                )
            )

        snapshot = BundleSnapshot(
            record_ids=tuple(_required_string(record, "id") for record in records),
            current_states=MappingProxyType(states),
        )
        return snapshot, issues

    def _schema_issues(
        self,
        records: tuple[JsonObject, ...],
        start: int,
    ) -> list[IntegrityIssue]:
        issues: list[IntegrityIssue] = []
        for position, record in enumerate(records[start:], start):
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
        external_refs: frozenset[str],
    ) -> tuple[dict[str, ClaimStatus], dict[str, JsonObject], list[IntegrityIssue]]:
        """Replay states, and name the transition behind each subject's state."""

        states: dict[str, ClaimStatus] = {}
        producers: dict[str, JsonObject] = {}
        issues: list[IntegrityIssue] = []

        for position, record in enumerate(records):
            kind = _required_string(record, "kind")
            record_id = _required_string(record, "id")
            if kind in {"claim", "hypothesis"}:
                states[record_id] = ClaimStatus(_required_string(record, "status"))
                continue
            if kind == "decision" and self._policy.require_supported_reliance:
                issues.extend(
                    self._reliance_issues(record, states, index, external_refs)
                )
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
            if (
                self._trusted_authorizers is not None
                and request.authorized_by not in self._trusted_authorizers
            ):
                if to_status in EVIDENCE_BACKED_STATUSES:
                    message = (
                        "evidence-backed transition is authorized by an actor "
                        "that is not trusted to authorize it"
                    )
                elif from_status in EVIDENCE_BACKED_STATUSES:
                    message = (
                        "transition out of an evidence-backed status is authorized "
                        "by an actor that is not trusted to authorize it"
                    )
                else:
                    message = None
                if message is not None:
                    issues.append(
                        IntegrityIssue(
                            code=IntegrityCode.UNTRUSTED_AUTHORIZER,
                            record_id=record_id,
                            reference=request.authorized_by,
                            message=message,
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
                producers[subject_ref] = record

        return states, producers, issues

    @staticmethod
    def _reliance_issues(
        decision: JsonObject,
        states: Mapping[str, ClaimStatus],
        index: Mapping[str, JsonObject],
        external_refs: frozenset[str],
    ) -> list[IntegrityIssue]:
        disposition = _required_string(decision, "disposition")
        if disposition not in _RELYING_DISPOSITIONS:
            return []
        issues: list[IntegrityIssue] = []
        for reference in _references(decision, "relied_on_claim_refs"):
            if reference not in index and reference in external_refs:
                continue
            status = states.get(reference)
            if status in _SUPPORTED_STATUSES:
                continue
            replayed = (
                "has no replayed status before the decision"
                if status is None
                else f"has replayed status {status.value} at the decision"
            )
            issues.append(
                IntegrityIssue(
                    code=IntegrityCode.UNSUPPORTED_RELIANCE,
                    record_id=_required_string(decision, "id"),
                    reference=reference,
                    message=f"{disposition} decision relies on a subject that {replayed}",
                )
            )
        return issues

    @staticmethod
    def _actor_type_issues(records: tuple[JsonObject, ...]) -> list[IntegrityIssue]:
        first_types: dict[str, str] = {}
        conflicting: set[str] = set()
        issues: list[IntegrityIssue] = []
        for record in records:
            for actor in _actors(record):
                actor_id = _required_string(actor, "id")
                actor_type = _required_string(actor, "actor_type")
                first_type = first_types.setdefault(actor_id, actor_type)
                if first_type == actor_type or actor_id in conflicting:
                    continue
                conflicting.add(actor_id)
                issues.append(
                    IntegrityIssue(
                        code=IntegrityCode.ACTOR_TYPE_CONFLICT,
                        record_id=_required_string(record, "id"),
                        reference=actor_id,
                        message=(
                            f"actor is declared as {actor_type} here but as "
                            f"{first_type} earlier in the bundle"
                        ),
                    )
                )
        return issues

    @staticmethod
    def _expired_support_issues(
        states: Mapping[str, ClaimStatus],
        producers: Mapping[str, JsonObject],
        index: Mapping[str, JsonObject],
        as_of: datetime,
    ) -> list[IntegrityIssue]:
        issues: list[IntegrityIssue] = []
        for subject_id, status in states.items():
            if status not in _SUPPORTED_STATUSES:
                continue
            valid_until = _optional_string(
                _object(index[subject_id], "scope"), "valid_until"
            )
            if valid_until is not None and _at_or_before(
                valid_until, "scope.valid_until", as_of
            ):
                issues.append(
                    IntegrityIssue(
                        code=IntegrityCode.EXPIRED_SUPPORT,
                        record_id=subject_id,
                        message=(
                            f"{status.value} status has expired: scope valid_until "
                            f"{valid_until} is at or before {as_of.isoformat()}"
                        ),
                    )
                )
            producer = producers.get(subject_id)
            if producer is None:
                continue
            for evidence_ref in _references(producer, "evidence_refs"):
                evidence = index.get(evidence_ref)
                if evidence is None or _required_string(evidence, "kind") != "evidence":
                    continue
                expiries = (
                    ("expires_at", _optional_string(evidence, "expires_at")),
                    (
                        "scope valid_until",
                        _optional_string(_object(evidence, "scope"), "valid_until"),
                    ),
                )
                for label, expiry in expiries:
                    if expiry is not None and _at_or_before(expiry, label, as_of):
                        issues.append(
                            IntegrityIssue(
                                code=IntegrityCode.EXPIRED_SUPPORT,
                                record_id=subject_id,
                                reference=evidence_ref,
                                message=(
                                    f"{status.value} status rests on evidence whose "
                                    f"{label} {expiry} is at or before "
                                    f"{as_of.isoformat()}"
                                ),
                            )
                        )
        return issues
