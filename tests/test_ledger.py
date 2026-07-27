# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from itself import (
    BundleIntegrityError,
    ClaimStatus,
    JsonlLedgerStore,
    JsonObject,
    JsonValue,
    Ledger,
    LedgerFormatError,
)

ROOT = Path(__file__).resolve().parents[1]
VALID_HISTORY = (
    ROOT / "conformance" / "bundles" / "valid" / "evidence-backed-history.json"
)


def _records() -> list[JsonObject]:
    value = cast(JsonValue, json.loads(VALID_HISTORY.read_text(encoding="utf-8")))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError("valid history fixture must contain a JSON object array")
    return cast(list[JsonObject], value)


def test_ledger_extends_and_replays_complete_history() -> None:
    records = _records()
    ledger = Ledger(records[:1])

    snapshot = ledger.extend(records[1:])

    assert len(ledger) == len(records)
    assert (
        snapshot.current_states["claim-cache-cause"]
        is ClaimStatus.CORROBORATED_WITHIN_SCOPE
    )


def test_invalid_append_does_not_mutate_ledger() -> None:
    claim = _records()[0]
    ledger = Ledger([claim])
    before_records = ledger.records
    before_snapshot = ledger.snapshot

    with pytest.raises(BundleIntegrityError):
        ledger.append(claim)

    assert ledger.records == before_records
    assert ledger.snapshot is before_snapshot


def test_invalid_batch_does_not_partially_mutate_ledger() -> None:
    records = _records()
    ledger = Ledger(records[:1])
    invalid_batch = [records[1], records[0]]

    with pytest.raises(BundleIntegrityError):
        ledger.extend(invalid_batch)

    assert len(ledger) == 1
    assert ledger.snapshot.current_states["claim-cache-cause"] is ClaimStatus.PROPOSED


def test_ledger_owns_defensive_record_copies() -> None:
    original = _records()[0]
    ledger = Ledger([original])

    original["proposition"] = "caller mutation"
    exposed = ledger.records[0]
    exposed["proposition"] = "returned-copy mutation"

    assert ledger.records[0]["proposition"] == (
        "A stale cache causes the observed verification failure."
    )


def test_iteration_returns_defensive_copies() -> None:
    ledger = Ledger([_records()[0]])
    iterated = next(iter(ledger))

    iterated["proposition"] = "iteration mutation"

    assert ledger.records[0]["proposition"] != "iteration mutation"


def test_jsonl_store_round_trips_order_and_state(tmp_path: Path) -> None:
    records = _records()
    store = JsonlLedgerStore(tmp_path / "assurance.jsonl")

    written_snapshot = store.extend(records)
    loaded = store.load()

    assert store.path.read_text(encoding="utf-8").count("\n") == len(records)
    assert loaded.records == tuple(records)
    assert loaded.snapshot.record_ids == written_snapshot.record_ids
    assert loaded.snapshot.current_states == written_snapshot.current_states


def test_jsonl_store_stops_at_configured_record_limit(tmp_path: Path) -> None:
    store = JsonlLedgerStore(tmp_path / "assurance.jsonl")
    store.extend(_records())

    with pytest.raises(LedgerFormatError, match="record count exceeds limit 1"):
        store.load(max_records=1)


def test_jsonl_store_rejects_negative_record_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        JsonlLedgerStore(tmp_path / "assurance.jsonl").load(max_records=-1)


def test_jsonl_store_rejects_ledger_over_byte_limit(tmp_path: Path) -> None:
    store = JsonlLedgerStore(tmp_path / "assurance.jsonl")
    store.extend(_records())

    with pytest.raises(LedgerFormatError, match="size exceeds limit"):
        store.load(max_bytes=store.path.stat().st_size - 1)


def test_jsonl_store_rejects_negative_byte_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        JsonlLedgerStore(tmp_path / "assurance.jsonl").load(max_bytes=-1)


def test_jsonl_encoding_is_deterministic(tmp_path: Path) -> None:
    records = _records()
    first = JsonlLedgerStore(tmp_path / "first.jsonl")
    second = JsonlLedgerStore(tmp_path / "second.jsonl")

    first.extend(records)
    second.extend(records)

    assert first.path.read_bytes() == second.path.read_bytes()


def test_invalid_persisted_append_leaves_file_unchanged(tmp_path: Path) -> None:
    claim = _records()[0]
    store = JsonlLedgerStore(tmp_path / "assurance.jsonl")
    store.append(claim)
    before = store.path.read_bytes()

    with pytest.raises(BundleIntegrityError):
        store.append(claim)

    assert store.path.read_bytes() == before


def test_empty_extend_does_not_create_file(tmp_path: Path) -> None:
    store = JsonlLedgerStore(tmp_path / "assurance.jsonl")

    snapshot = store.extend([])

    assert snapshot.record_count == 0
    assert not store.path.exists()


@pytest.mark.parametrize(
    ("content", "expected_detail"),
    [
        (b"{}\n\n", "blank lines"),
        (b"[]\n", "one JSON object"),
        (b"{not-json}\n", "invalid JSON"),
        (b'{"value":NaN}\n', "non-standard JSON numeric constant"),
        (b'{"id":"first","id":"second"}\n', "duplicate JSON object key"),
        (b"\xff\n", "not valid UTF-8"),
    ],
)
def test_jsonl_store_rejects_malformed_lines(
    tmp_path: Path,
    content: bytes,
    expected_detail: str,
) -> None:
    path = tmp_path / "malformed.jsonl"
    path.write_bytes(content)

    with pytest.raises(LedgerFormatError) as raised:
        JsonlLedgerStore(path).load()

    assert expected_detail in raised.value.detail


def test_jsonl_error_reports_exact_line_number(tmp_path: Path) -> None:
    path = tmp_path / "malformed.jsonl"
    valid_line = json.dumps(_records()[0], separators=(",", ":"))
    path.write_text(f"{valid_line}\n{{not-json}}\n", encoding="utf-8")

    with pytest.raises(LedgerFormatError) as raised:
        JsonlLedgerStore(path).load()

    assert raised.value.path == path
    assert raised.value.line_number == 2


def test_jsonl_store_rejects_symbolic_link_path(tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_text("{}\n", encoding="utf-8")
    path = tmp_path / "ledger.jsonl"
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(OSError, match="must not be a symbolic link"):
        JsonlLedgerStore(path).load()
    with pytest.raises(OSError, match="must not be a symbolic link"):
        JsonlLedgerStore(path).append(_records()[0])

    assert path.is_symlink()
    assert target.read_text(encoding="utf-8") == "{}\n"


def test_jsonl_store_rejects_dangling_symbolic_link_path(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    try:
        path.symlink_to(tmp_path / "missing.jsonl")
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(OSError, match="must not be a symbolic link"):
        JsonlLedgerStore(path).load()

    assert path.is_symlink()
