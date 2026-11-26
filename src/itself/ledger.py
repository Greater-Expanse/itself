# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Append-only in-memory and JSON Lines storage for protocol records."""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import os
from collections.abc import Callable, Collection, Iterable, Iterator
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import rfc8785

from ._filesystem import (
    FileIdentity,
    open_observed_regular_file,
    replace_file_atomically,
    require_regular_file,
)
from ._json import strict_json_loads
from .bundle import BundleSnapshot, BundleValidator

# JsonValue is unused here, but typing.get_type_hints needs it to resolve the
# recursive JsonObject alias in this module's annotations.
from .types import (
    JsonObject,
    JsonValue,  # noqa: F401  # pyright: ignore[reportUnusedImport]
    StrPath,
)


def _clone(record: JsonObject) -> JsonObject:
    return deepcopy(record)


class LedgerFormatError(ValueError):
    """Raised when a JSON Lines ledger cannot be decoded into object records."""

    def __init__(self, path: Path, line_number: int, detail: str) -> None:
        self.path: Path = path
        self.line_number: int = line_number
        self.detail: str = detail
        super().__init__(f"{path}:{line_number}: {detail}")


def _decode_record_stream(
    handle: BinaryIO,
    *,
    path: Path,
    max_records: int | None,
    max_bytes: int | None,
) -> tuple[list[JsonObject], int]:
    """Decode JSON Lines records, returning them with the number of bytes read."""

    records: list[JsonObject] = []
    observed_bytes = 0
    line_number = 0
    while True:
        read_size = -1 if max_bytes is None else max_bytes - observed_bytes + 1
        raw_line = handle.readline(read_size)
        if not raw_line:
            break
        line_number += 1
        observed_bytes += len(raw_line)
        if max_bytes is not None and observed_bytes > max_bytes:
            raise LedgerFormatError(
                path,
                line_number,
                f"ledger size exceeds limit {max_bytes} bytes",
            )
        if max_records is not None and line_number > max_records:
            raise LedgerFormatError(
                path,
                line_number,
                f"ledger record count exceeds limit {max_records}",
            )
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as error:
            raise LedgerFormatError(
                path,
                line_number,
                f"ledger line is not valid UTF-8: {error}",
            ) from error
        if not line.strip():
            raise LedgerFormatError(
                path,
                line_number,
                "blank lines are not valid ledger records",
            )
        try:
            value = strict_json_loads(line)
        except (json.JSONDecodeError, ValueError) as error:
            raise LedgerFormatError(
                path,
                line_number,
                f"invalid JSON: {error}",
            ) from error
        if not isinstance(value, dict):
            raise LedgerFormatError(
                path,
                line_number,
                "each line must contain one JSON object",
            )
        records.append(value)
    return records, observed_bytes


def decode_jsonl_records(
    content: bytes,
    *,
    path: StrPath,
    max_records: int | None = None,
    max_bytes: int | None = None,
) -> list[JsonObject]:
    """Decode complete ledger bytes with the file loader's limits and messages.

    ``path`` names the source in diagnostics only; nothing is read from it.
    """

    if max_records is not None and max_records < 0:
        raise ValueError("max_records must not be negative")
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes must not be negative")
    source = Path(path)
    if max_bytes is not None and len(content) > max_bytes:
        raise LedgerFormatError(
            source,
            1,
            f"ledger size exceeds limit {max_bytes} bytes",
        )
    records, _ = _decode_record_stream(
        io.BytesIO(content),
        path=source,
        max_records=max_records,
        max_bytes=max_bytes,
    )
    return records


class Ledger:
    """An ordered, append-only collection with atomic in-memory mutation."""

    def __init__(
        self,
        records: Iterable[JsonObject] = (),
        *,
        external_refs: Collection[str] = (),
        validator: BundleValidator | None = None,
    ) -> None:
        self._validator = validator or BundleValidator()
        self._external_refs = frozenset(external_refs)
        candidate = tuple(_clone(record) for record in records)
        self._snapshot = self._validator.validate(
            candidate,
            external_refs=self._external_refs,
        )
        self._records = candidate

    @classmethod
    def _adopt(
        cls,
        records: tuple[JsonObject, ...],
        *,
        external_refs: Collection[str],
        validator: BundleValidator,
        schema_checked: int,
    ) -> Ledger:
        """Build a ledger over records that no other caller can reach, uncloned.

        The store passes freshly decoded records, or its private held copies
        whose first ``schema_checked`` records this validator already accepted.
        The ledger never mutates them and only hands out clones, so sharing
        them with the store's held copy is safe.  A zero prefix validates
        exactly as the constructor does.
        """

        ledger = object.__new__(cls)
        ledger._validator = validator
        ledger._external_refs = frozenset(external_refs)
        if schema_checked == 0:
            ledger._snapshot = validator.validate(
                records,
                external_refs=ledger._external_refs,
            )
        else:
            ledger._snapshot = ledger._validated(
                records,
                schema_checked=schema_checked,
            )
        ledger._records = records
        return ledger

    @property
    def records(self) -> tuple[JsonObject, ...]:
        """Return defensive copies of records in authoritative replay order."""

        return tuple(_clone(record) for record in self._records)

    @property
    def snapshot(self) -> BundleSnapshot:
        """Return the current deterministic replay snapshot."""

        return self._snapshot

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[JsonObject]:
        return iter(self.records)

    def append(self, record: JsonObject) -> BundleSnapshot:
        """Validate and append one record without partial mutation on failure."""

        return self.extend((record,))

    def extend(self, records: Iterable[JsonObject]) -> BundleSnapshot:
        """Validate and append a batch as one all-or-nothing operation.

        References resolve against the complete candidate history, so records
        that reference each other, such as a completed test and its evidence,
        must arrive in the same batch.
        """

        additions = tuple(_clone(record) for record in records)
        if not additions:
            return self._snapshot

        # The history is read once, so the prefix that skips the schema check
        # is exactly the history the candidate was built from.
        existing = self._records
        candidate = existing + additions
        snapshot = self._validated(candidate, schema_checked=len(existing))
        self._records = candidate
        self._snapshot = snapshot
        return snapshot

    def _validated(
        self,
        candidate: tuple[JsonObject, ...],
        *,
        schema_checked: int,
    ) -> BundleSnapshot:
        validator = self._validator
        # Held records are private copies this validator already accepted, so
        # only the additions need the per-record schema check.  That shortcut
        # is internal to the package, and a validator that overrides validate
        # receives the whole candidate through its own method instead.
        if type(validator).validate is BundleValidator.validate:
            return validator._validated(  # pyright: ignore[reportPrivateUsage]
                candidate,
                self._external_refs,
                schema_checked,
            )
        return validator.validate(candidate, external_refs=self._external_refs)


@dataclass(frozen=True, slots=True)
class _Held:
    """The records a store last validated or wrote, and the bytes they match."""

    digest: bytes
    records: tuple[JsonObject, ...]
    # True only for bytes this store wrote, which are the canonical serialization
    # of ``records``.  A file it merely loaded may differ in whitespace or order.
    canonical: bool


@dataclass(frozen=True, slots=True)
class _Stored:
    """One read of the ledger file."""

    records: tuple[JsonObject, ...]
    identity: FileIdentity | None
    content: bytes
    digest: bytes
    # The held copy when the bytes read are exactly the bytes it matches.
    held: _Held | None


class JsonlLedgerStore:
    """Persist a logical append-only ledger as deterministic JSON Lines.

    An instance holds private copies of the records it last validated or wrote,
    with the SHA-256 of the bytes they match.  When a later read returns exactly
    those bytes, it neither decodes nor schema-checks the stored history again;
    any other bytes take the full strict decode and schema validation.  The
    cross-record integrity analysis always covers the whole history.  The held
    copies are never handed out, so no caller can mutate them.
    """

    def __init__(self, path: StrPath, validator: BundleValidator | None = None) -> None:
        self.path: Path = Path(path)
        self._validator = validator or BundleValidator()
        self._held: _Held | None = None

    def load(
        self,
        *,
        external_refs: Collection[str] = (),
        max_records: int | None = None,
        max_bytes: int | None = None,
    ) -> Ledger:
        """Load and validate the complete ledger, or return an empty ledger."""

        _require_limits(max_records, max_bytes)
        return self._existing(
            self._read(max_records=max_records, max_bytes=max_bytes),
            external_refs,
        )

    def append(
        self,
        record: JsonObject,
        *,
        external_refs: Collection[str] = (),
        max_records: int | None = None,
        max_bytes: int | None = None,
    ) -> BundleSnapshot:
        """Validate and atomically persist one logical append, as ``extend`` does."""

        return self.extend(
            (record,),
            external_refs=external_refs,
            max_records=max_records,
            max_bytes=max_bytes,
        )

    def extend(
        self,
        records: Iterable[JsonObject],
        *,
        external_refs: Collection[str] = (),
        max_records: int | None = None,
        max_bytes: int | None = None,
    ) -> BundleSnapshot:
        """Validate and atomically persist a batch of logical appends.

        As with ``Ledger.extend``, records that reference each other must
        arrive in the same batch.  The store is single-writer: if the file is
        created, replaced, or modified after this call reads it, the append
        raises OSError and leaves the other writer's file in place.  The
        directory is synchronized after the replacement where the filesystem
        supports it; an OSError from that sync, such as EIO, propagates with
        the batch already stored.

        ``max_records`` and ``max_bytes`` mean what they mean for ``load``.
        They apply to the stored ledger as read, with the error ``load`` would
        raise.  They also apply to the result: a batch that would leave more
        records or bytes than allowed raises the error ``load`` would raise
        reading that file, before anything is written.
        """

        _require_limits(max_records, max_bytes)
        additions = tuple(records)
        stored = self._read(max_records=max_records, max_bytes=max_bytes)
        ledger = self._existing(stored, external_refs)
        if not additions:
            return ledger.snapshot

        existing_count = len(stored.records)
        snapshot = ledger.extend(additions)
        resulting = ledger._records  # pyright: ignore[reportPrivateUsage]

        # Bytes this store wrote are the canonical form of the records they
        # hold, so they stand as the output prefix.  Any other file may differ
        # in whitespace or key order, and every record is serialized again.
        held = stored.held
        chunks: Iterable[bytes]
        if held is not None and held.canonical:
            chunks = itertools.chain(
                (stored.content,),
                (
                    rfc8785.dumps(record) + b"\n"
                    for record in resulting[existing_count:]
                ),
            )
        else:
            chunks = (rfc8785.dumps(record) + b"\n" for record in resulting)
        if max_bytes is not None:
            chunks = list(chunks)
            if sum(len(chunk) for chunk in chunks) > max_bytes:
                raise LedgerFormatError(
                    self.path,
                    1,
                    f"ledger size exceeds limit {max_bytes} bytes",
                )
        if max_records is not None and len(resulting) > max_records:
            raise LedgerFormatError(
                self.path,
                max_records + 1,
                f"ledger record count exceeds limit {max_records}",
            )

        digest = hashlib.sha256()
        self._atomic_replace(_digested(chunks, digest.update), expected=stored.identity)
        if self._holds_records():
            self._held = _Held(digest.digest(), resulting, canonical=True)
        return snapshot

    def _holds_records(self) -> bool:
        # A validator that overrides ``validate`` may inspect or mutate the
        # records it is given, so no history is held for it.
        return type(self._validator).validate is BundleValidator.validate

    def _existing(self, stored: _Stored, external_refs: Collection[str]) -> Ledger:
        """Validate the stored history, holding it once it has been accepted."""

        held = stored.held
        ledger = Ledger._adopt(  # pyright: ignore[reportPrivateUsage]
            stored.records,
            external_refs=external_refs,
            validator=self._validator,
            schema_checked=len(stored.records) if held is not None else 0,
        )
        if held is None and stored.identity is not None and self._holds_records():
            self._held = _Held(stored.digest, stored.records, canonical=False)
        return ledger

    def _read(
        self,
        *,
        max_records: int | None,
        max_bytes: int | None,
    ) -> _Stored:
        """Read the stored records, decoding only bytes this store has not held."""

        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return _Stored((), None, b"", hashlib.sha256(b"").digest(), None)
        require_regular_file(metadata, self.path, "ledger")
        descriptor, opened_metadata = open_observed_regular_file(
            self.path,
            metadata,
            changed=lambda: OSError(f"ledger path changed while opening: {self.path}"),
        )
        if max_bytes is not None and opened_metadata.st_size > max_bytes:
            os.close(descriptor)
            raise LedgerFormatError(
                self.path,
                1,
                f"ledger size exceeds limit {max_bytes} bytes",
            )

        # Reading one byte past the limit is what the line-by-line limit check
        # would have observed, so the errors are unchanged.
        with os.fdopen(descriptor, "rb") as handle:
            content = handle.read(-1 if max_bytes is None else max_bytes + 1)
            final_size = os.fstat(handle.fileno()).st_size

        def changed() -> bool:
            return (
                len(content) != opened_metadata.st_size
                or final_size != opened_metadata.st_size
            )

        identity = FileIdentity.of(opened_metadata)
        digest = hashlib.sha256(content).digest()
        held = self._held
        if held is not None and self._holds_records() and held.digest == digest:
            if max_records is not None and len(held.records) > max_records:
                raise LedgerFormatError(
                    self.path,
                    max_records + 1,
                    f"ledger record count exceeds limit {max_records}",
                )
            if changed():
                raise OSError(f"ledger path changed while reading: {self.path}")
            return _Stored(held.records, identity, content, digest, held)

        decoded, observed_bytes = _decode_record_stream(
            io.BytesIO(content),
            path=self.path,
            max_records=max_records,
            max_bytes=max_bytes,
        )
        if observed_bytes != opened_metadata.st_size or changed():
            raise OSError(f"ledger path changed while reading: {self.path}")
        return _Stored(tuple(decoded), identity, content, digest, None)

    def _atomic_replace(
        self,
        chunks: Iterable[bytes],
        *,
        expected: FileIdentity | None,
    ) -> None:
        replace_file_atomically(
            self.path,
            chunks,
            expected=expected,
            label="ledger",
        )


def _require_limits(max_records: int | None, max_bytes: int | None) -> None:
    if max_records is not None and max_records < 0:
        raise ValueError("max_records must not be negative")
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes must not be negative")


def _digested(
    chunks: Iterable[bytes],
    update: Callable[[bytes], object],
) -> Iterator[bytes]:
    for chunk in chunks:
        update(chunk)
        yield chunk
