# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Append-only in-memory and JSON Lines storage for protocol records."""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
from collections.abc import Collection, Iterable, Iterator, Sequence
from copy import deepcopy
from pathlib import Path
from typing import BinaryIO

import rfc8785

from ._json import strict_json_loads
from .bundle import BundleSnapshot, BundleValidator
from .types import JsonObject, StrPath


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

        candidate = self._records + additions
        # Held records are private copies this validator already accepted, so
        # only the additions need the per-record schema check.
        snapshot = self._validator.validate(
            candidate,
            external_refs=self._external_refs,
            schema_checked_prefix=len(self._records),
        )
        self._records = candidate
        self._snapshot = snapshot
        return snapshot


class JsonlLedgerStore:
    """Persist a logical append-only ledger as deterministic JSON Lines."""

    def __init__(self, path: StrPath, validator: BundleValidator | None = None) -> None:
        self.path: Path = Path(path)
        self._validator = validator or BundleValidator()

    def load(
        self,
        *,
        external_refs: Collection[str] = (),
        max_records: int | None = None,
        max_bytes: int | None = None,
    ) -> Ledger:
        """Load and validate the complete ledger, or return an empty ledger."""

        if max_records is not None and max_records < 0:
            raise ValueError("max_records must not be negative")
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        records = self._read_records(
            max_records=max_records,
            max_bytes=max_bytes,
        )
        return Ledger(
            records,
            external_refs=external_refs,
            validator=self._validator,
        )

    def append(
        self,
        record: JsonObject,
        *,
        external_refs: Collection[str] = (),
    ) -> BundleSnapshot:
        """Validate and atomically persist one logical append."""

        return self.extend((record,), external_refs=external_refs)

    def extend(
        self,
        records: Iterable[JsonObject],
        *,
        external_refs: Collection[str] = (),
    ) -> BundleSnapshot:
        """Validate and atomically persist a batch of logical appends.

        As with ``Ledger.extend``, records that reference each other must
        arrive in the same batch.
        """

        additions = tuple(records)
        ledger = self.load(external_refs=external_refs)
        if not additions:
            return ledger.snapshot

        snapshot = ledger.extend(additions)
        self._atomic_replace(ledger.records)
        return snapshot

    def _read_records(
        self,
        *,
        max_records: int | None = None,
        max_bytes: int | None = None,
    ) -> list[JsonObject]:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return []
        if stat.S_ISLNK(metadata.st_mode):
            raise OSError(f"ledger path must not be a symbolic link: {self.path}")
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"ledger path must be a regular file: {self.path}")

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags)
        try:
            opened_metadata = os.fstat(descriptor)
            if not stat.S_ISREG(opened_metadata.st_mode) or (
                opened_metadata.st_dev,
                opened_metadata.st_ino,
            ) != (metadata.st_dev, metadata.st_ino):
                raise OSError(f"ledger path changed while opening: {self.path}")
            if max_bytes is not None and opened_metadata.st_size > max_bytes:
                raise LedgerFormatError(
                    self.path,
                    1,
                    f"ledger size exceeds limit {max_bytes} bytes",
                )
        except BaseException:
            os.close(descriptor)
            raise

        with os.fdopen(descriptor, "rb") as handle:
            records, observed_bytes = _decode_record_stream(
                handle,
                path=self.path,
                max_records=max_records,
                max_bytes=max_bytes,
            )
            if (
                observed_bytes != opened_metadata.st_size
                or os.fstat(handle.fileno()).st_size != opened_metadata.st_size
            ):
                raise OSError(f"ledger path changed while reading: {self.path}")
        return records

    def _atomic_replace(self, records: Sequence[JsonObject]) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            original_metadata = self.path.lstat()
        except FileNotFoundError:
            original_metadata = None
        if original_metadata is not None:
            if stat.S_ISLNK(original_metadata.st_mode):
                raise OSError(f"ledger path must not be a symbolic link: {self.path}")
            if not stat.S_ISREG(original_metadata.st_mode):
                raise OSError(f"ledger path must be a regular file: {self.path}")

        descriptor, temporary_name = tempfile.mkstemp(
            dir=parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                for record in records:
                    handle.write(rfc8785.dumps(record))
                    handle.write(b"\n")
                handle.flush()
                os.fsync(handle.fileno())

            try:
                current_metadata = self.path.lstat()
            except FileNotFoundError:
                current_metadata = None
            if original_metadata is None:
                if current_metadata is not None:
                    raise OSError(f"ledger path appeared while writing: {self.path}")
            elif current_metadata is None or (
                current_metadata.st_dev,
                current_metadata.st_ino,
            ) != (
                original_metadata.st_dev,
                original_metadata.st_ino,
            ):
                raise OSError(f"ledger path changed while writing: {self.path}")
            else:
                temporary_path.chmod(stat.S_IMODE(original_metadata.st_mode))
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)
