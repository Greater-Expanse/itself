# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Structured, provider-neutral projections of validated ledger state."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from .ledger import Ledger
from .state import ClaimStatus
from .types import JsonObject


class SubjectKind(StrEnum):
    """Record kinds whose epistemic state can be replayed."""

    CLAIM = "claim"
    HYPOTHESIS = "hypothesis"


@dataclass(frozen=True, slots=True)
class SubjectReplay:
    """Initial and replayed state for one claim or hypothesis."""

    subject_id: str
    kind: SubjectKind
    initial_status: ClaimStatus
    current_status: ClaimStatus
    transition_count: int

    def to_json_object(self) -> JsonObject:
        """Return a JSON-compatible structured representation."""

        return {
            "subject_id": self.subject_id,
            "kind": self.kind.value,
            "initial_status": self.initial_status.value,
            "current_status": self.current_status.value,
            "transition_count": self.transition_count,
        }


@dataclass(frozen=True, slots=True)
class LedgerSummary:
    """Aggregate record and current-state counts for one valid ledger."""

    record_count: int
    record_counts: Mapping[str, int]
    state_counts: Mapping[ClaimStatus, int]
    subjects: tuple[SubjectReplay, ...]

    @property
    def subject_count(self) -> int:
        """Return the number of replayable claims and hypotheses."""

        return len(self.subjects)

    def to_json_object(self) -> JsonObject:
        """Return a JSON-compatible structured representation."""

        return {
            "record_count": self.record_count,
            "subject_count": self.subject_count,
            "record_counts": dict(self.record_counts),
            "state_counts": {
                status.value: count for status, count in self.state_counts.items()
            },
            "subjects": [subject.to_json_object() for subject in self.subjects],
        }


def _string(record: JsonObject, field: str) -> str:
    value = record[field]
    if not isinstance(value, str):
        raise TypeError(f"validated field {field!r} was not a string")
    return value


def replay_subjects(ledger: Ledger) -> tuple[SubjectReplay, ...]:
    """Project claims and hypotheses in base-record order after state replay."""

    records = ledger.records
    transition_counts = Counter(
        _string(record, "subject_ref")
        for record in records
        if _string(record, "kind") == "status_transition"
    )
    subjects: list[SubjectReplay] = []
    for record in records:
        kind_value = _string(record, "kind")
        if kind_value not in {SubjectKind.CLAIM.value, SubjectKind.HYPOTHESIS.value}:
            continue
        subject_id = _string(record, "id")
        subjects.append(
            SubjectReplay(
                subject_id=subject_id,
                kind=SubjectKind(kind_value),
                initial_status=ClaimStatus(_string(record, "status")),
                current_status=ledger.snapshot.current_states[subject_id],
                transition_count=transition_counts[subject_id],
            )
        )
    return tuple(subjects)


def summarize_ledger(ledger: Ledger) -> LedgerSummary:
    """Return deterministic aggregate counts and subject replay projections."""

    records = ledger.records
    subjects = replay_subjects(ledger)
    record_counts = Counter(_string(record, "kind") for record in records)
    state_counts = Counter(subject.current_status for subject in subjects)
    return LedgerSummary(
        record_count=len(records),
        record_counts=MappingProxyType(dict(sorted(record_counts.items()))),
        state_counts=MappingProxyType(
            dict(sorted(state_counts.items(), key=lambda item: item[0].value))
        ),
        subjects=subjects,
    )
