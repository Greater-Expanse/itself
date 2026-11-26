# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import itself.cli as cli_module
from itself import (
    DEFAULT_EVIDENCE_BUNDLE_LIMITATIONS,
    EvidenceBundleLimits,
    EvidenceBundleValidator,
    JsonlLedgerStore,
    JsonObject,
    JsonReceiptStore,
    JsonValue,
    ValidationPolicy,
    __version__,
    build_reasoning_receipt,
    replay_subjects,
    summarize_ledger,
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


def test_bundle_create_media_types_ignore_host_mimetypes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "bundle"
    trace_path = tmp_path / "trace.log"
    trace_path.write_text("check passed\n", encoding="utf-8")
    notes_path = tmp_path / "notes.unregistered"
    notes_path.write_text("reviewer notes\n", encoding="utf-8")
    artifact_path = REFERENCE_BUNDLE / "artifacts" / "model-assertion.json"
    mimetypes.add_type("text/x-host-json", ".json")
    mimetypes.add_type("text/x-host-log", ".log")
    mimetypes.add_type("text/x-host-notes", ".unregistered")
    try:
        result = main(
            [
                "bundle",
                "create",
                str(REFERENCE_BUNDLE / "ledger.jsonl"),
                str(destination),
                "--title",
                "Host-independent media types",
                "--created-at",
                "2026-07-23T12:00:00Z",
                "--artifact",
                f"artifact-diagnostician-output={artifact_path}",
                "--input",
                str(REFERENCE_BUNDLE / "inputs" / "diagnosis-request.json"),
                "--input",
                str(trace_path),
                "--supplemental",
                str(notes_path),
            ]
        )
    finally:
        mimetypes.init()

    captured = capsys.readouterr()
    manifest = EvidenceBundleValidator().validate(destination).manifest
    media_types = {
        cast(str, entry["path"]): entry["media_type"]
        for entry in cast(list[JsonObject], manifest["files"])
    }
    assert result == 0
    assert not captured.err
    assert media_types["inputs/diagnosis-request.json"] == "application/json"
    assert media_types["inputs/trace.log"] == "text/plain"
    assert media_types["supplemental/notes.unregistered"] == "application/octet-stream"


def test_bundle_validate_reports_deeply_nested_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "bundle"
    shutil.copytree(REFERENCE_BUNDLE, destination)
    (destination / "bundle.json").write_text("[" * 3000 + "]" * 3000, encoding="utf-8")

    result = main(["bundle", "validate", str(destination)])

    captured = capsys.readouterr()
    assert result == 1
    assert "FAIL" in captured.err
    assert "cannot load one strict JSON object" in captured.err
    assert not captured.out


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


@pytest.mark.parametrize("alias", ["same-path", "hard-link", "symbolic-link"])
def test_receipt_generate_refuses_to_replace_its_source_ledger(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    alias: str,
) -> None:
    ledger_path = tmp_path / "assurance.jsonl"
    JsonlLedgerStore(ledger_path).extend(_history())
    original = ledger_path.read_bytes()
    output_path = ledger_path
    if alias != "same-path":
        output_path = tmp_path / "alias.jsonl"
        try:
            if alias == "hard-link":
                os.link(ledger_path, output_path)
            else:
                output_path.symlink_to(ledger_path)
        except OSError:
            pytest.skip(f"{alias}s are unavailable")

    result = main(
        ["receipt", "generate", str(ledger_path), "--output", str(output_path)]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert f"FAIL {output_path}: receipt output would replace" in captured.err
    assert not captured.out
    assert ledger_path.read_bytes() == original


def test_receipt_generate_still_replaces_an_existing_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger_path = tmp_path / "assurance.jsonl"
    receipt_path = tmp_path / "receipt.json"
    JsonlLedgerStore(ledger_path).extend(_history())
    receipt_path.write_text("{}\n", encoding="utf-8")

    result = main(
        ["receipt", "generate", str(ledger_path), "--output", str(receipt_path)]
    )

    captured = capsys.readouterr()
    receipt = JsonReceiptStore(receipt_path).load()
    assert result == 0
    assert "receipt generated" in captured.out
    assert cast(JsonObject, receipt["source_ledger"])["record_count"] == 10


def test_read_commands_apply_cli_resource_limits(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path = tmp_path / "assurance.jsonl"
    receipt_path = tmp_path / "receipt.json"
    JsonlLedgerStore(ledger_path).extend(_history())
    assert (
        main(["receipt", "generate", str(ledger_path), "--output", str(receipt_path)])
        == 0
    )
    capsys.readouterr()
    monkeypatch.setattr(
        cli_module,
        "_CLI_LIMITS",
        EvidenceBundleLimits(
            max_ledger_records=9,
            max_receipt_bytes=receipt_path.stat().st_size - 1,
        ),
    )

    ledger_result = main(["ledger", "summary", str(ledger_path)])
    ledger_output = capsys.readouterr()
    receipt_result = main(["receipt", "validate", str(receipt_path)])
    receipt_output = capsys.readouterr()

    assert ledger_result == 1
    assert "ledger record count exceeds limit 9" in ledger_output.err
    assert receipt_result == 1
    assert "receipt size exceeds limit" in receipt_output.err


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


def test_receipt_generate_refuses_a_receipt_later_reads_would_reject(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path = tmp_path / "assurance.jsonl"
    receipt_path = tmp_path / "receipt.json"
    JsonlLedgerStore(ledger_path).extend(_history())
    monkeypatch.setattr(
        cli_module,
        "_CLI_LIMITS",
        EvidenceBundleLimits(max_receipt_bytes=100),
    )

    result = main(
        ["receipt", "generate", str(ledger_path), "--output", str(receipt_path)]
    )

    assert result == 1
    assert "exceeds limit 100 bytes" in capsys.readouterr().err
    assert not receipt_path.exists()


@pytest.mark.parametrize(
    ("command", "trusted", "expected"),
    [
        (["bundle", "validate", str(REFERENCE_BUNDLE)], "someone-else", 1),
        (["bundle", "validate", str(REFERENCE_BUNDLE)], "case-001-promotion-policy", 0),
        (
            ["ledger", "validate", str(REFERENCE_BUNDLE / "ledger.jsonl")],
            "someone-else",
            1,
        ),
        (
            ["ledger", "replay", str(REFERENCE_BUNDLE / "ledger.jsonl")],
            "case-001-promotion-policy",
            0,
        ),
    ],
    ids=["bundle-untrusted", "bundle-trusted", "ledger-untrusted", "replay-trusted"],
)
def test_trusted_authorizer_option_restricts_evidence_backed_transitions(
    capsys: pytest.CaptureFixture[str],
    command: list[str],
    trusted: str,
    expected: int,
) -> None:
    result = main([*command, "--trusted-authorizer", trusted])

    captured = capsys.readouterr()
    assert result == expected
    assert ("untrusted_authorizer" in captured.err) == (expected == 1)


def test_ledger_append_applies_the_trusted_authorizers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger_path = tmp_path / "assurance.jsonl"
    source = REFERENCE_BUNDLE / "ledger.jsonl"
    records = [
        json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()
    ]
    record_paths: list[str] = []
    for index, record in enumerate(records):
        path = tmp_path / f"record-{index:02}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        record_paths.append(str(path))

    result = main(
        [
            "ledger",
            "append",
            str(ledger_path),
            *record_paths,
            "--trusted-authorizer",
            "someone-else",
        ]
    )

    assert result == 1
    assert "untrusted_authorizer" in capsys.readouterr().err
    assert not ledger_path.exists()


def test_receipt_validate_refuses_a_trust_list_without_the_ledger(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger_path = tmp_path / "assurance.jsonl"
    receipt_path = tmp_path / "receipt.json"
    JsonlLedgerStore(ledger_path).extend(_history())
    assert (
        main(["receipt", "generate", str(ledger_path), "--output", str(receipt_path)])
        == 0
    )
    capsys.readouterr()

    result = main(
        ["receipt", "validate", str(receipt_path), "--trusted-authorizer", "nobody"]
    )

    assert result == 1
    assert "--trusted-authorizer requires --ledger" in capsys.readouterr().err


# Control characters and line separators a record id or file name may carry,
# including a forged result line and a GitHub Actions annotation.
FORGED_TEXT = "x\nPASS forged\x1b[2K\r::error::forged\x7f\x85\u2028\u2029\tend"
ESCAPED_FORGED_TEXT = (
    "x\\x0aPASS forged\\x1b[2K\\x0d::error::forged\\x7f\\x85\\u2028\\u2029\\x09end"
)
UNSAFE_OUTPUT_CHARACTERS = (
    *(chr(code) for code in range(0x20) if chr(code) != "\n"),
    *(chr(code) for code in range(0x7F, 0xA0)),
    "\u2028",
    "\u2029",
)


def _claim_with_id(record_id: str) -> JsonObject:
    claim = _history()[0]
    claim["id"] = record_id
    return claim


def _write_jsonl(path: Path, records: list[JsonObject]) -> None:
    path.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )


def _assert_no_unsafe_characters(text: str) -> None:
    assert not [
        character for character in UNSAFE_OUTPUT_CHARACTERS if character in text
    ]


def test_replay_text_escapes_control_characters_in_record_ids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    JsonlLedgerStore(ledger_path).append(_claim_with_id(FORGED_TEXT))

    result = main(["ledger", "replay", str(ledger_path)])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == (
        f"{ESCAPED_FORGED_TEXT}\tclaim\tproposed -> proposed\t0 transitions\n"
    )
    assert not captured.err


def test_replay_json_keeps_record_ids_exact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    JsonlLedgerStore(ledger_path).append(_claim_with_id(FORGED_TEXT))

    result = main(["ledger", "replay", str(ledger_path), "--format", "json"])

    replayed = cast(list[JsonObject], json.loads(capsys.readouterr().out))
    assert result == 0
    assert replayed[0]["subject_id"] == FORGED_TEXT


def test_failures_escape_record_ids_and_keep_one_line_per_issue(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    claim = _claim_with_id(FORGED_TEXT)
    _write_jsonl(ledger_path, [claim, claim, claim])

    result = main(["ledger", "validate", str(ledger_path)])

    captured = capsys.readouterr()
    assert result == 1
    assert not captured.out
    assert captured.err.split("\n") == [
        f"FAIL duplicate_id: {ESCAPED_FORGED_TEXT}: duplicates the record at "
        "position 0; encountered again at position 1",
        f"duplicate_id: {ESCAPED_FORGED_TEXT}: duplicates the record at "
        "position 0; encountered again at position 2",
        "",
    ]
    _assert_no_unsafe_characters(captured.err)


def test_record_validate_escapes_file_names(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    try:
        valid = tmp_path / "valid\n::error::forged.json"
        invalid = tmp_path / "invalid\x1b[2K.json"
        valid.write_bytes(VALID_CLAIM.read_bytes())
        invalid.write_bytes(INVALID_CLAIM.read_bytes())
    except OSError:
        pytest.skip("file names with control characters are unavailable")

    result = main(["validate", str(valid), str(invalid)])

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == f"PASS {tmp_path}/valid\\x0a::error::forged.json\n"
    assert captured.err.startswith(f"FAIL {tmp_path}/invalid\\x1b[2K.json\n  ")
    _assert_no_unsafe_characters(captured.out + captured.err)


def test_bundle_manifest_schema_failures_keep_one_line_per_issue(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = tmp_path / "bundle"
    shutil.copytree(REFERENCE_BUNDLE, bundle)
    manifest = cast(
        JsonObject,
        json.loads((bundle / "bundle.json").read_text(encoding="utf-8")),
    )
    del manifest["title"]
    manifest["closed_artifacts"] = FORGED_TEXT
    (bundle / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = main(["bundle", "validate", str(bundle)])

    lines = capsys.readouterr().err.split("\n")
    assert result == 1
    assert len(lines) == 3
    assert lines[0].startswith("FAIL $")
    assert lines[2] == ""
    assert "'title' is a required property" in "\n".join(lines)
    _assert_no_unsafe_characters("".join(lines))


def test_receipt_schema_failures_keep_one_line_per_issue(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    receipt = cast(
        JsonObject,
        json.loads(
            (REFERENCE_BUNDLE / "reasoning-receipt.json").read_text(encoding="utf-8")
        ),
    )
    del receipt["limitations"]
    receipt["receipt_version"] = FORGED_TEXT
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    result = main(["receipt", "validate", str(receipt_path)])

    lines = capsys.readouterr().err.split("\n")
    assert result == 1
    assert len(lines) == 3
    assert lines[0].startswith("FAIL $")
    assert lines[2] == ""
    _assert_no_unsafe_characters("".join(lines))


def test_blank_trusted_authorizer_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(
            [
                "ledger",
                "validate",
                str(REFERENCE_BUNDLE / "ledger.jsonl"),
                "--trusted-authorizer",
                " ",
            ]
        )

    assert raised.value.code == 2
    assert "actor id must not be blank" in capsys.readouterr().err


POLICY_COMMANDS = (
    ["bundle", "create", "ledger.jsonl", "bundle", "--title", "Bundle"],
    ["bundle", "validate", "bundle"],
    ["ledger", "validate", "ledger.jsonl"],
    ["ledger", "append", "ledger.jsonl", "record.json"],
    ["ledger", "replay", "ledger.jsonl"],
    ["ledger", "summary", "ledger.jsonl"],
    ["receipt", "generate", "ledger.jsonl"],
    ["receipt", "validate", "receipt.json"],
)


@pytest.mark.parametrize("command", POLICY_COMMANDS, ids=" ".join)
def test_policy_options_are_accepted_wherever_the_trust_list_is(
    command: list[str],
) -> None:
    parser = cli_module._parser()  # pyright: ignore[reportPrivateUsage]
    options = [
        "--trusted-authorizer",
        "reviewer-alex",
        "--require-supported-reliance",
        "--as-of",
        "2026-08-01T02:00:00+02:00",
    ]

    policy = cli_module._validation_policy(  # pyright: ignore[reportPrivateUsage]
        parser.parse_args([*command, *options])
    )

    assert policy == ValidationPolicy(
        trusted_authorizers=frozenset({"reviewer-alex"}),
        require_supported_reliance=True,
        as_of=datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert (
        cli_module._validation_policy(  # pyright: ignore[reportPrivateUsage]
            parser.parse_args(command)
        )
        is None
    )


def _write_ledger(path: Path, records: list[JsonObject]) -> None:
    JsonlLedgerStore(path).extend(records)


def _reliance_before_support() -> list[JsonObject]:
    records = _history()
    position = [record["id"] for record in records].index("transition-cache-under-test")
    actor: JsonObject = {
        "id": "reviewer-alex",
        "actor_type": "human",
        "role": "reviewer",
    }
    decision: JsonObject = {
        "protocol_version": "0.1.0-alpha.3",
        "kind": "decision",
        "id": "decision-cache-fix",
        "created_at": "2026-07-22T13:00:00Z",
        "created_by": dict(actor),
        "question": "May the cache fix ship?",
        "disposition": "approved",
        "authorized_by": dict(actor),
        "scope": {"description": "Controlled cache-failure case, harness version 1"},
        "relied_on_claim_refs": ["claim-cache-cause"],
    }
    return [*records[: position + 1], decision, *records[position + 1 :]]


def test_require_supported_reliance_option_refuses_unsupported_reliance(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "assurance.jsonl"
    _write_ledger(path, _reliance_before_support())

    default = main(["ledger", "validate", str(path)])
    capsys.readouterr()
    required = main(["ledger", "validate", str(path), "--require-supported-reliance"])

    assert default == 0
    assert required == 1
    assert (
        "unsupported_reliance: decision-cache-fix [claim-cache-cause]: approved "
        "decision relies on a subject that has replayed status under_test at the "
        "decision"
    ) in capsys.readouterr().err


def test_as_of_option_refuses_expired_support(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "assurance.jsonl"
    records = _history()
    review = next(
        record for record in records if record["id"] == "evidence-cache-review"
    )
    review["expires_at"] = "2026-08-01T00:00:00Z"
    _write_ledger(path, records)

    before = main(["ledger", "summary", str(path), "--as-of", "2026-07-31T23:59:59Z"])
    capsys.readouterr()
    after = main(
        ["ledger", "summary", str(path), "--as-of", "2026-08-01t02:00:00+02:00"]
    )

    assert before == 0
    assert after == 1
    assert (
        "expired_support: claim-cache-cause [evidence-cache-review]: "
        "corroborated_within_scope status rests on evidence whose expires_at "
        "2026-08-01T00:00:00Z is at or before 2026-08-01T00:00:00+00:00"
    ) in capsys.readouterr().err


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("2026-08-01T00:00:00", "is not an RFC 3339 date-time with an offset"),
        ("2026-08-01", "is not an RFC 3339 date-time with an offset"),
        ("2026-08-01 00:00:00Z", "is not an RFC 3339 date-time with an offset"),
        ("2026-08-01T00:00:00+0200", "is not an RFC 3339 date-time with an offset"),
        ("tomorrow", "is not an RFC 3339 date-time with an offset"),
        ("2026-02-30T00:00:00Z", "is not an ISO 8601 date-time"),
        ("2026-07-31T23:59:60Z", "is not an ISO 8601 date-time"),
        ("0001-01-01T00:00:00+01:00", "is outside the representable date-time range"),
    ],
)
def test_as_of_option_requires_an_rfc3339_date_time_with_an_offset(
    capsys: pytest.CaptureFixture[str],
    value: str,
    message: str,
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(
            [
                "ledger",
                "validate",
                str(REFERENCE_BUNDLE / "ledger.jsonl"),
                "--as-of",
                value,
            ]
        )

    assert raised.value.code == 2
    assert f"{value!r} {message}" in capsys.readouterr().err


@pytest.mark.parametrize(
    "options",
    [
        ["--require-supported-reliance"],
        ["--as-of", "2026-08-01T00:00:00Z"],
        ["--require-supported-reliance", "--as-of", "2026-08-01T00:00:00Z"],
    ],
    ids=["reliance", "as-of", "both"],
)
def test_receipt_validate_refuses_a_policy_without_the_ledger(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    options: list[str],
) -> None:
    ledger_path = tmp_path / "assurance.jsonl"
    receipt_path = tmp_path / "receipt.json"
    _write_ledger(ledger_path, _history())
    assert (
        main(["receipt", "generate", str(ledger_path), "--output", str(receipt_path)])
        == 0
    )
    capsys.readouterr()

    result = main(["receipt", "validate", str(receipt_path), *options])

    assert result == 1
    assert f"{options[0]} requires --ledger" in capsys.readouterr().err
    assert (
        main(
            [
                "receipt",
                "validate",
                str(receipt_path),
                "--ledger",
                str(ledger_path),
                *options,
            ]
        )
        == 0
    )


def test_ledger_append_applies_the_cli_ledger_limits(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path = tmp_path / "assurance.jsonl"
    records = _history()
    _write_ledger(ledger_path, records[:-1])
    stored = ledger_path.read_bytes()
    record_path = tmp_path / "record.json"
    record_path.write_text(json.dumps(records[-1]), encoding="utf-8")
    append = ["ledger", "append", str(ledger_path), str(record_path)]

    monkeypatch.setattr(
        cli_module,
        "_CLI_LIMITS",
        EvidenceBundleLimits(max_ledger_records=len(records) - 1),
    )
    by_count = main(append)
    count_error = capsys.readouterr().err
    monkeypatch.setattr(
        cli_module,
        "_CLI_LIMITS",
        EvidenceBundleLimits(max_ledger_bytes=len(stored)),
    )
    by_size = main(append)
    size_error = capsys.readouterr().err

    assert by_count == 1
    assert f"ledger record count exceeds limit {len(records) - 1}" in count_error
    assert by_size == 1
    assert f"ledger size exceeds limit {len(stored)} bytes" in size_error
    assert ledger_path.read_bytes() == stored


# DEL, C1 controls, and the line and paragraph separators that json.dumps
# leaves raw, beside non-ASCII text that is safe to print.
JSON_UNSAFE_TEXT = "id\x7f\x80\x85\x9f\u2028\u2029"
JSON_SAFE_TEXT = "café→Ω"


def _assert_terminal_json(text: str) -> JsonValue:
    _assert_no_unsafe_characters(text)
    for escape in ("\\u007f", "\\u0080", "\\u0085", "\\u009f", "\\u2028", "\\u2029"):
        assert escape in text
    assert JSON_SAFE_TEXT in text
    return cast(JsonValue, json.loads(text))


@pytest.mark.parametrize(
    "command",
    [["ledger", "replay"], ["receipt", "generate"]],
    ids=" ".join,
)
def test_json_output_escapes_characters_that_could_forge_terminal_lines(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: list[str],
) -> None:
    record_id = JSON_UNSAFE_TEXT + JSON_SAFE_TEXT
    ledger_path = tmp_path / "ledger.jsonl"
    JsonlLedgerStore(ledger_path).append(_claim_with_id(record_id))
    ledger = JsonlLedgerStore(ledger_path).load()
    options = ["--format", "json"] if command[0] == "ledger" else []

    result = main([*command, str(ledger_path), *options])

    output = _assert_terminal_json(capsys.readouterr().out)
    assert result == 0
    if command[0] == "ledger":
        assert output == [
            subject.to_json_object() for subject in replay_subjects(ledger)
        ]
    else:
        assert output == build_reasoning_receipt(ledger)


def test_summary_json_output_is_terminal_safe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    JsonlLedgerStore(ledger_path).append(_claim_with_id(JSON_UNSAFE_TEXT))

    result = main(["ledger", "summary", str(ledger_path), "--format", "json"])

    output = capsys.readouterr().out
    _assert_no_unsafe_characters(output)
    assert result == 0
    assert (
        json.loads(output)
        == summarize_ledger(JsonlLedgerStore(ledger_path).load()).to_json_object()
    )


def test_files_the_stores_write_keep_their_bytes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record_id = JSON_UNSAFE_TEXT + JSON_SAFE_TEXT
    ledger_path = tmp_path / "ledger.jsonl"
    receipt_path = tmp_path / "receipt.json"
    expected_path = tmp_path / "expected.json"
    JsonlLedgerStore(ledger_path).append(_claim_with_id(record_id))
    JsonReceiptStore(expected_path).write(
        build_reasoning_receipt(JsonlLedgerStore(ledger_path).load())
    )

    result = main(
        ["receipt", "generate", str(ledger_path), "--output", str(receipt_path)]
    )

    capsys.readouterr()
    assert result == 0
    assert receipt_path.read_bytes() == expected_path.read_bytes()
    assert "\u2028".encode() in receipt_path.read_bytes()
    assert "\u2028".encode() in ledger_path.read_bytes()


def test_module_entry_point_reports_the_itself_program_name() -> None:
    completed = subprocess.run(
        (sys.executable, "-m", "itself.cli", "--version"),
        check=False,
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == f"itself {__version__}\n"
