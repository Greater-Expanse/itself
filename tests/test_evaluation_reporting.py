# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evaluations import (
    EvaluationCheck,
    EvaluationEnvironment,
    EvaluationOutcome,
    EvaluationReport,
    EvaluationReportValidationError,
    EvaluationReportValidator,
    EvaluationStatus,
    build_evaluation_report,
    evaluation_run_id,
    render_evaluation_markdown,
    write_evaluation_report,
)
from itself import JsonObject

GENERATED_AT = datetime(2026, 7, 24, 12, tzinfo=UTC)
ENVIRONMENT = EvaluationEnvironment(
    python_version="3.11.15",
    python_implementation="CPython",
    platform="test-platform",
)


def _check(
    status: EvaluationStatus = EvaluationStatus.PASSED,
) -> EvaluationCheck:
    return EvaluationCheck(
        check_id=f"test.{status.value}",
        category="contract",
        status=status,
        duration_ms=3,
        expected="The contract is enforced.",
        observed=f"The check was {status.value}.",
        details={"fixture": True},
    )


def _report(*checks: EvaluationCheck) -> EvaluationReport:
    return build_evaluation_report(
        suite_id="report-contract",
        generated_at=GENERATED_AT,
        sdk_version="0.1.0a6",
        source_revision="abc123",
        environment=ENVIRONMENT,
        checks=checks,
        limitations=("This is a contract fixture.",),
    )


def test_report_is_content_identified_and_schema_valid() -> None:
    report = _report(
        _check(EvaluationStatus.PASSED),
        _check(EvaluationStatus.OBSERVED),
        _check(EvaluationStatus.SKIPPED),
    )
    value = report.to_json_object()
    identity = deepcopy(value)
    del identity["run_id"]

    assert report.outcome is EvaluationOutcome.PASSED
    assert report.run_id == evaluation_run_id(identity)
    assert value["summary"] == {
        "total": 3,
        "passed": 1,
        "failed": 0,
        "observed": 1,
        "skipped": 1,
    }
    EvaluationReportValidator().validate(value)


def test_failed_and_observational_outcomes_are_derived() -> None:
    failed = _report(_check(EvaluationStatus.FAILED))
    observational = _report(_check(EvaluationStatus.OBSERVED))

    assert failed.outcome is EvaluationOutcome.FAILED
    assert observational.outcome is EvaluationOutcome.OBSERVATIONAL


def test_validator_rejects_tampered_report() -> None:
    value = _report(_check()).to_json_object()
    summary = value["summary"]
    if not isinstance(summary, dict):
        raise TypeError("summary fixture must be an object")
    summary["failed"] = -1

    with pytest.raises(EvaluationReportValidationError, match="minimum"):
        EvaluationReportValidator().validate(value)


def test_validator_rejects_semantically_inconsistent_report() -> None:
    value = _report(_check()).to_json_object()
    value["outcome"] = "observational"

    with pytest.raises(EvaluationReportValidationError, match="check outcomes"):
        EvaluationReportValidator().validate(value)


def test_validator_rejects_report_not_bound_by_run_id() -> None:
    value = _report(_check()).to_json_object()
    value["sdk_version"] = "tampered"

    with pytest.raises(EvaluationReportValidationError, match="does not bind"):
        EvaluationReportValidator().validate(value)


def test_json_is_canonical_source_for_markdown_report(tmp_path: Path) -> None:
    report = _report(_check())
    write_evaluation_report(tmp_path, report)

    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert markdown == render_evaluation_markdown(report)
    assert report.run_id in markdown
    assert '"suite_id": "report-contract"' in (tmp_path / "report.json").read_text(
        encoding="utf-8"
    )

    with pytest.raises(FileExistsError):
        write_evaluation_report(tmp_path, report)


def test_check_details_are_defensively_copied() -> None:
    details: JsonObject = {"nested": {"value": "original"}}
    check = EvaluationCheck(
        check_id="copy",
        category="contract",
        status=EvaluationStatus.PASSED,
        duration_ms=0,
        expected="Details remain stable.",
        observed="Details remained stable.",
        details=details,
    )
    nested = details["nested"]
    if not isinstance(nested, dict):
        raise TypeError("nested fixture must be an object")
    nested["value"] = "mutated"

    assert check.to_json_object()["details"] == {"nested": {"value": "original"}}
