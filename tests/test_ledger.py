# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import errno
import json
import os
import re
import stat
from collections.abc import Callable, Collection, Iterable
from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import cast

import pytest
import rfc8785

import itself._filesystem as filesystem_module
from itself import (
    BundleIntegrityError,
    BundleSnapshot,
    BundleValidator,
    ClaimStatus,
    IntegrityCode,
    JsonlLedgerStore,
    JsonObject,
    JsonValue,
    Ledger,
    LedgerFormatError,
    ProtocolValidator,
)
from itself.ledger import decode_jsonl_records

ROOT = Path(__file__).resolve().parents[1]
VALID_HISTORY = (
    ROOT / "conformance" / "bundles" / "valid" / "evidence-backed-history.json"
)


def _records() -> list[JsonObject]:
    value = cast(JsonValue, json.loads(VALID_HISTORY.read_text(encoding="utf-8")))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError("valid history fixture must contain a JSON object array")
    return cast(list[JsonObject], value)


class _CountingProtocolValidator(ProtocolValidator):
    """Count per-record schema checks without changing their results."""

    def __init__(self) -> None:
        super().__init__()
        self.checked = 0

    def errors(self, record: JsonValue) -> list[str]:
        self.checked += 1
        return super().errors(record)


def _issue_codes(error: BundleIntegrityError) -> list[IntegrityCode]:
    return [issue.code for issue in error.issues]


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


def test_append_schema_checks_only_the_new_record() -> None:
    records = _records()
    schema = _CountingProtocolValidator()
    ledger = Ledger(records[:-1], validator=BundleValidator(schema))
    schema.checked = 0

    snapshot = ledger.append(records[-1])

    assert schema.checked == 1
    assert (
        snapshot.current_states["claim-cache-cause"]
        is ClaimStatus.CORROBORATED_WITHIN_SCOPE
    )


def test_append_still_rejects_a_schema_invalid_record() -> None:
    records = _records()
    ledger = Ledger(records[:1])
    invalid = deepcopy(records[1])
    del invalid["reason"]

    with pytest.raises(BundleIntegrityError) as raised:
        ledger.append(invalid)

    issues = raised.value.issues
    assert {issue.code for issue in issues} == {IntegrityCode.SCHEMA_INVALID}
    assert {issue.record_id for issue in issues} == {"transition-cache-testable"}
    assert "$: 'reason' is a required property" in [issue.message for issue in issues]
    assert len(ledger) == 1


def test_append_still_rejects_a_duplicate_of_an_earlier_record() -> None:
    records = _records()
    ledger = Ledger(records)
    duplicate = deepcopy(records[0])
    duplicate["proposition"] = "A different proposition under a reused identifier."

    with pytest.raises(BundleIntegrityError) as raised:
        ledger.append(duplicate)

    assert _issue_codes(raised.value) == [IntegrityCode.DUPLICATE_ID]
    assert len(ledger) == len(records)


def test_append_still_replays_state_across_the_existing_history() -> None:
    records = _records()
    ledger = Ledger(records)
    stale = deepcopy(records[1])
    stale["id"] = "transition-cache-testable-again"

    with pytest.raises(BundleIntegrityError) as raised:
        ledger.append(stale)

    assert _issue_codes(raised.value) == [IntegrityCode.STATE_MISMATCH]


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


def test_jsonl_store_append_runs_one_full_validation(tmp_path: Path) -> None:
    records = _records()
    schema = _CountingProtocolValidator()
    store = JsonlLedgerStore(
        tmp_path / "assurance.jsonl",
        validator=BundleValidator(schema),
    )
    store.extend(records[:-1])
    schema.checked = 0

    # A store that did not write the history loads and checks it once; the
    # append checks only itself.
    fresh = JsonlLedgerStore(store.path, validator=BundleValidator(schema))
    fresh.append(records[-1])

    assert schema.checked == len(records)


def test_warm_jsonl_store_append_schema_checks_only_the_addition(
    tmp_path: Path,
) -> None:
    records = _records()
    schema = _CountingProtocolValidator()
    store = JsonlLedgerStore(
        tmp_path / "assurance.jsonl",
        validator=BundleValidator(schema),
    )
    store.extend(records[:-1])
    schema.checked = 0

    store.append(records[-1])

    # The instance wrote the stored bytes, so it holds their validated records.
    assert schema.checked == 1


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


@pytest.mark.parametrize(
    ("content", "max_records", "max_bytes"),
    [
        (b"{}\n\n", None, None),
        (b"[]\n", None, None),
        (b'{"value":\n', None, None),
        (b"\xff\n", None, None),
        (b"{}\n{}\n", 1, None),
        (b"{}\n{}\n", None, 5),
    ],
)
def test_bytes_decoder_reports_file_loader_errors(
    tmp_path: Path,
    content: bytes,
    max_records: int | None,
    max_bytes: int | None,
) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(content)

    with pytest.raises(LedgerFormatError) as from_file:
        JsonlLedgerStore(path).load(max_records=max_records, max_bytes=max_bytes)
    with pytest.raises(LedgerFormatError) as from_bytes:
        decode_jsonl_records(
            content,
            path=path,
            max_records=max_records,
            max_bytes=max_bytes,
        )

    assert str(from_bytes.value) == str(from_file.value)
    assert from_bytes.value.line_number == from_file.value.line_number


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


def test_store_refuses_a_record_its_reader_could_not_read_back(
    tmp_path: Path,
) -> None:
    records = _records()
    store = JsonlLedgerStore(tmp_path / "ledger.jsonl")
    store.extend(records[:4])
    before = store.path.read_bytes()
    nested: JsonValue = 1
    for _ in range(127):
        nested = [nested]
    evidence = deepcopy(records[3])
    evidence["id"] = "evidence-nested-too-deep"
    evidence["result"] = {"value": nested}

    with pytest.raises(
        BundleIntegrityError, match="nesting exceeds 128 levels"
    ) as error:
        store.append(evidence)

    assert _issue_codes(error.value) == [IntegrityCode.SCHEMA_INVALID]
    assert store.path.read_bytes() == before
    assert store.load().snapshot.record_count == 4


class _InterleavingValidator(BundleValidator):
    """Let another writer act after the store has read the ledger."""

    def __init__(self, interleave: Callable[[], None]) -> None:
        super().__init__()
        self._interleave: Callable[[], None] | None = interleave

    def validate(
        self,
        records: Iterable[JsonObject],
        *,
        external_refs: Collection[str] = (),
    ) -> BundleSnapshot:
        interleave, self._interleave = self._interleave, None
        if interleave is not None:
            interleave()
        return super().validate(records, external_refs=external_refs)


def _append_with_another_store(path: Path, record: JsonObject) -> None:
    JsonlLedgerStore(path).append(record)


def _append_in_place(path: Path, record: JsonObject) -> None:
    with path.open("ab") as handle:
        handle.write(json.dumps(record).encode() + b"\n")


@pytest.mark.parametrize(
    "other_writer",
    [_append_with_another_store, _append_in_place],
    ids=["replaced-by-another-store", "modified-in-place"],
)
def test_jsonl_store_refuses_to_overwrite_a_concurrent_append(
    tmp_path: Path,
    other_writer: Callable[[Path, JsonObject], None],
) -> None:
    records = _records()
    path = tmp_path / "assurance.jsonl"
    JsonlLedgerStore(path).append(records[0])
    other_bytes: list[bytes] = []

    def interleave() -> None:
        other_writer(path, records[1])
        other_bytes.append(path.read_bytes())

    store = JsonlLedgerStore(path, validator=_InterleavingValidator(interleave))
    with pytest.raises(OSError, match="changed concurrently and was not replaced"):
        store.append(records[1])

    assert path.read_bytes() == other_bytes[0]
    assert not list(tmp_path.glob(".assurance.jsonl.*.tmp"))


def test_jsonl_store_refuses_a_ledger_created_concurrently(tmp_path: Path) -> None:
    records = _records()
    path = tmp_path / "assurance.jsonl"

    def interleave() -> None:
        JsonlLedgerStore(path).append(records[0])

    store = JsonlLedgerStore(path, validator=_InterleavingValidator(interleave))
    with pytest.raises(OSError, match="created concurrently and was not replaced"):
        store.append(records[0])

    assert JsonlLedgerStore(path).load().snapshot.record_count == 1


def test_jsonl_store_refuses_a_ledger_removed_concurrently(tmp_path: Path) -> None:
    records = _records()
    path = tmp_path / "assurance.jsonl"
    JsonlLedgerStore(path).append(records[0])

    store = JsonlLedgerStore(path, validator=_InterleavingValidator(path.unlink))
    with pytest.raises(OSError, match="removed concurrently and was not replaced"):
        store.append(records[1])

    assert not path.exists()


def test_jsonl_store_rechecks_for_a_symbolic_link_before_replacing(
    tmp_path: Path,
) -> None:
    records = _records()
    path = tmp_path / "assurance.jsonl"
    target = tmp_path / "target.jsonl"
    target.write_text("{}\n", encoding="utf-8")
    JsonlLedgerStore(path).append(records[0])
    try:
        (tmp_path / "probe").symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    def interleave() -> None:
        path.unlink()
        path.symlink_to(target)

    store = JsonlLedgerStore(path, validator=_InterleavingValidator(interleave))
    with pytest.raises(OSError, match="must not be a symbolic link"):
        store.append(records[1])

    assert path.is_symlink()
    assert target.read_text(encoding="utf-8") == "{}\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode semantics")
def test_jsonl_store_append_keeps_the_ledger_permissions(tmp_path: Path) -> None:
    records = _records()
    store = JsonlLedgerStore(tmp_path / "assurance.jsonl")
    store.append(records[0])
    store.path.chmod(0o640)

    store.append(records[1])

    assert stat.S_IMODE(store.path.stat().st_mode) == 0o640


def test_jsonl_store_rejects_a_path_that_is_not_a_regular_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "assurance.jsonl"
    path.mkdir()

    with pytest.raises(OSError, match="must be a regular file"):
        JsonlLedgerStore(path).load()
    with pytest.raises(OSError, match="must be a regular file"):
        JsonlLedgerStore(path).append(_records()[0])

    assert path.is_dir()
    assert not list(path.iterdir())


def test_jsonl_store_synchronizes_the_directory_after_the_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "ledger" / "assurance.jsonl"
    synchronized: list[tuple[Path, bool]] = []
    synchronize = filesystem_module.fsync_directory

    def recording_fsync_directory(directory: Path) -> None:
        synchronized.append((directory, path.is_file()))
        synchronize(directory)

    monkeypatch.setattr(filesystem_module, "fsync_directory", recording_fsync_directory)

    JsonlLedgerStore(path).append(_records()[0])

    assert synchronized == [(path.parent, True)]


def _failing_fsync_directory(code: int) -> Callable[[Path], None]:
    def fail(directory: Path) -> None:
        raise OSError(code, os.strerror(code), str(directory))

    return fail


@pytest.mark.parametrize("name", ["EINVAL", "ENOTSUP", "EOPNOTSUPP", "EACCES", "EPERM"])
def test_jsonl_store_append_completes_when_the_directory_cannot_be_synchronized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    records = _records()
    monkeypatch.setattr(
        filesystem_module,
        "fsync_directory",
        _failing_fsync_directory(getattr(errno, name)),
    )
    store = JsonlLedgerStore(tmp_path / "assurance.jsonl")

    store.append(records[0])
    store.append(records[1])

    assert store.load().records == tuple(records[:2])


def test_jsonl_store_append_reports_another_sync_failure_after_storing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _records()
    monkeypatch.setattr(
        filesystem_module, "fsync_directory", _failing_fsync_directory(errno.EIO)
    )
    store = JsonlLedgerStore(tmp_path / "assurance.jsonl")

    with pytest.raises(OSError) as raised:
        store.append(records[0])

    # The replacement had already happened, as the store documents.
    assert raised.value.errno == errno.EIO
    assert store.load().records == (records[0],)


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="needs POSIX permissions that bind the current user",
)
def test_jsonl_store_appends_in_a_directory_it_cannot_read(tmp_path: Path) -> None:
    records = _records()
    directory = tmp_path / "write-only"
    directory.mkdir()
    store = JsonlLedgerStore(directory / "assurance.jsonl")
    directory.chmod(0o300)
    try:
        store.append(records[0])
        store.append(records[1])
    finally:
        directory.chmod(0o700)

    assert store.load().records == tuple(records[:2])


def test_ledger_gives_a_validator_that_overrides_validate_every_record() -> None:
    records = _records()
    seen: list[int] = []

    class RecordingValidator(BundleValidator):
        def validate(
            self,
            records: Iterable[JsonObject],
            *,
            external_refs: Collection[str] = (),
        ) -> BundleSnapshot:
            candidate = tuple(records)
            seen.append(len(candidate))
            return super().validate(candidate, external_refs=external_refs)

    ledger = Ledger(records[:1], validator=RecordingValidator())
    ledger.extend(records[1:3])

    assert seen == [1, 3]
    assert len(ledger) == 3


def _claim(index: int) -> JsonObject:
    claim = deepcopy(_records()[0])
    claim["id"] = f"claim-{index}"
    claim["proposition"] = f"Proposition number {index}."
    return claim


def _without_paths(message: str) -> str:
    return re.sub(r"/[^\s:]*\.jsonl", "<ledger>", message)


def _outcome(
    action: Callable[[], BundleSnapshot | Ledger],
) -> tuple[str, object, object]:
    """Return what an operation produced or raised, in a comparable form."""

    try:
        result = action()
    except BundleIntegrityError as error:
        return ("integrity", str(error), error.issues)
    except (OSError, ValueError) as error:
        return (type(error).__name__, _without_paths(str(error)), None)
    if isinstance(result, Ledger):
        return ("ok", result.snapshot, result.records)
    return ("ok", result, None)


def _cold_copy(path: Path, directory: Path) -> JsonlLedgerStore:
    """Return a store that has read nothing, over a byte copy of the ledger."""

    copy = directory / "cold.jsonl"
    copy.write_bytes(path.read_bytes())
    return JsonlLedgerStore(copy)


def test_warm_store_matches_a_fresh_store_for_every_append(tmp_path: Path) -> None:
    warm = JsonlLedgerStore(tmp_path / "warm" / "ledger.jsonl")
    cold_path = tmp_path / "cold" / "ledger.jsonl"
    invalid = _claim(99)
    del invalid["proposition"]
    duplicate = _claim(2)
    duplicate["proposition"] = "A reused identifier."
    attempts = [_claim(0), _claim(1), _claim(2), invalid, duplicate, _claim(3)]

    kinds: list[str] = []
    for record in attempts:
        warm_result = _outcome(partial(warm.append, record))
        cold_result = _outcome(partial(JsonlLedgerStore(cold_path).append, record))

        assert warm_result == cold_result
        assert warm.path.read_bytes() == cold_path.read_bytes()
        kinds.append(warm_result[0])

    assert kinds == ["ok", "ok", "ok", "integrity", "integrity", "ok"]


def _warm_store(tmp_path: Path, count: int = 3) -> JsonlLedgerStore:
    store = JsonlLedgerStore(tmp_path / "warm" / "ledger.jsonl")
    for index in range(count):
        store.append(_claim(index))
    return store


def _edit_in_place(path: Path, old: bytes, new: bytes) -> None:
    """Replace same-size bytes in place and restore the modification time.

    The file keeps its device, inode, size and modification time, so only the
    digest of the bytes can tell the store that they changed.
    """

    assert len(old) == len(new)
    before = path.stat()
    identity = filesystem_module.FileIdentity.of(before)
    content = path.read_bytes()
    assert old in content
    with path.open("r+b") as handle:
        handle.write(content.replace(old, new, 1))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert filesystem_module.FileIdentity.of(path.stat()) == identity


def _assert_same_as_a_fresh_store(
    warm: JsonlLedgerStore,
    record: JsonObject,
    directory: Path,
) -> tuple[str, object, object]:
    cold = _cold_copy(warm.path, directory)
    cold_result = _outcome(lambda: cold.append(record))
    warm_result = _outcome(lambda: warm.append(record))

    assert warm_result == cold_result
    assert warm.path.read_bytes() == cold.path.read_bytes()
    return warm_result


def test_warm_store_sees_a_record_appended_by_another_store(tmp_path: Path) -> None:
    warm = _warm_store(tmp_path)
    JsonlLedgerStore(warm.path).append(_claim(10))

    result = _assert_same_as_a_fresh_store(warm, _claim(11), tmp_path)

    assert result[0] == "ok"
    assert warm.load().snapshot.record_ids == (
        "claim-0",
        "claim-1",
        "claim-2",
        "claim-10",
        "claim-11",
    )


def test_warm_store_rereads_a_same_size_edit_that_keeps_the_identity(
    tmp_path: Path,
) -> None:
    warm = _warm_store(tmp_path)
    _edit_in_place(warm.path, b'"id":"claim-0"', b'"id":"claim-9"')

    result = _assert_same_as_a_fresh_store(warm, _claim(11), tmp_path)

    assert result[0] == "ok"
    assert warm.load().snapshot.record_ids == (
        "claim-9",
        "claim-1",
        "claim-2",
        "claim-11",
    )


def test_warm_store_rechecks_a_stored_record_edited_to_be_schema_invalid(
    tmp_path: Path,
) -> None:
    warm = _warm_store(tmp_path)
    _edit_in_place(warm.path, b'"status":"proposed"', b'"status":"proposeX"')
    before = warm.path.read_bytes()

    result = _assert_same_as_a_fresh_store(warm, _claim(11), tmp_path)

    assert result[0] == "integrity"
    assert warm.path.read_bytes() == before
    assert _outcome(warm.load)[0] == "integrity"


def test_append_rewrites_a_valid_but_non_canonical_ledger_canonically(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.jsonl"
    lines = [
        json.dumps(_claim(index), indent=None, sort_keys=False) for index in range(3)
    ]
    spaced = [line.replace('": ', '" : ') for line in lines]
    path.write_bytes(("\n".join(spaced) + "\n").encode())
    warm = JsonlLedgerStore(path)
    warm.load()

    _assert_same_as_a_fresh_store(warm, _claim(3), tmp_path)

    canonical = JsonlLedgerStore(tmp_path / "expected.jsonl")
    for index in range(4):
        canonical.append(_claim(index))
    assert path.read_bytes() == canonical.path.read_bytes()
    # The rewrite is now bytes the instance wrote, and the next append reuses them.
    _assert_same_as_a_fresh_store(warm, _claim(4), tmp_path)


def test_callers_cannot_change_what_a_store_holds(tmp_path: Path) -> None:
    warm = JsonlLedgerStore(tmp_path / "warm" / "ledger.jsonl")
    appended = _claim(0)
    warm.append(appended)
    appended["id"] = "claim-mutated"
    loaded = warm.load()
    for record in loaded.records:
        record["id"] = "claim-mutated"
    for record in warm.load().records:
        record["proposition"] = "mutated"
    for record in loaded:
        record["proposition"] = "mutated"

    result = _assert_same_as_a_fresh_store(warm, _claim(1), tmp_path)

    assert result[0] == "ok"
    assert warm.load().records == (_claim(0), _claim(1))


def test_a_record_added_after_a_failed_append_is_not_held(tmp_path: Path) -> None:
    warm = _warm_store(tmp_path, 1)
    invalid = _claim(1)
    del invalid["proposition"]
    with pytest.raises(BundleIntegrityError):
        warm.append(invalid)

    _assert_same_as_a_fresh_store(warm, _claim(1), tmp_path)

    assert warm.load().records == (_claim(0), _claim(1))


@pytest.mark.parametrize(
    ("max_records", "max_bytes"),
    [(2, None), (3, None), (None, 10)],
    ids=["records-over", "records-at-limit", "bytes-over"],
)
def test_load_limits_are_the_same_for_a_warm_and_a_fresh_store(
    tmp_path: Path,
    max_records: int | None,
    max_bytes: int | None,
) -> None:
    warm = _warm_store(tmp_path)
    cold = _cold_copy(warm.path, tmp_path)

    assert _outcome(
        lambda: warm.load(max_records=max_records, max_bytes=max_bytes)
    ) == _outcome(lambda: cold.load(max_records=max_records, max_bytes=max_bytes))
    # The failed read did not poison the held records.
    assert _outcome(warm.load) == _outcome(cold.load)


def _what_load_raises_for_the_result(
    store: JsonlLedgerStore,
    batch: list[JsonObject],
    directory: Path,
    *,
    max_records: int | None = None,
    max_bytes: int | None = None,
) -> tuple[str, object, object]:
    """Return what ``load`` raises reading the ledger the batch would produce."""

    expected = _cold_copy(store.path, directory)
    expected.extend(batch)
    return _outcome(lambda: expected.load(max_records=max_records, max_bytes=max_bytes))


@pytest.mark.parametrize("max_records", [3, 0], ids=["over-by-one", "over-by-all"])
def test_extend_refuses_a_result_over_the_record_limit(
    tmp_path: Path,
    max_records: int,
) -> None:
    store = _warm_store(tmp_path)
    before = store.path.read_bytes()
    batch = [_claim(3)]
    expected = _what_load_raises_for_the_result(
        store, batch, tmp_path, max_records=max_records
    )

    assert expected == (
        "LedgerFormatError",
        f"<ledger>:{max_records + 1}: ledger record count exceeds limit {max_records}",
        None,
    )
    assert _outcome(lambda: store.append(batch[0], max_records=max_records)) == expected
    assert _outcome(lambda: store.extend(batch, max_records=max_records)) == expected
    assert store.path.read_bytes() == before
    assert [path.name for path in store.path.parent.iterdir()] == ["ledger.jsonl"]


def test_extend_allows_a_result_exactly_at_the_record_limit(tmp_path: Path) -> None:
    store = _warm_store(tmp_path)

    store.append(_claim(3), max_records=4)
    store.extend([_claim(4)], max_records=5)

    assert store.load(max_records=5).snapshot.record_count == 5


def test_extend_applies_the_byte_limit_to_the_result_at_its_boundary(
    tmp_path: Path,
) -> None:
    store = _warm_store(tmp_path)
    before = store.path.read_bytes()
    resulting_size = len(before) + len(rfc8785.dumps(_claim(3))) + 1
    expected = _what_load_raises_for_the_result(
        store, [_claim(3)], tmp_path, max_bytes=resulting_size - 1
    )

    assert expected[0] == "LedgerFormatError"
    assert (
        _outcome(lambda: store.append(_claim(3), max_bytes=resulting_size - 1))
        == expected
    )
    assert store.path.read_bytes() == before

    store.append(_claim(3), max_bytes=resulting_size)

    assert len(store.path.read_bytes()) == resulting_size
    assert store.load(max_bytes=resulting_size).snapshot.record_count == 4


def test_extend_reports_the_byte_limit_before_the_record_limit(tmp_path: Path) -> None:
    store = _warm_store(tmp_path)
    expected = _what_load_raises_for_the_result(
        store, [_claim(3)], tmp_path, max_bytes=100, max_records=1
    )

    assert expected == (
        "LedgerFormatError",
        "<ledger>:1: ledger size exceeds limit 100 bytes",
        None,
    )
    assert expected == _outcome(
        lambda: store.append(_claim(3), max_bytes=100, max_records=1)
    )


@pytest.mark.parametrize(
    ("max_records", "max_bytes"),
    [(2, None), (None, 10)],
    ids=["records", "bytes"],
)
def test_extend_applies_limits_to_the_stored_ledger_as_load_does(
    tmp_path: Path,
    max_records: int | None,
    max_bytes: int | None,
) -> None:
    store = _warm_store(tmp_path)
    cold = _cold_copy(store.path, tmp_path)
    before = store.path.read_bytes()
    expected = _outcome(lambda: cold.load(max_records=max_records, max_bytes=max_bytes))

    assert expected[0] == "LedgerFormatError"
    assert (
        _outcome(
            lambda: store.append(
                _claim(3), max_records=max_records, max_bytes=max_bytes
            )
        )
        == expected
    )
    assert (
        _outcome(
            lambda: store.extend(
                [_claim(3)], max_records=max_records, max_bytes=max_bytes
            )
        )
        == expected
    )
    assert store.path.read_bytes() == before


def test_extend_with_limits_at_the_stored_boundary_succeeds(tmp_path: Path) -> None:
    store = _warm_store(tmp_path, 2)
    stored_size = len(store.path.read_bytes())

    assert store.extend([], max_records=2, max_bytes=stored_size).record_count == 2
    store.extend([_claim(2)], max_records=3)

    assert store.load().snapshot.record_count == 3


def test_extend_rejects_negative_limits(tmp_path: Path) -> None:
    store = JsonlLedgerStore(tmp_path / "ledger.jsonl")

    with pytest.raises(ValueError, match="max_records must not be negative"):
        store.append(_claim(0), max_records=-1)
    with pytest.raises(ValueError, match="max_bytes must not be negative"):
        store.extend([_claim(0)], max_bytes=-1)
    assert not store.path.exists()


def test_an_empty_extend_applies_the_record_limit_to_the_stored_ledger(
    tmp_path: Path,
) -> None:
    store = _warm_store(tmp_path, 2)

    with pytest.raises(LedgerFormatError, match="record count exceeds limit 1"):
        store.extend((), max_records=1)


def test_extend_checks_the_stored_size_before_decoding_it(tmp_path: Path) -> None:
    store = _warm_store(tmp_path, 2)
    stored_size = len(store.path.read_bytes())
    with store.path.open("ab") as handle:
        handle.write(b"{not json\n")

    with pytest.raises(
        LedgerFormatError, match=f"ledger size exceeds limit {stored_size} bytes"
    ):
        store.extend([_claim(2)], max_bytes=stored_size)


class _StampingValidator(BundleValidator):
    """Validate, then restamp every record it is given and remember each one.

    Overriding ``validate`` lets a validator see and change the records, so a
    store must not reuse records such a validator was handed.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[JsonObject, ...]] = []

    def validate(
        self,
        records: Iterable[JsonObject],
        *,
        external_refs: Collection[str] = (),
    ) -> BundleSnapshot:
        candidate = tuple(records)
        snapshot = super().validate(candidate, external_refs=external_refs)
        self.calls.append(candidate)
        for record in candidate:
            record["created_at"] = f"2026-07-22T12:00:{len(self.calls):02d}Z"
        return snapshot


def test_a_store_holds_no_history_for_a_validator_that_overrides_validate(
    tmp_path: Path,
) -> None:
    warm_validator = _StampingValidator()
    warm = JsonlLedgerStore(tmp_path / "warm.jsonl", validator=warm_validator)
    fresh_validator = _StampingValidator()
    fresh_path = tmp_path / "fresh.jsonl"

    for index in range(3):
        warm.append(_claim(index))
        JsonlLedgerStore(fresh_path, validator=fresh_validator).append(_claim(index))

    # Each append validates the stored history, then the extended one.  The
    # history handed over is decoded afresh each time, never an object the
    # validator was given by an earlier append, so the history is restamped
    # and rewritten exactly as a store that held nothing would.
    assert len(warm_validator.calls) == 6
    for position in range(2, len(warm_validator.calls), 2):
        history = warm_validator.calls[position]
        earlier = {
            id(record) for call in warm_validator.calls[:position] for record in call
        }
        assert len(history) == position // 2
        assert not {id(record) for record in history} & earlier
    assert warm.path.read_bytes() == fresh_path.read_bytes()
