# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from evaluations import (
    EvaluationEnvironment,
    EvaluationOutcome,
    EvaluationReportValidator,
    EvaluationStatus,
)
from evaluations.sdk_exercise import main, run_sdk_exercise
from itself import EvidenceBundleValidator, JsonObject, JsonValue

GENERATED_AT = datetime(2026, 7, 24, 15, tzinfo=UTC)
ENVIRONMENT = EvaluationEnvironment(
    python_version="3.11.15",
    python_implementation="CPython",
    platform="test-platform",
)


@dataclass(slots=True)
class StepClock:
    """Return one additional millisecond on every observation."""

    value: int = 0

    def __call__(self) -> int:
        current = self.value
        self.value += 1_000_000
        return current


def _load_object(path: Path) -> JsonObject:
    value = cast(JsonValue, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def test_sdk_exercise_reports_end_to_end_and_mutation_behavior(
    tmp_path: Path,
) -> None:
    output = tmp_path / "evaluation"

    report = run_sdk_exercise(
        output,
        source_revision="abc123",
        generated_at=GENERATED_AT,
        environment=ENVIRONMENT,
        monotonic_ns=StepClock(),
    )

    assert report.outcome is EvaluationOutcome.PASSED
    assert len(report.checks) == 31
    assert not [
        check for check in report.checks if check.status is EvaluationStatus.FAILED
    ]
    assert {check.category for check in report.checks} == {
        "schema portability",
        "end-to-end lifecycle",
        "determinism",
        "record mutation",
        "ledger mutation",
        "evidence-bundle mutation",
    }
    assert {
        check.check_id
        for check in report.checks
        if check.check_id.startswith("mutation.ledger.")
    } == {
        "mutation.ledger.blank-transition-subject",
        "mutation.ledger.dangling-reference",
        "mutation.ledger.duplicate-id",
        "mutation.ledger.invalid-transition",
        "mutation.ledger.proto-scope-mismatch",
        "mutation.ledger.state-drift",
        "mutation.ledger.test-plan-kind-mismatch",
        "mutation.ledger.transition-subject-mismatch",
        "mutation.ledger.unrelated-evidence",
        "mutation.ledger.verdict-after-transition",
        "mutation.ledger.wrong-reference-kind",
    }

    value = _load_object(output / "report.json")
    assert value == report.to_json_object()
    EvaluationReportValidator().validate(value)
    EvidenceBundleValidator().validate(output / "artifacts" / "lifecycle-bundle")
    assert report.run_id in (output / "report.md").read_text(encoding="utf-8")
    if os.name == "posix":
        assert stat.S_IMODE(output.stat().st_mode) == stat.S_IRWXU


def test_sdk_exercise_report_is_reproducible_under_fixed_observations(
    tmp_path: Path,
) -> None:
    first = run_sdk_exercise(
        tmp_path / "first",
        source_revision="abc123",
        generated_at=GENERATED_AT,
        environment=ENVIRONMENT,
        monotonic_ns=StepClock(),
    )
    second = run_sdk_exercise(
        tmp_path / "second",
        source_revision="abc123",
        generated_at=GENERATED_AT,
        environment=ENVIRONMENT,
        monotonic_ns=StepClock(),
    )

    assert first.to_json_object() == second.to_json_object()
    assert (tmp_path / "first" / "report.md").read_bytes() == (
        tmp_path / "second" / "report.md"
    ).read_bytes()


def test_sdk_exercise_refuses_to_replace_output(tmp_path: Path) -> None:
    output = tmp_path / "evaluation"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("caller-owned\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        run_sdk_exercise(output)

    assert sentinel.read_text(encoding="utf-8") == "caller-owned\n"


def test_sdk_exercise_cli_reports_completed_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "evaluation"

    result = main(
        [
            "--output",
            str(output),
            "--source-revision",
            "abc123",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out.startswith(f"PASSED {output}: 31 checks, urn:sha256:")
    assert not captured.err
