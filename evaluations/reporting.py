# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Typed, schema-valid evaluation reports with one canonical JSON source."""

from __future__ import annotations

import hashlib
import json
import platform
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from itself import JsonObject, JsonValue, StrPath
from itself._json import format_json_path as _format_path

EVALUATION_REPORT_VERSION: Final = "0.1.0"
_REPORT_SCHEMA_PATH: Final = (
    Path(__file__).parent / "contracts" / "v1" / "evaluation-report.schema.json"
)


class _SchemaValidator(Protocol):
    """Typed surface used from the partially typed jsonschema dependency."""

    def iter_errors(self, instance: JsonValue) -> Iterator[ValidationError]: ...


class EvaluationStatus(StrEnum):
    """Possible outcomes for one independently inspectable evaluation check."""

    PASSED = "passed"
    FAILED = "failed"
    OBSERVED = "observed"
    SKIPPED = "skipped"


class EvaluationOutcome(StrEnum):
    """Aggregate outcome derived from all checks in one evaluation run."""

    PASSED = "passed"
    FAILED = "failed"
    OBSERVATIONAL = "observational"


def _empty_details() -> Mapping[str, JsonValue]:
    return {}


@dataclass(frozen=True, slots=True)
class EvaluationCheck:
    """One timed assertion, observation, or explicitly skipped capability."""

    check_id: str
    category: str
    status: EvaluationStatus
    duration_ms: int
    expected: str
    observed: str
    details: Mapping[str, JsonValue] = field(default_factory=_empty_details)

    def __post_init__(self) -> None:
        for name, value in (
            ("check_id", self.check_id),
            ("category", self.category),
            ("expected", self.expected),
            ("observed", self.observed),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.duration_ms < 0:
            raise ValueError("duration_ms must not be negative")
        object.__setattr__(
            self,
            "details",
            MappingProxyType(deepcopy(dict(self.details))),
        )

    def to_json_object(self) -> JsonObject:
        """Return a defensive language-neutral check representation."""

        return {
            "id": self.check_id,
            "category": self.category,
            "status": self.status.value,
            "duration_ms": self.duration_ms,
            "expected": self.expected,
            "observed": self.observed,
            "details": deepcopy(dict(self.details)),
        }


@dataclass(frozen=True, slots=True)
class EvaluationEnvironment:
    """Runtime identity retained without machine-specific filesystem paths."""

    python_version: str
    python_implementation: str
    platform: str

    @classmethod
    def current(cls) -> EvaluationEnvironment:
        """Capture the current interpreter and operating-system identity."""

        return cls(
            python_version=platform.python_version(),
            python_implementation=platform.python_implementation(),
            platform=platform.platform(),
        )

    def to_json_object(self) -> JsonObject:
        """Return the environment's report representation."""

        return {
            "python_version": self.python_version,
            "python_implementation": self.python_implementation,
            "platform": self.platform,
        }


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """One immutable, content-identified evaluation report."""

    suite_id: str
    run_id: str
    generated_at: datetime
    sdk_version: str
    source_revision: str | None
    environment: EvaluationEnvironment
    outcome: EvaluationOutcome
    checks: tuple[EvaluationCheck, ...]
    limitations: tuple[str, ...]

    def to_json_object(self) -> JsonObject:
        """Return the complete language-neutral report representation."""

        status_counts = {
            status: sum(check.status is status for check in self.checks)
            for status in EvaluationStatus
        }
        return {
            "report_version": EVALUATION_REPORT_VERSION,
            "suite_id": self.suite_id,
            "run_id": self.run_id,
            "generated_at": _timestamp(self.generated_at),
            "sdk_version": self.sdk_version,
            "source_revision": self.source_revision,
            "environment": self.environment.to_json_object(),
            "outcome": self.outcome.value,
            "summary": {
                "total": len(self.checks),
                "passed": status_counts[EvaluationStatus.PASSED],
                "failed": status_counts[EvaluationStatus.FAILED],
                "observed": status_counts[EvaluationStatus.OBSERVED],
                "skipped": status_counts[EvaluationStatus.SKIPPED],
            },
            "checks": [check.to_json_object() for check in self.checks],
            "limitations": list(self.limitations),
        }


class EvaluationReportValidationError(ValueError):
    """Raised when an evaluation report violates its versioned contract."""


class EvaluationReportValidator:
    """Validate evaluation reports against the repository contract."""

    def __init__(self) -> None:
        value = cast(
            JsonValue,
            json.loads(_REPORT_SCHEMA_PATH.read_text(encoding="utf-8")),
        )
        if not isinstance(value, dict):
            raise TypeError("evaluation report schema must contain a JSON object")
        Draft202012Validator.check_schema(value)
        self._validator = cast(
            _SchemaValidator,
            Draft202012Validator(value, format_checker=FormatChecker()),
        )

    def errors(self, report: JsonValue) -> list[str]:
        """Return stable, human-readable report validation errors."""

        issues = sorted(
            self._validator.iter_errors(report),
            key=lambda error: tuple(str(item) for item in error.absolute_path),
        )
        schema_errors = [
            f"{_format_path(tuple(issue.absolute_path))}: {issue.message}"
            for issue in issues
        ]
        if schema_errors:
            return schema_errors
        if not isinstance(report, dict):
            raise TypeError("schema-valid evaluation report was not an object")
        return _semantic_errors(report)

    def validate(self, report: JsonValue) -> None:
        """Validate a report or raise EvaluationReportValidationError."""

        issues = self.errors(report)
        if issues:
            raise EvaluationReportValidationError("\n".join(issues))


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _semantic_errors(report: JsonObject) -> list[str]:
    checks_value = report["checks"]
    summary_value = report["summary"]
    if not isinstance(checks_value, list) or not all(
        isinstance(item, dict) for item in checks_value
    ):
        raise TypeError("schema-valid checks field was not an object array")
    if not isinstance(summary_value, dict):
        raise TypeError("schema-valid summary field was not an object")
    checks = cast(list[JsonObject], checks_value)
    check_ids = tuple(cast(str, check["id"]) for check in checks)
    errors: list[str] = []
    if len(check_ids) != len(set(check_ids)):
        errors.append("$.checks: check identifiers must be unique")

    statuses = tuple(cast(str, check["status"]) for check in checks)
    expected_summary: JsonObject = {
        "total": len(checks),
        "passed": statuses.count(EvaluationStatus.PASSED.value),
        "failed": statuses.count(EvaluationStatus.FAILED.value),
        "observed": statuses.count(EvaluationStatus.OBSERVED.value),
        "skipped": statuses.count(EvaluationStatus.SKIPPED.value),
    }
    if summary_value != expected_summary:
        errors.append("$.summary: counts do not match the check outcomes")

    expected_outcome = (
        EvaluationOutcome.FAILED.value
        if EvaluationStatus.FAILED.value in statuses
        else (
            EvaluationOutcome.PASSED.value
            if EvaluationStatus.PASSED.value in statuses
            else EvaluationOutcome.OBSERVATIONAL.value
        )
    )
    if report["outcome"] != expected_outcome:
        errors.append("$.outcome: value does not match the check outcomes")

    identity = deepcopy(report)
    declared_run_id = identity.pop("run_id")
    if declared_run_id != evaluation_run_id(identity):
        errors.append("$.run_id: value does not bind the report contents")
    return errors


def _canonical_json(value: JsonObject) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def evaluation_run_id(report_without_run_id: JsonObject) -> str:
    """Derive one content identifier from every other report field."""

    if "run_id" in report_without_run_id:
        raise ValueError("report_without_run_id must not contain run_id")
    return f"urn:sha256:{hashlib.sha256(_canonical_json(report_without_run_id)).hexdigest()}"


def _outcome(checks: Sequence[EvaluationCheck]) -> EvaluationOutcome:
    if any(check.status is EvaluationStatus.FAILED for check in checks):
        return EvaluationOutcome.FAILED
    if any(check.status is EvaluationStatus.PASSED for check in checks):
        return EvaluationOutcome.PASSED
    return EvaluationOutcome.OBSERVATIONAL


def build_evaluation_report(
    *,
    suite_id: str,
    generated_at: datetime,
    sdk_version: str,
    source_revision: str | None,
    environment: EvaluationEnvironment,
    checks: Sequence[EvaluationCheck],
    limitations: Sequence[str],
) -> EvaluationReport:
    """Build, content-identify, and schema-validate one evaluation report."""

    if source_revision is not None and not source_revision.strip():
        raise ValueError("source_revision must be non-empty or None")
    check_values = tuple(checks)
    limitation_values = tuple(limitations)
    if not check_values:
        raise ValueError("an evaluation report requires at least one check")
    check_ids = tuple(check.check_id for check in check_values)
    if len(check_ids) != len(set(check_ids)):
        raise ValueError("evaluation check identifiers must be unique")
    if not limitation_values or any(not item.strip() for item in limitation_values):
        raise ValueError("limitations must contain non-empty values")

    provisional = EvaluationReport(
        suite_id=suite_id,
        run_id="urn:sha256:" + ("0" * 64),
        generated_at=generated_at,
        sdk_version=sdk_version,
        source_revision=source_revision,
        environment=environment,
        outcome=_outcome(check_values),
        checks=check_values,
        limitations=limitation_values,
    )
    identity = provisional.to_json_object()
    del identity["run_id"]
    report = EvaluationReport(
        suite_id=suite_id,
        run_id=evaluation_run_id(identity),
        generated_at=generated_at,
        sdk_version=sdk_version,
        source_revision=source_revision,
        environment=environment,
        outcome=provisional.outcome,
        checks=check_values,
        limitations=limitation_values,
    )
    EvaluationReportValidator().validate(report.to_json_object())
    return report


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render_evaluation_markdown(report: EvaluationReport) -> str:
    """Render a compact human report from the canonical report object."""

    value = report.to_json_object()
    summary = cast(JsonObject, value["summary"])
    lines = [
        f"# Itself evaluation: `{report.suite_id}`",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Outcome | `{report.outcome.value}` |",
        f"| Run ID | `{report.run_id}` |",
        f"| Generated | `{_timestamp(report.generated_at)}` |",
        f"| SDK | `{report.sdk_version}` |",
        f"| Source revision | `{report.source_revision or 'unavailable'}` |",
        f"| Python | `{report.environment.python_implementation} "
        f"{report.environment.python_version}` |",
        f"| Platform | `{_markdown_cell(report.environment.platform)}` |",
        "",
        "## Summary",
        "",
        (
            f"{summary['total']} checks: {summary['passed']} passed, "
            f"{summary['failed']} failed, {summary['observed']} observed, "
            f"{summary['skipped']} skipped."
        ),
        "",
        "## Checks",
        "",
        "| Check | Category | Status | Duration | Observation |",
        "|---|---|---:|---:|---|",
    ]
    lines.extend(
        (
            f"| `{check.check_id}` | {_markdown_cell(check.category)} | "
            f"`{check.status.value}` | {check.duration_ms} ms | "
            f"{_markdown_cell(check.observed)} |"
        )
        for check in report.checks
    )
    lines.extend(("", "## Limitations", ""))
    lines.extend(f"- {limitation}" for limitation in report.limitations)
    lines.append("")
    return "\n".join(lines)


def write_evaluation_report(directory: StrPath, report: EvaluationReport) -> None:
    """Write canonical JSON and derived Markdown into an existing directory."""

    EvaluationReportValidator().validate(report.to_json_object())
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "report.json"
    markdown_path = root / "report.md"
    if json_path.exists() or markdown_path.exists():
        raise FileExistsError("evaluation report output already exists")
    json_path.write_text(
        json.dumps(
            report.to_json_object(),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        render_evaluation_markdown(report),
        encoding="utf-8",
    )
