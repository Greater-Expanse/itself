# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Closed, digest-inventoried evidence bundles for exchange and inspection."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Final, Protocol, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ._filesystem import publish_path_no_replace, write_file_exclusive
from ._formats import schema_format_checker
from ._json import format_json_path as _format_path
from ._json import strict_json_loads
from .ledger import Ledger, decode_jsonl_records
from .receipts import (
    ReasoningReceiptValidator,
    build_reasoning_receipt,
    canonical_ledger_bytes,
    decode_receipt_document,
)
from .types import JsonObject, JsonValue, StrPath

EVIDENCE_BUNDLE_VERSION: Final = "0.1.0-alpha.2"
BUNDLE_MANIFEST_NAME: Final = "bundle.json"
EVIDENCE_BUNDLE_LEDGER_PATH: Final = "ledger.jsonl"
EVIDENCE_BUNDLE_RECEIPT_PATH: Final = "reasoning-receipt.json"
DEFAULT_EVIDENCE_BUNDLE_LIMITATIONS: Final = (
    "Structural integrity does not establish that any real-world claim is true.",
    (
        "The bundle does not establish that an evidence source, evaluator, "
        "or authority is correct."
    ),
)
_DIGEST_CHUNK_BYTES: Final = 1024 * 1024
# Bundle paths double as artifact URIs. URI readers decode percent escapes and
# drop queries and fragments, so the builder refuses these characters; the
# validator still accepts them, which leaves the published format unchanged.
_BUILDER_REJECTED_PATH_CHARACTERS: Final = frozenset("%?#")


class _SchemaValidator(Protocol):
    """Typed surface used from the partially typed jsonschema dependency."""

    def iter_errors(self, instance: JsonValue) -> Iterator[ValidationError]: ...


class EvidenceBundleValidationError(ValueError):
    """Raised when an evidence bundle is structurally or cryptographically invalid."""


class EvidenceBundleBuildError(ValueError):
    """Raised when an evidence bundle cannot be safely materialized."""


@dataclass(frozen=True, slots=True)
class EvidenceBundleLimits:
    """Explicit resource ceilings applied while building and verifying bundles."""

    max_manifest_bytes: int = 4 * 1024 * 1024
    max_files: int = 1024
    max_directory_entries: int = 4096
    max_file_bytes: int = 256 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024
    max_ledger_bytes: int = 128 * 1024 * 1024
    max_receipt_bytes: int = 64 * 1024 * 1024
    max_ledger_records: int = 100_000

    def __post_init__(self) -> None:
        values = (
            ("max_manifest_bytes", self.max_manifest_bytes),
            ("max_files", self.max_files),
            ("max_directory_entries", self.max_directory_entries),
            ("max_file_bytes", self.max_file_bytes),
            ("max_total_bytes", self.max_total_bytes),
            ("max_ledger_bytes", self.max_ledger_bytes),
            ("max_receipt_bytes", self.max_receipt_bytes),
            ("max_ledger_records", self.max_ledger_records),
        )
        for name, value in values:
            runtime_value = cast(object, value)
            if isinstance(runtime_value, bool) or not isinstance(runtime_value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must not be negative")


DEFAULT_EVIDENCE_BUNDLE_LIMITS: Final = EvidenceBundleLimits()


class EvidenceBundleFileRole(StrEnum):
    """Caller-supplied file roles accepted by the evidence-bundle builder."""

    INPUT = "input"
    ARTIFACT = "artifact"
    SUPPLEMENTAL = "supplemental"


@dataclass(frozen=True, slots=True)
class EvidenceBundleFile:
    """One immutable caller-supplied file to materialize inside a bundle."""

    path: str
    content: bytes
    media_type: str
    role: EvidenceBundleFileRole
    record_ref: str | None = None

    def __post_init__(self) -> None:
        try:
            parse_bundle_path(self.path)
        except EvidenceBundleValidationError as error:
            raise EvidenceBundleBuildError(str(error)) from error
        if not _BUILDER_REJECTED_PATH_CHARACTERS.isdisjoint(self.path):
            raise EvidenceBundleBuildError(
                f"bundle file path {self.path!r} must not contain '%', '?', or '#'"
            )
        if self.path in {
            BUNDLE_MANIFEST_NAME,
            EVIDENCE_BUNDLE_LEDGER_PATH,
            EVIDENCE_BUNDLE_RECEIPT_PATH,
        }:
            raise EvidenceBundleBuildError(
                f"bundle file path {self.path!r} is reserved by the builder"
            )
        if not isinstance(cast(object, self.content), bytes):
            raise TypeError("bundle file content must be bytes")
        if not self.media_type.strip():
            raise EvidenceBundleBuildError("bundle file media_type must not be empty")
        if not isinstance(cast(object, self.role), EvidenceBundleFileRole):
            raise TypeError("bundle file role must be an EvidenceBundleFileRole")
        if self.role is EvidenceBundleFileRole.ARTIFACT:
            if self.record_ref is None or not self.record_ref.strip():
                raise EvidenceBundleBuildError(
                    "artifact bundle files require a non-empty record_ref"
                )
        elif self.record_ref is not None:
            raise EvidenceBundleBuildError(
                "only artifact bundle files may declare record_ref"
            )

    @classmethod
    def from_path(
        cls,
        source: StrPath,
        *,
        path: str,
        media_type: str,
        role: EvidenceBundleFileRole,
        record_ref: str | None = None,
        max_bytes: int = DEFAULT_EVIDENCE_BUNDLE_LIMITS.max_file_bytes,
    ) -> EvidenceBundleFile:
        """Read one regular, non-symlink source file into an immutable bundle file."""

        source_path = Path(source)
        runtime_max_bytes = cast(object, max_bytes)
        if isinstance(runtime_max_bytes, bool) or not isinstance(
            runtime_max_bytes, int
        ):
            raise TypeError("max_bytes must be an integer")
        if max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        try:
            content = _read_bounded_regular_file(
                source_path,
                max_bytes=max_bytes,
            )
        except EvidenceBundleValidationError as error:
            raise EvidenceBundleBuildError(str(error)) from error
        return cls(
            path=path,
            content=content,
            media_type=media_type,
            role=role,
            record_ref=record_ref,
        )


def _open_regular_file(path: Path) -> tuple[int, os.stat_result]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise EvidenceBundleValidationError(
            f"{path}: cannot inspect regular file: {error}"
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise EvidenceBundleValidationError(
            f"{path}: expected a regular non-symlink file"
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise EvidenceBundleValidationError(
            f"{path}: cannot open regular file: {error}"
        ) from error
    try:
        opened_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(opened_metadata.st_mode) or (
            opened_metadata.st_dev,
            opened_metadata.st_ino,
        ) != (metadata.st_dev, metadata.st_ino):
            raise EvidenceBundleValidationError(f"{path}: file changed while opening")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened_metadata


def _read_bounded_regular_file(path: Path, *, max_bytes: int) -> bytes:
    descriptor, metadata = _open_regular_file(path)
    if metadata.st_size > max_bytes:
        os.close(descriptor)
        raise EvidenceBundleValidationError(
            f"{path}: file size {metadata.st_size} exceeds limit {max_bytes}"
        )
    with os.fdopen(descriptor, "rb") as handle:
        content = handle.read(max_bytes + 1)
        final_size = os.fstat(handle.fileno()).st_size
    if len(content) > max_bytes:
        raise EvidenceBundleValidationError(
            f"{path}: file exceeds limit {max_bytes} while reading"
        )
    if len(content) != metadata.st_size or final_size != metadata.st_size:
        raise EvidenceBundleValidationError(f"{path}: file changed while reading")
    return content


def _digest_bounded_regular_file(
    path: Path,
    *,
    display_path: str,
    expected_size: int,
    max_bytes: int,
) -> str:
    descriptor, metadata = _open_regular_file(path)
    if metadata.st_size > max_bytes:
        os.close(descriptor)
        raise EvidenceBundleValidationError(
            f"{path}: file size {metadata.st_size} exceeds limit {max_bytes}"
        )
    if metadata.st_size != expected_size:
        os.close(descriptor)
        raise EvidenceBundleValidationError(
            f"bundle inventory size mismatch for {display_path!r}"
        )

    digest = hashlib.sha256()
    observed_size = 0
    with os.fdopen(descriptor, "rb") as handle:
        while chunk := handle.read(_DIGEST_CHUNK_BYTES):
            observed_size += len(chunk)
            if observed_size > max_bytes:
                raise EvidenceBundleValidationError(
                    f"{path}: file exceeds limit {max_bytes} while hashing"
                )
            digest.update(chunk)
        final_size = os.fstat(handle.fileno()).st_size
    if observed_size != expected_size or final_size != expected_size:
        raise EvidenceBundleValidationError(f"{path}: file changed while hashing")
    return digest.hexdigest()


def _load_json_object(path: Path, *, max_bytes: int) -> JsonObject:
    content = _read_bounded_regular_file(path, max_bytes=max_bytes)
    try:
        value = strict_json_loads(content)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise EvidenceBundleValidationError(
            f"{path}: cannot load one strict JSON object: {error}"
        ) from error
    if not isinstance(value, dict):
        raise EvidenceBundleValidationError(f"{path}: expected one JSON object")
    return value


def _object(value: JsonValue, field_name: str) -> JsonObject:
    if not isinstance(value, dict):
        raise TypeError(f"schema-valid field {field_name!r} was not an object")
    return value


def _objects(value: JsonValue, field_name: str) -> tuple[JsonObject, ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError(f"schema-valid field {field_name!r} was not an object array")
    return tuple(cast(JsonObject, item) for item in value)


def _string(value: JsonValue, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"schema-valid field {field_name!r} was not a string")
    return value


def _integer(value: JsonValue, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"schema-valid field {field_name!r} was not an integer")
    return value


def _file_byte_limit(limits: EvidenceBundleLimits, role: str) -> int:
    limit = limits.max_file_bytes
    if role == "ledger":
        limit = min(limit, limits.max_ledger_bytes)
    elif role == "reasoning_receipt":
        limit = min(limit, limits.max_receipt_bytes)
    return limit


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _pretty_json(value: JsonValue) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EvidenceBundleBuildError("bundle created_at must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _write_file(root: Path, relative: str, content: bytes) -> None:
    path = root.joinpath(*parse_bundle_path(relative).parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_file_exclusive(
        path,
        content,
        mode=stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH,
    )


def _inventory_entry(
    *,
    path: str,
    role: str,
    media_type: str,
    content: bytes,
    record_ref: str | None = None,
) -> JsonObject:
    entry: JsonObject = {
        "path": path,
        "role": role,
        "media_type": media_type,
        "size_bytes": len(content),
        "digest": {
            "algorithm": "sha256",
            "value": _sha256(content),
        },
    }
    if record_ref is not None:
        entry["record_ref"] = record_ref
    return entry


def canonical_bundle_json(value: JsonValue) -> bytes:
    """Return the deterministic JSON representation used for bundle identity."""

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def evidence_bundle_id(manifest_without_id: JsonObject) -> str:
    """Derive the bundle identifier from metadata and the complete inventory."""

    if "bundle_id" in manifest_without_id:
        raise ValueError("manifest_without_id must not contain bundle_id")
    return f"urn:sha256:{_sha256(canonical_bundle_json(manifest_without_id))}"


def parse_bundle_path(value: str) -> PurePosixPath:
    """Parse one normalized, relative evidence-bundle path."""

    candidate = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise EvidenceBundleValidationError(
            f"bundle file path {value!r} is not a normalized relative path"
        )
    return candidate


@dataclass(frozen=True, slots=True)
class VerifiedEvidenceBundle:
    """Validated bundle projections safe for downstream inspection."""

    path: Path
    manifest: JsonObject
    ledger: Ledger
    receipt: JsonObject


def _actual_file_paths(root: Path, limits: EvidenceBundleLimits) -> set[str]:
    actual_paths: set[str] = set()
    pending = [root]
    entry_count = 0
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as descendants:
                for descendant in descendants:
                    entry_count += 1
                    if entry_count > limits.max_directory_entries:
                        raise EvidenceBundleValidationError(
                            "bundle directory entry count exceeds limit "
                            f"{limits.max_directory_entries}"
                        )
                    relative = Path(descendant.path).relative_to(root).as_posix()
                    if descendant.is_symlink():
                        raise EvidenceBundleValidationError(
                            f"bundle must not contain symbolic link {relative!r}"
                        )
                    if descendant.is_dir(follow_symlinks=False):
                        pending.append(Path(descendant.path))
                    elif descendant.is_file(follow_symlinks=False):
                        actual_paths.add(relative)
                    else:
                        raise EvidenceBundleValidationError(
                            f"bundle contains non-regular entry {relative!r}"
                        )
        except EvidenceBundleValidationError:
            raise
        except OSError as error:
            raise EvidenceBundleValidationError(
                f"{directory}: cannot inspect bundle directory: {error}"
            ) from error
    return actual_paths


def _validate_inventory(
    root: Path,
    entries: tuple[JsonObject, ...],
    limits: EvidenceBundleLimits,
) -> None:
    """Check the exact file set and every inventoried size and digest."""

    if len(entries) > limits.max_files:
        raise EvidenceBundleValidationError(
            f"bundle file count {len(entries)} exceeds limit {limits.max_files}"
        )
    entry_paths = tuple(_string(entry["path"], "path") for entry in entries)
    if len(entry_paths) != len(set(entry_paths)):
        raise EvidenceBundleValidationError(
            "bundle file inventory contains duplicate paths"
        )

    declared_total = 0
    for entry in entries:
        relative = _string(entry["path"], "path")
        declared_size = entry["size_bytes"]
        # The schema's "integer" also admits 3302.0, which canonical_bundle_json
        # would hash differently from the integer 3302.
        if isinstance(declared_size, bool) or not isinstance(declared_size, int):
            raise EvidenceBundleValidationError(
                f"bundle inventory file {relative!r} must write size_bytes as an "
                f"integer literal, not {declared_size!r}"
            )
        byte_limit = _file_byte_limit(limits, _string(entry["role"], "role"))
        if declared_size > byte_limit:
            raise EvidenceBundleValidationError(
                f"bundle inventory file {relative!r} declares {declared_size} "
                f"bytes, exceeding limit {byte_limit}"
            )
        declared_total += declared_size
        if declared_total > limits.max_total_bytes:
            raise EvidenceBundleValidationError(
                f"bundle inventoried byte total exceeds limit {limits.max_total_bytes}"
            )

    root_resolved = root.resolve()
    for entry, relative in zip(entries, entry_paths, strict=True):
        relative_path = parse_bundle_path(relative)
        candidate = root.joinpath(*relative_path.parts)
        # A manifest path can make these probes fail, for example with a name
        # longer than NAME_MAX; Path.resolve raises RuntimeError on a symlink
        # loop before Python 3.13.
        try:
            regular = not candidate.is_symlink() and candidate.is_file()
            resolved = candidate.resolve() if regular else None
        except (OSError, RuntimeError) as error:
            raise EvidenceBundleValidationError(
                f"bundle inventory file {relative!r} cannot be inspected: {error}"
            ) from error
        if resolved is None:
            raise EvidenceBundleValidationError(
                f"bundle inventory file {relative!r} is missing or not regular"
            )
        if not resolved.is_relative_to(root_resolved):
            raise EvidenceBundleValidationError(
                f"bundle inventory file {relative!r} escapes the bundle root"
            )
        observed_digest = _digest_bounded_regular_file(
            candidate,
            display_path=relative,
            expected_size=_integer(entry["size_bytes"], "size_bytes"),
            max_bytes=_file_byte_limit(limits, _string(entry["role"], "role")),
        )
        digest = _object(entry["digest"], "digest")
        if _string(digest["value"], "digest.value") != observed_digest:
            raise EvidenceBundleValidationError(
                f"bundle inventory digest mismatch for {relative!r}"
            )

    actual_paths = _actual_file_paths(root, limits)
    expected_paths = set(entry_paths) | {BUNDLE_MANIFEST_NAME}
    if actual_paths != expected_paths:
        missing = sorted(expected_paths.difference(actual_paths))
        extra = sorted(actual_paths.difference(expected_paths))
        raise EvidenceBundleValidationError(
            f"bundle file inventory mismatch; missing={missing}, extra={extra}"
        )


def _read_inventoried_file(
    root: Path,
    entry: JsonObject,
    limits: EvidenceBundleLimits,
) -> tuple[Path, bytes]:
    """Read one inventoried file once and return the bytes its entry binds."""

    relative = _string(entry["path"], "path")
    path = root.joinpath(*parse_bundle_path(relative).parts)
    content = _read_bounded_regular_file(
        path,
        max_bytes=_file_byte_limit(limits, _string(entry["role"], "role")),
    )
    if len(content) != _integer(entry["size_bytes"], "size_bytes"):
        raise EvidenceBundleValidationError(
            f"bundle inventory size mismatch for {relative!r}"
        )
    digest = _object(entry["digest"], "digest")
    if _sha256(content) != _string(digest["value"], "digest.value"):
        raise EvidenceBundleValidationError(
            f"bundle inventory digest mismatch for {relative!r}"
        )
    return path, content


@dataclass(frozen=True, slots=True)
class EvidenceBundleValidator:
    """Verify inventory, bytes, protocol history, receipt, and artifacts."""

    limits: EvidenceBundleLimits = field(default_factory=EvidenceBundleLimits)

    def _schema_validator(self) -> _SchemaValidator:
        schema_resource = files("itself").joinpath(
            "schemas",
            "v0alpha2",
            "evidence-bundle.schema.json",
        )
        schema = cast(
            JsonObject,
            strict_json_loads(schema_resource.read_text(encoding="utf-8")),
        )
        Draft202012Validator.check_schema(schema)
        return cast(
            _SchemaValidator,
            Draft202012Validator(schema, format_checker=schema_format_checker()),
        )

    def errors(self, manifest: JsonValue) -> list[str]:
        """Return stable, human-readable manifest schema errors."""

        issues = sorted(
            self._schema_validator().iter_errors(manifest),
            key=lambda error: tuple(str(item) for item in error.absolute_path),
        )
        return [
            f"{_format_path(tuple(issue.absolute_path))}: {issue.message}"
            for issue in issues
        ]

    def validate_manifest(self, manifest: JsonValue) -> None:
        """Validate one evidence-bundle manifest."""

        issues = self.errors(manifest)
        if issues:
            raise EvidenceBundleValidationError("\n".join(issues))

    @staticmethod
    def _single_role(
        entries: tuple[JsonObject, ...],
        role: str,
    ) -> JsonObject:
        matches = tuple(entry for entry in entries if entry["role"] == role)
        if len(matches) != 1:
            raise EvidenceBundleValidationError(
                f"bundle must inventory exactly one {role!r} file"
            )
        return matches[0]

    @staticmethod
    def _validate_artifacts(
        ledger: Ledger,
        entries: tuple[JsonObject, ...],
    ) -> None:
        artifact_entries = {
            _string(entry["record_ref"], "record_ref"): entry
            for entry in entries
            if entry["role"] == "artifact"
        }
        if len(artifact_entries) != sum(
            entry["role"] == "artifact" for entry in entries
        ):
            raise EvidenceBundleValidationError(
                "bundle artifact inventory contains duplicate record_ref values"
            )

        artifact_records = {
            _string(record["id"], "id"): record
            for record in ledger.records
            if record["kind"] == "artifact_reference"
        }
        if set(artifact_entries) != set(artifact_records):
            raise EvidenceBundleValidationError(
                "closed bundle must materialize every ledger artifact exactly once"
            )
        for record_id, record in artifact_records.items():
            entry = artifact_entries[record_id]
            path = _string(entry["path"], "path")
            if _string(record["uri"], "uri") != path:
                raise EvidenceBundleValidationError(
                    f"artifact {record_id!r} URI does not match its inventory path"
                )
            if record["media_type"] != entry["media_type"]:
                raise EvidenceBundleValidationError(
                    f"artifact {record_id!r} media type does not match its inventory"
                )
            record_digest_value = record.get("digest")
            if not isinstance(record_digest_value, dict):
                raise EvidenceBundleValidationError(
                    f"closed-bundle artifact {record_id!r} must declare a digest"
                )
            record_digest = record_digest_value
            entry_digest = _object(entry["digest"], "digest")
            if record_digest != entry_digest:
                raise EvidenceBundleValidationError(
                    f"artifact {record_id!r} digest does not match its inventory"
                )

    def validate(self, path: StrPath) -> VerifiedEvidenceBundle:
        """Independently verify one closed evidence-bundle directory."""

        root = Path(path)
        if not root.is_dir() or root.is_symlink():
            raise EvidenceBundleValidationError(
                f"{root}: bundle root is not a directory"
            )
        manifest = _load_json_object(
            root / BUNDLE_MANIFEST_NAME,
            max_bytes=self.limits.max_manifest_bytes,
        )
        self.validate_manifest(manifest)
        entries = _objects(manifest["files"], "files")
        _validate_inventory(root, entries, self.limits)

        identity = {
            key: deepcopy(value)
            for key, value in manifest.items()
            if key != "bundle_id"
        }
        if manifest["bundle_id"] != evidence_bundle_id(identity):
            raise EvidenceBundleValidationError(
                "bundle_id does not bind the bundle metadata and file inventory"
            )

        ledger_entry = self._single_role(entries, "ledger")
        receipt_entry = self._single_role(entries, "reasoning_receipt")
        if ledger_entry["media_type"] != "application/x-ndjson":
            raise EvidenceBundleValidationError(
                "ledger file must use application/x-ndjson"
            )
        if receipt_entry["media_type"] != "application/json":
            raise EvidenceBundleValidationError(
                "reasoning receipt file must use application/json"
            )

        # Decode only bytes checked against the manifest in the same read, so a
        # file replaced after the inventory pass fails here instead of parsing.
        ledger_path, ledger_content = _read_inventoried_file(
            root,
            ledger_entry,
            self.limits,
        )
        ledger = Ledger(
            decode_jsonl_records(
                ledger_content,
                path=ledger_path,
                max_records=self.limits.max_ledger_records,
                max_bytes=_file_byte_limit(self.limits, "ledger"),
            )
        )
        receipt_path, receipt_content = _read_inventoried_file(
            root,
            receipt_entry,
            self.limits,
        )
        receipt = decode_receipt_document(receipt_content, path=receipt_path)
        ReasoningReceiptValidator().validate_against_ledger(receipt, ledger)
        self._validate_artifacts(ledger, entries)
        return VerifiedEvidenceBundle(
            path=root,
            manifest=deepcopy(manifest),
            ledger=ledger,
            receipt=deepcopy(receipt),
        )


@dataclass(frozen=True, slots=True)
class EvidenceBundleBuilder:
    """Materialize, verify, and publish one immutable closed evidence bundle."""

    validator: EvidenceBundleValidator = field(default_factory=EvidenceBundleValidator)

    @staticmethod
    def _validate_paths(
        bundle_files: Sequence[EvidenceBundleFile],
    ) -> tuple[PurePosixPath, ...]:
        paths = (
            *(PurePosixPath(bundle_file.path) for bundle_file in bundle_files),
            PurePosixPath(EVIDENCE_BUNDLE_LEDGER_PATH),
            PurePosixPath(EVIDENCE_BUNDLE_RECEIPT_PATH),
            PurePosixPath(BUNDLE_MANIFEST_NAME),
        )
        if len(paths) != len(set(paths)):
            raise EvidenceBundleBuildError("bundle file paths must be unique")
        for path in paths:
            if any(parent in paths for parent in path.parents if parent.parts):
                raise EvidenceBundleBuildError(
                    f"bundle file path {path.as_posix()!r} conflicts with a file parent"
                )
        return paths

    def _validate_resources(
        self,
        *,
        paths: tuple[PurePosixPath, ...],
        bundle_files: tuple[EvidenceBundleFile, ...],
        ledger: Ledger,
        ledger_content: bytes,
        receipt_content: bytes,
    ) -> None:
        limits = self.validator.limits
        inventoried_file_count = len(paths) - 1
        if inventoried_file_count > limits.max_files:
            raise EvidenceBundleBuildError(
                f"bundle file count {inventoried_file_count} exceeds limit "
                f"{limits.max_files}"
            )
        directories = {
            parent for path in paths for parent in path.parents if parent.parts
        }
        directory_entries = len(paths) + len(directories)
        if directory_entries > limits.max_directory_entries:
            raise EvidenceBundleBuildError(
                f"bundle directory entry count {directory_entries} exceeds limit "
                f"{limits.max_directory_entries}"
            )
        if len(ledger) > limits.max_ledger_records:
            raise EvidenceBundleBuildError(
                f"ledger record count {len(ledger)} exceeds limit "
                f"{limits.max_ledger_records}"
            )

        contents = (
            *(
                (
                    bundle_file.path,
                    bundle_file.role.value,
                    bundle_file.content,
                )
                for bundle_file in bundle_files
            ),
            (EVIDENCE_BUNDLE_LEDGER_PATH, "ledger", ledger_content),
            (
                EVIDENCE_BUNDLE_RECEIPT_PATH,
                "reasoning_receipt",
                receipt_content,
            ),
        )
        total_bytes = 0
        for relative, role, content in contents:
            byte_limit = _file_byte_limit(limits, role)
            if len(content) > byte_limit:
                raise EvidenceBundleBuildError(
                    f"bundle file {relative!r} has {len(content)} bytes, "
                    f"exceeding limit {byte_limit}"
                )
            total_bytes += len(content)
            if total_bytes > limits.max_total_bytes:
                raise EvidenceBundleBuildError(
                    f"bundle byte total exceeds limit {limits.max_total_bytes}"
                )

    def build(
        self,
        destination: StrPath,
        *,
        ledger: Ledger,
        title: str,
        files: Sequence[EvidenceBundleFile],
        created_at: datetime | None = None,
        limitations: Sequence[str] = DEFAULT_EVIDENCE_BUNDLE_LIMITATIONS,
    ) -> VerifiedEvidenceBundle:
        """Build into a private sibling directory and publish without overwriting."""

        target = Path(destination)
        if target.exists() or target.is_symlink():
            raise FileExistsError(target)

        bundle_files = tuple(files)
        if not all(
            isinstance(cast(object, item), EvidenceBundleFile) for item in bundle_files
        ):
            raise TypeError("files must contain only EvidenceBundleFile values")
        paths = self._validate_paths(bundle_files)

        ledger_content = canonical_ledger_bytes(ledger)
        receipt = build_reasoning_receipt(ledger)
        receipt_content = _pretty_json(receipt)
        self._validate_resources(
            paths=paths,
            bundle_files=bundle_files,
            ledger=ledger,
            ledger_content=ledger_content,
            receipt_content=receipt_content,
        )
        entries = [
            _inventory_entry(
                path=bundle_file.path,
                role=bundle_file.role.value,
                media_type=bundle_file.media_type,
                content=bundle_file.content,
                record_ref=bundle_file.record_ref,
            )
            for bundle_file in bundle_files
        ]
        entries.extend(
            (
                _inventory_entry(
                    path=EVIDENCE_BUNDLE_LEDGER_PATH,
                    role="ledger",
                    media_type="application/x-ndjson",
                    content=ledger_content,
                ),
                _inventory_entry(
                    path=EVIDENCE_BUNDLE_RECEIPT_PATH,
                    role="reasoning_receipt",
                    media_type="application/json",
                    content=receipt_content,
                ),
            )
        )
        manifest_files: list[JsonValue] = [entry for entry in entries]
        manifest_limitations: list[JsonValue] = [item for item in limitations]
        manifest_without_id: JsonObject = {
            "bundle_version": EVIDENCE_BUNDLE_VERSION,
            "title": title,
            "created_at": _timestamp(created_at or datetime.now(UTC)),
            "closed_artifacts": True,
            "files": manifest_files,
            "limitations": manifest_limitations,
        }
        manifest: JsonObject = {
            "bundle_id": evidence_bundle_id(manifest_without_id),
            **manifest_without_id,
        }
        self.validator.validate_manifest(manifest)
        manifest_content = _pretty_json(manifest)
        if len(manifest_content) > self.validator.limits.max_manifest_bytes:
            raise EvidenceBundleBuildError(
                f"bundle manifest has {len(manifest_content)} bytes, exceeding limit "
                f"{self.validator.limits.max_manifest_bytes}"
            )

        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                dir=parent,
                prefix=f".{target.name}.",
            )
        )
        staging.chmod(stat.S_IRWXU)
        published = False
        try:
            for bundle_file in bundle_files:
                _write_file(staging, bundle_file.path, bundle_file.content)
            _write_file(staging, EVIDENCE_BUNDLE_LEDGER_PATH, ledger_content)
            _write_file(staging, EVIDENCE_BUNDLE_RECEIPT_PATH, receipt_content)
            _write_file(staging, BUNDLE_MANIFEST_NAME, manifest_content)
            verified = self.validator.validate(staging)
            if verified.manifest["bundle_id"] != manifest["bundle_id"]:
                raise EvidenceBundleBuildError(
                    f"{staging}: staging directory changed before verification"
                )

            publish_path_no_replace(staging, target)
            published = True
            # Full validation ran once, on staging, and saw the manifest written
            # above. Its result depends only on the file set and the file bytes,
            # so re-checking the published inventory digests and manifest bytes
            # proves the target is what was validated without parsing it again.
            _validate_inventory(target, tuple(entries), self.validator.limits)
            published_manifest = _read_bounded_regular_file(
                target / BUNDLE_MANIFEST_NAME,
                max_bytes=self.validator.limits.max_manifest_bytes,
            )
            if published_manifest != manifest_content:
                raise EvidenceBundleValidationError(
                    f"{target}: published manifest differs from the verified manifest"
                )
            return replace(verified, path=target)
        finally:
            if not published and staging.exists():
                shutil.rmtree(staging)
