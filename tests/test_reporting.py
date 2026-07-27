# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from itself import (
    ClaimStatus,
    JsonObject,
    JsonValue,
    Ledger,
    SubjectKind,
    replay_subjects,
    summarize_ledger,
)

ROOT = Path(__file__).resolve().parents[1]
VALID_HISTORY = (
    ROOT / "conformance" / "bundles" / "valid" / "evidence-backed-history.json"
)


def _ledger() -> Ledger:
    value = cast(JsonValue, json.loads(VALID_HISTORY.read_text(encoding="utf-8")))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError("valid history fixture must contain a JSON object array")
    return Ledger(cast(list[JsonObject], value))


def test_replay_projects_subjects_in_base_record_order() -> None:
    replay = replay_subjects(_ledger())

    assert len(replay) == 1
    assert replay[0].subject_id == "claim-cache-cause"
    assert replay[0].kind is SubjectKind.CLAIM
    assert replay[0].initial_status is ClaimStatus.PROPOSED
    assert replay[0].current_status is ClaimStatus.CORROBORATED_WITHIN_SCOPE
    assert replay[0].transition_count == 4


def test_summary_separates_record_and_current_state_counts() -> None:
    summary = summarize_ledger(_ledger())

    assert summary.record_count == 10
    assert summary.subject_count == 1
    assert summary.record_counts == {
        "artifact_reference": 1,
        "claim": 1,
        "evidence": 2,
        "status_transition": 4,
        "verdict": 2,
    }
    assert summary.state_counts == {ClaimStatus.CORROBORATED_WITHIN_SCOPE: 1}


def test_summary_json_projection_uses_wire_values() -> None:
    projection = summarize_ledger(_ledger()).to_json_object()

    assert projection["state_counts"] == {"corroborated_within_scope": 1}
    assert projection["subjects"] == [
        {
            "subject_id": "claim-cache-cause",
            "kind": "claim",
            "initial_status": "proposed",
            "current_status": "corroborated_within_scope",
            "transition_count": 4,
        }
    ]


def test_empty_ledger_has_empty_summary() -> None:
    summary = summarize_ledger(Ledger())

    assert summary.record_count == 0
    assert summary.subject_count == 0
    assert not summary.record_counts
    assert not summary.state_counts
    assert not summary.subjects
