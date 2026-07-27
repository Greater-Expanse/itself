# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from itself import JsonObject, JsonValue

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = ROOT / "experiments" / "contracts" / "v1"


class _SchemaValidator(Protocol):
    def iter_errors(self, instance: JsonValue) -> Iterator[ValidationError]: ...


def _validator(name: str) -> _SchemaValidator:
    value = cast(
        JsonValue,
        json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8")),
    )
    assert isinstance(value, dict)
    Draft202012Validator.check_schema(value)
    return cast(
        _SchemaValidator,
        Draft202012Validator(value, format_checker=FormatChecker()),
    )


def _file(path: str) -> JsonObject:
    return {
        "path": path,
        "media_type": "application/json",
        "visibility": "shareable",
        "size_bytes": 1,
        "digest": {"algorithm": "sha256", "value": "0" * 64},
    }


def _binding(attempt_id: str, result_path: str) -> JsonObject:
    return {
        "source_attempt_id": attempt_id,
        "result_path": result_path,
        "raw_artifact_sha256": "1" * 64,
    }


def _completed_bundle(*, replicate_index: int = 1) -> JsonObject:
    narrative_path = "attempts/narrative-attempt-1/result.json"
    structured_path = "attempts/structured-attempt-1/result.json"
    files: list[JsonValue] = [_file(f"file-{index}.json") for index in range(14)]
    return {
        "bundle_version": "0.2.0",
        "bundle_id": f"urn:sha256:{'0' * 64}",
        "comparison_series_id": "case-001-comparison",
        "replicate_index": replicate_index,
        "outcome": "completed",
        "created_at": "2026-07-22T20:00:00Z",
        "attempt_outcomes": {
            "narrative-attempt-1": "completed",
            "structured-attempt-1": "completed",
        },
        "completed_branch_count": 3,
        "contains_private_artifacts": True,
        "branch_bindings": {
            "narrative_diagnosis": _binding(
                "narrative-attempt-1",
                narrative_path,
            ),
            "structured_without_testing": _binding(
                "structured-attempt-1",
                structured_path,
            ),
            "evidence_enforced": _binding(
                "structured-attempt-1",
                structured_path,
            ),
        },
        "files": files,
    }


def _failed_bundle(*, failed_attempt: str) -> JsonObject:
    if failed_attempt == "narrative-attempt-1":
        outcomes: JsonObject = {
            "narrative-attempt-1": "adapter_failed",
            "structured-attempt-1": "not_run",
        }
    else:
        outcomes = {
            "narrative-attempt-1": "completed",
            "structured-attempt-1": "adapter_failed",
        }
    return {
        "bundle_version": "0.2.0",
        "bundle_id": f"urn:sha256:{'0' * 64}",
        "comparison_series_id": "case-001-comparison",
        "replicate_index": 1,
        "outcome": "adapter_failed",
        "created_at": "2026-07-22T20:00:00Z",
        "attempt_outcomes": outcomes,
        "completed_branch_count": 0,
        "contains_private_artifacts": False,
        "branch_bindings": {
            "narrative_diagnosis": None,
            "structured_without_testing": None,
            "evidence_enforced": None,
        },
        "files": [_file("comparison-manifest.json"), _file("attempt-report.json")],
    }


def test_comparison_bundle_schema_accepts_completed_replicate() -> None:
    assert not list(
        _validator("comparison-bundle.schema.json").iter_errors(_completed_bundle())
    )


def test_comparison_bundle_schema_accepts_later_replicate() -> None:
    assert not list(
        _validator("comparison-bundle.schema.json").iter_errors(
            _completed_bundle(replicate_index=10)
        )
    )


def test_comparison_bundle_schema_accepts_either_registered_failure_boundary() -> None:
    validator = _validator("comparison-bundle.schema.json")

    assert not list(
        validator.iter_errors(_failed_bundle(failed_attempt="narrative-attempt-1"))
    )
    assert not list(
        validator.iter_errors(_failed_bundle(failed_attempt="structured-attempt-1"))
    )


def test_comparison_bundle_schema_rejects_skipping_failed_narrative_attempt() -> None:
    bundle = _failed_bundle(failed_attempt="narrative-attempt-1")
    outcomes = cast(JsonObject, bundle["attempt_outcomes"])
    outcomes["structured-attempt-1"] = "completed"

    assert list(_validator("comparison-bundle.schema.json").iter_errors(bundle))


def test_comparison_bundle_schema_rejects_branches_after_attempt_failure() -> None:
    bundle = _failed_bundle(failed_attempt="structured-attempt-1")
    bindings = cast(JsonObject, bundle["branch_bindings"])
    bindings["narrative_diagnosis"] = _binding(
        "narrative-attempt-1",
        "attempts/narrative-attempt-1/result.json",
    )

    assert list(_validator("comparison-bundle.schema.json").iter_errors(bundle))


def test_comparison_attempt_report_schema_accepts_sanitized_failure() -> None:
    report: JsonObject = {
        "attempt_report_version": "0.2.0",
        "comparison_series_id": "case-001-comparison",
        "attempt_id": "structured-attempt-1",
        "profile_id": "diagnosis-assertion-v0.2.0",
        "outcome": "adapter_failed",
        "completed_at": "2026-07-22T20:00:00Z",
        "failure": {
            "kind": "transport",
            "retryable": True,
            "status_code": None,
        },
    }

    assert not list(
        _validator("comparison-attempt-report.schema.json").iter_errors(report)
    )


def test_comparison_attempt_report_schema_rejects_private_error_detail() -> None:
    report: JsonObject = {
        "attempt_report_version": "0.2.0",
        "comparison_series_id": "case-001-comparison",
        "attempt_id": "narrative-attempt-1",
        "profile_id": "narrative-assertion-v0.2.0",
        "outcome": "adapter_failed",
        "completed_at": "2026-07-22T20:00:00Z",
        "failure": {
            "kind": "transport",
            "retryable": True,
            "status_code": None,
            "detail": "provider-private text",
        },
    }

    assert list(_validator("comparison-attempt-report.schema.json").iter_errors(report))
