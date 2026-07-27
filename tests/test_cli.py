# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from itself import (
    DEFAULT_EVIDENCE_BUNDLE_LIMITATIONS,
    EvidenceBundleValidator,
    JsonlLedgerStore,
    JsonObject,
    JsonReceiptStore,
    JsonValue,
)
from itself.cli import main

ROOT = Path(__file__).resolve().parents[1]
VALID_CLAIM = ROOT / "conformance" / "valid" / "minimal-claim.json"
INVALID_CLAIM = ROOT / "conformance" / "invalid" / "claim-without-scope.json"
VALID_HISTORY = (
    ROOT / "conformance" / "bundles" / "valid" / "evidence-backed-history.json"
)
REFERENCE_BUNDLE = ROOT / "examples" / "cache-key-diagnosis" / "bundle"


def _history() -> list[JsonObject]:
    value = cast(JsonValue, json.loads(VALID_HISTORY.read_text(encoding="utf-8")))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError("valid history fixture must contain a JSON object array")
    return cast(list[JsonObject], value)


def test_record_validate_reports_success(capsys: pytest.CaptureFixture[str]) -> None:
    result = main(["validate", str(VALID_CLAIM)])

    captured = capsys.readouterr()
    assert result == 0
    assert f"PASS {VALID_CLAIM}" in captured.out
    assert not captured.err


def test_record_validate_reports_failure_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = main(["validate", str(INVALID_CLAIM)])

    captured = capsys.readouterr()
    assert result == 1
    assert not captured.out
    assert f"FAIL {INVALID_CLAIM}" in captured.err


def test_bundle_validate_reports_content_identity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = main(["bundle", "validate", str(REFERENCE_BUNDLE)])

    captured = capsys.readouterr()
    assert result == 0
    assert f"PASS {REFERENCE_BUNDLE}: 23 records, 4 files" in captured.out
    assert "urn:sha256:b76cced9" in captured.out
    assert not captured.err


def test_bundle_create_materializes_a_closed_bundle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "bundle"
    ledger_path = REFERENCE_BUNDLE / "ledger.jsonl"
    input_path = REFERENCE_BUNDLE / "inputs" / "diagnosis-request.json"
    artifact_path = REFERENCE_BUNDLE / "artifacts" / "model-assertion.json"

    result = main(
        [
            "bundle",
            "create",
            str(ledger_path),
            str(destination),
            "--title",
            "CLI-created evidence bundle",
            "--created-at",
            "2026-07-23T12:00:00Z",
            "--artifact",
            f"artifact-diagnostician-output={artifact_path}",
            "--input",
            str(input_path),
            "--limitation",
            "This is a CLI integration fixture.",
        ]
    )

    captured = capsys.readouterr()
    verified = EvidenceBundleValidator().validate(destination)
    assert result == 0
    assert f"PASS {destination}: 23 records, 4 files" in captured.out
    assert not captured.err
    assert verified.manifest["created_at"] == "2026-07-23T12:00:00Z"
    assert verified.manifest["limitations"] == [
        *DEFAULT_EVIDENCE_BUNDLE_LIMITATIONS,
        "This is a CLI integration fixture.",
    ]
    assert (destination / "inputs" / input_path.name).read_bytes() == (
        input_path.read_bytes()
    )


def test_bundle_create_requires_every_ledger_artifact_mapping(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "bundle"

    result = main(
        [
            "bundle",
            "create",
            str(REFERENCE_BUNDLE / "ledger.jsonl"),
            str(destination),
            "--title",
            "Incomplete bundle",
            "--input",
            str(REFERENCE_BUNDLE / "inputs" / "diagnosis-request.json"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "missing=['artifact-diagnostician-output']" in captured.err
    assert not captured.out
    assert not destination.exists()


def test_ledger_append_creates_and_validates_ledger(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "assurance.jsonl"

    append_result = main(["ledger", "append", str(path), str(VALID_CLAIM)])
    append_output = capsys.readouterr()
    validate_result = main(["ledger", "validate", str(path)])
    validate_output = capsys.readouterr()

    assert append_result == 0
    assert "appended 1 records" in append_output.out
    assert validate_result == 0
    assert "1 records, 1 subjects" in validate_output.out


def test_ledger_append_batch_is_atomic(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "assurance.jsonl"
    assert main(["ledger", "append", str(path), str(VALID_CLAIM)]) == 0
    capsys.readouterr()
    before = path.read_bytes()

    result = main(
        [
            "ledger",
            "append",
            str(path),
            str(ROOT / "conformance" / "valid" / "artifact-reference.json"),
            str(INVALID_CLAIM),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "schema_invalid" in captured.err
    assert path.read_bytes() == before


def test_replay_text_shows_state_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "assurance.jsonl"
    JsonlLedgerStore(path).extend(_history())

    result = main(["ledger", "replay", str(path)])

    captured = capsys.readouterr()
    assert result == 0
    assert (
        "claim-cache-cause\tclaim\tproposed -> corroborated_within_scope"
        in captured.out
    )
    assert "4 transitions" in captured.out


def test_replay_json_is_machine_readable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "assurance.jsonl"
    JsonlLedgerStore(path).extend(_history())

    result = main(["ledger", "replay", str(path), "--format", "json"])

    captured = capsys.readouterr()
    output = cast(list[JsonObject], json.loads(captured.out))
    assert result == 0
    assert output[0]["subject_id"] == "claim-cache-cause"
    assert output[0]["current_status"] == "corroborated_within_scope"


def test_summary_json_separates_records_and_states(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "assurance.jsonl"
    JsonlLedgerStore(path).extend(_history())

    result = main(["ledger", "summary", str(path), "--format", "json"])

    captured = capsys.readouterr()
    output = cast(JsonObject, json.loads(captured.out))
    assert result == 0
    assert output["record_count"] == 10
    assert output["record_counts"] == {
        "artifact_reference": 1,
        "claim": 1,
        "evidence": 2,
        "status_transition": 4,
        "verdict": 2,
    }
    assert output["state_counts"] == {"corroborated_within_scope": 1}


def test_summary_text_is_stable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "assurance.jsonl"
    JsonlLedgerStore(path).extend(_history())

    result = main(["ledger", "summary", str(path)])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out.splitlines() == [
        "records: 10",
        "subjects: 1",
        "records_by_kind:",
        "  artifact_reference: 1",
        "  claim: 1",
        "  evidence: 2",
        "  status_transition: 4",
        "  verdict: 2",
        "current_states:",
        "  corroborated_within_scope: 1",
    ]


def test_read_command_rejects_missing_ledger(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "missing.jsonl"

    result = main(["ledger", "summary", str(path)])

    captured = capsys.readouterr()
    assert result == 1
    assert "ledger file does not exist" in captured.err


def test_read_command_reports_malformed_ledger(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "malformed.jsonl"
    path.write_text("{not-json}\n", encoding="utf-8")

    result = main(["ledger", "validate", str(path)])

    captured = capsys.readouterr()
    assert result == 1
    assert "invalid JSON" in captured.err


def test_record_validate_rejects_duplicate_object_keys(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"kind":"claim","kind":"evidence"}', encoding="utf-8")

    result = main(["validate", str(path)])

    captured = capsys.readouterr()
    assert result == 1
    assert "duplicate JSON object key 'kind'" in captured.err
    assert not captured.out


def test_receipt_generate_prints_machine_readable_document(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger_path = tmp_path / "assurance.jsonl"
    JsonlLedgerStore(ledger_path).extend(_history())

    result = main(["receipt", "generate", str(ledger_path)])

    captured = capsys.readouterr()
    receipt = cast(JsonObject, json.loads(captured.out))
    assert result == 0
    assert receipt["receipt_version"] == "0.1.0-alpha.2"
    assert cast(JsonObject, receipt["source_ledger"])["record_count"] == 10


def test_receipt_generate_and_validate_against_ledger(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger_path = tmp_path / "assurance.jsonl"
    receipt_path = tmp_path / "receipt.json"
    JsonlLedgerStore(ledger_path).extend(_history())

    generate_result = main(
        [
            "receipt",
            "generate",
            str(ledger_path),
            "--output",
            str(receipt_path),
        ]
    )
    generate_output = capsys.readouterr()
    validate_result = main(
        [
            "receipt",
            "validate",
            str(receipt_path),
            "--ledger",
            str(ledger_path),
        ]
    )
    validate_output = capsys.readouterr()

    assert generate_result == 0
    assert "receipt generated" in generate_output.out
    assert validate_result == 0
    assert "bound to" in validate_output.out


def test_receipt_binding_rejects_schema_valid_tampering(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger_path = tmp_path / "assurance.jsonl"
    receipt_path = tmp_path / "receipt.json"
    JsonlLedgerStore(ledger_path).extend(_history())
    assert (
        main(
            [
                "receipt",
                "generate",
                str(ledger_path),
                "--output",
                str(receipt_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    receipt = JsonReceiptStore(receipt_path).load()
    subjects = cast(list[JsonObject], receipt["subjects"])
    subjects[0]["current_status"] = "supported"
    JsonReceiptStore(receipt_path).write(receipt)

    result = main(
        [
            "receipt",
            "validate",
            str(receipt_path),
            "--ledger",
            str(ledger_path),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "does not match" in captured.err
