# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
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
from evaluations.capacity_curve import main, run_capacity_curve
from itself import EvidenceBundleValidator, JsonObject, JsonValue

GENERATED_AT = datetime(2026, 7, 24, 16, tzinfo=UTC)
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


def test_capacity_curve_reports_observations_and_retains_bundles(
    tmp_path: Path,
) -> None:
    output = tmp_path / "capacity"

    report = run_capacity_curve(
        output,
        sizes=(1, 3),
        source_revision="abc123",
        generated_at=GENERATED_AT,
        environment=ENVIRONMENT,
        monotonic_ns=StepClock(),
    )

    assert report.outcome is EvaluationOutcome.OBSERVATIONAL
    assert [check.check_id for check in report.checks] == [
        "capacity.records-1",
        "capacity.records-3",
    ]
    assert all(check.status is EvaluationStatus.OBSERVED for check in report.checks)
    for size, check in zip((1, 3), report.checks, strict=True):
        assert check.details["record_count"] == size
        assert cast(int, check.details["ledger_bytes"]) > 0
        assert cast(int, check.details["receipt_bytes"]) > 0
        EvidenceBundleValidator().validate(output / "artifacts" / f"records-{size}")

    value = _load_object(output / "report.json")
    assert value == report.to_json_object()
    EvaluationReportValidator().validate(value)


def test_capacity_report_is_reproducible_under_fixed_observations(
    tmp_path: Path,
) -> None:
    first = run_capacity_curve(
        tmp_path / "first",
        sizes=(1, 3),
        source_revision="abc123",
        generated_at=GENERATED_AT,
        environment=ENVIRONMENT,
        monotonic_ns=StepClock(),
    )
    second = run_capacity_curve(
        tmp_path / "second",
        sizes=(1, 3),
        source_revision="abc123",
        generated_at=GENERATED_AT,
        environment=ENVIRONMENT,
        monotonic_ns=StepClock(),
    )

    assert first.to_json_object() == second.to_json_object()


@pytest.mark.parametrize("sizes", [(), (0,), (-1,), (1, 1)])
def test_capacity_curve_rejects_invalid_sizes(
    tmp_path: Path,
    sizes: tuple[int, ...],
) -> None:
    output = tmp_path / "capacity"

    with pytest.raises(ValueError):
        run_capacity_curve(output, sizes=sizes)

    assert not output.exists()


def test_capacity_curve_cli_reports_observational_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "capacity"

    result = main(
        [
            "--output",
            str(output),
            "--size",
            "1",
            "--size",
            "2",
            "--source-revision",
            "abc123",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out.startswith(f"OBSERVATIONAL {output}: 2 samples, urn:sha256:")
    assert not captured.err
