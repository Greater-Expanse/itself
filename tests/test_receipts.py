# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

from experiments.cases.cache_key_scripted_run import run_scripted_case
from itself import (
    JsonlLedgerStore,
    JsonObject,
    JsonReceiptStore,
    JsonValue,
    Ledger,
    ReasoningReceiptFormatError,
    ReasoningReceiptValidationError,
    ReasoningReceiptValidator,
    build_reasoning_receipt,
    canonical_ledger_bytes,
    ledger_sha256,
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


def _objects(receipt: JsonObject, field: str) -> list[JsonObject]:
    value = receipt[field]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError(f"receipt field {field!r} must contain object values")
    return cast(list[JsonObject], value)


def test_receipt_is_deterministic_and_bound_to_canonical_ledger() -> None:
    ledger = _ledger()

    first = build_reasoning_receipt(ledger)
    second = build_reasoning_receipt(ledger)
    digest = ledger_sha256(ledger)

    assert first == second
    assert first["receipt_id"] == f"urn:sha256:{digest}"
    assert first["source_ledger"] == {
        "canonicalization": "rfc8785-jsonl-v1",
        "record_count": 10,
        "protocol_versions": ["0.1.0-alpha.3"],
        "digest": {"algorithm": "sha256", "value": digest},
    }
    ReasoningReceiptValidator().validate_against_ledger(first, ledger)


def test_canonical_bytes_match_jsonl_store_representation(tmp_path: Path) -> None:
    ledger = _ledger()
    path = tmp_path / "ledger.jsonl"
    JsonlLedgerStore(path).extend(ledger.records)

    assert path.read_bytes() == canonical_ledger_bytes(ledger)


def test_receipt_is_identical_before_and_after_canonical_ledger_round_trip(
    tmp_path: Path,
) -> None:
    ledger = run_scripted_case().ledger
    path = tmp_path / "ledger.jsonl"
    JsonlLedgerStore(path).extend(ledger.records)

    assert build_reasoning_receipt(ledger) == build_reasoning_receipt(
        JsonlLedgerStore(path).load()
    )


def test_receipt_projects_public_state_and_authority_path() -> None:
    receipt = build_reasoning_receipt(_ledger())
    subjects = _objects(receipt, "subjects")
    transitions = _objects(receipt, "transitions")
    evidence = _objects(receipt, "evidence")

    assert subjects == [
        {
            "subject_id": "claim-cache-cause",
            "kind": "claim",
            "text": "A stale cache causes the observed verification failure.",
            "scope_description": "Controlled cache-failure case, harness version 1",
            "initial_status": "proposed",
            "current_status": "corroborated_within_scope",
            "transition_ids": [
                "transition-cache-testable",
                "transition-cache-under-test",
                "transition-cache-supported",
                "transition-cache-corroborated",
            ],
        }
    ]
    assert transitions[-1]["evidence_refs"] == [
        "evidence-cache-ablation",
        "evidence-cache-review",
    ]
    assert transitions[-1]["verdict_ref"] == "verdict-cache-corroborated"
    assert transitions[-1]["policy_ref"] == "policy-corroboration-v1"
    assert cast(JsonObject, evidence[-1]["authority"])["authority_type"] == (
        "human_reviewer"
    )


def test_case_receipt_separates_artifacts_tests_evidence_and_verdicts() -> None:
    receipt = build_reasoning_receipt(run_scripted_case().ledger)
    artifacts = _objects(receipt, "artifacts")

    assert len(artifacts) == 1
    assert set(artifacts[0]) == {"id", "uri", "media_type", "title", "digest"}
    assert len(_objects(receipt, "tests")) == 2
    assert len(_objects(receipt, "evidence")) == 2
    assert len(_objects(receipt, "verdicts")) == 3
    assert len(_objects(receipt, "transitions")) == 9
    assert not _objects(receipt, "decisions")


def test_schema_valid_tampering_is_rejected_against_source_ledger() -> None:
    ledger = _ledger()
    receipt = deepcopy(build_reasoning_receipt(ledger))
    subjects = _objects(receipt, "subjects")
    subjects[0]["current_status"] = "supported"

    assert ReasoningReceiptValidator().is_valid(receipt)
    with pytest.raises(ReasoningReceiptValidationError, match="does not match"):
        ReasoningReceiptValidator().validate_against_ledger(receipt, ledger)


def test_empty_ledger_produces_valid_explicitly_limited_receipt() -> None:
    receipt = build_reasoning_receipt(Ledger())

    assert receipt["record_counts"] == {}
    assert receipt["subjects"] == []
    limitations = receipt["limitations"]
    assert isinstance(limitations, list)
    assert "Structural integrity does not establish factual truth." in limitations
    assert ReasoningReceiptValidator().is_valid(receipt)


def test_receipt_store_round_trips_deterministically(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    receipt = build_reasoning_receipt(_ledger())
    store = JsonReceiptStore(path)

    store.write(receipt)
    first_bytes = path.read_bytes()
    store.write(receipt)

    assert store.load() == receipt
    assert path.read_bytes() == first_bytes
    assert first_bytes.endswith(b"\n")


def test_receipt_store_rejects_document_over_byte_limit(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    store = JsonReceiptStore(path)
    store.write(build_reasoning_receipt(_ledger()))

    with pytest.raises(ReasoningReceiptFormatError, match="size exceeds limit"):
        store.load(max_bytes=path.stat().st_size - 1)


def test_receipt_store_rejects_negative_byte_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        JsonReceiptStore(tmp_path / "receipt.json").load(max_bytes=-1)


def test_invalid_receipt_never_replaces_existing_store(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    receipt = build_reasoning_receipt(_ledger())
    store = JsonReceiptStore(path)
    store.write(receipt)
    before = path.read_bytes()
    invalid = deepcopy(receipt)
    del invalid["source_ledger"]

    with pytest.raises(ReasoningReceiptValidationError):
        store.write(invalid)

    assert path.read_bytes() == before


def test_receipt_store_rejects_non_object_json(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(ReasoningReceiptFormatError):
        JsonReceiptStore(path).load()


def test_receipt_store_rejects_duplicate_object_keys(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    path.write_text(
        '{"receipt_version":"first","receipt_version":"second"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ReasoningReceiptFormatError, match="duplicate JSON object key"):
        JsonReceiptStore(path).load()


def test_receipt_store_rejects_symbolic_link_path(tmp_path: Path) -> None:
    receipt = build_reasoning_receipt(_ledger())
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    path = tmp_path / "receipt.json"
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(OSError, match="must not be a symbolic link"):
        JsonReceiptStore(path).load()
    with pytest.raises(OSError, match="must not be a symbolic link"):
        JsonReceiptStore(path).write(receipt)

    assert path.is_symlink()
    assert target.read_text(encoding="utf-8") == "{}\n"
