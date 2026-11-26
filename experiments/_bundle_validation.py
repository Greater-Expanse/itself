# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Shared invariant mechanics for private experiment bundles."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final, cast

from experiments._contract_values import object_value as _object
from experiments._contract_values import string_value as _string
from experiments.trial_manifests import canonical_json_bytes
from itself import EvidenceBundleLimits, JsonObject, JsonValue, Ledger
from itself._filesystem import (
    open_observed_regular_file,
    publish_path_no_replace,
    read_and_close,
)
from itself._json import strict_json_loads
from itself.receipts import ReasoningReceiptValidationError, ReasoningReceiptValidator

BundleError = type[ValueError]
ManifestValidator = Callable[[JsonValue], None]

# Experiment bundles adopt the shipped evidence-bundle defaults: a JSON
# document is bounded like a bundle manifest, a ledger and a receipt by their
# own limits, and every other inventoried file by the per-file limit.
_LIMITS: Final = EvidenceBundleLimits()
DOCUMENT_BYTE_LIMIT: Final = _LIMITS.max_manifest_bytes
FILE_BYTE_LIMIT: Final = _LIMITS.max_file_bytes
LEDGER_BYTE_LIMIT: Final = _LIMITS.max_ledger_bytes
RECEIPT_BYTE_LIMIT: Final = _LIMITS.max_receipt_bytes
_QUARANTINE_SUFFIX: Final = ".quarantined"


@dataclass(frozen=True, slots=True)
class ValidatedInventory:
    """Validated manifest and content-addressed file inventory."""

    bundle_manifest: JsonObject
    file_entries: tuple[JsonObject, ...]
    entry_by_path: Mapping[str, JsonObject]
    paths: frozenset[str]


class AttemptQuarantinedError(RuntimeError):
    """Raised when an unpublished attempt is kept beside its destination.

    ``path`` names the directory that holds the captured provider responses.
    """

    def __init__(self, path: Path, detail: str) -> None:
        self.path: Path = path
        super().__init__(f"{detail}; the captured attempt was quarantined at {path}")


def read_regular_file(path: Path, *, max_bytes: int, error_type: BundleError) -> bytes:
    """Read one regular file, without following a link, up to ``max_bytes``.

    The path is checked with ``lstat`` before it is opened, so a FIFO, device,
    directory, or symbolic link is rejected without being read.
    """

    try:
        metadata = path.lstat()
    except OSError as error:
        raise error_type(f"{path}: cannot inspect file: {error}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise error_type(f"{path}: is not a regular file")
    descriptor, opened = open_observed_regular_file(
        path,
        metadata,
        changed=lambda: error_type(f"{path}: file changed while opening"),
        open_failed=lambda error: error_type(f"{path}: cannot open file: {error}"),
    )
    if opened.st_size > max_bytes:
        os.close(descriptor)
        raise error_type(
            f"{path}: file size {opened.st_size} exceeds limit {max_bytes}"
        )
    content, final_size = read_and_close(descriptor, limit=max_bytes)
    if len(content) > max_bytes:
        raise error_type(f"{path}: file exceeds limit {max_bytes} while reading")
    if len(content) != opened.st_size or final_size != opened.st_size:
        raise error_type(f"{path}: file changed while reading")
    return content


def load_json_object(
    path: Path,
    *,
    error_type: BundleError,
    max_bytes: int = DOCUMENT_BYTE_LIMIT,
) -> JsonObject:
    """Load one bounded, strict JSON object and translate failures."""

    content = read_regular_file(path, max_bytes=max_bytes, error_type=error_type)
    try:
        value = strict_json_loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise error_type(f"{path}: invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise error_type(f"{path}: expected one JSON object")
    return value


def normalized_bundle_path(
    value: str,
    *,
    error_type: BundleError,
) -> PurePosixPath:
    """Parse a normalized relative bundle path or reject path smuggling."""

    candidate = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise error_type(
            f"bundle file path {value!r} is not a normalized relative path"
        )
    return candidate


def sha256_bytes(value: bytes) -> str:
    """Return the lowercase SHA-256 digest of exact bytes."""

    return hashlib.sha256(value).hexdigest()


def pretty_json_bytes(value: JsonValue) -> bytes:
    """Encode deterministic, human-readable JSON bytes."""

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


def bundle_timestamp(value: datetime) -> str:
    """Encode one timezone-aware experiment bundle timestamp."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("bundle timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def require_private_mode(
    path: Path,
    *,
    error_type: BundleError,
    metadata: os.stat_result | None = None,
) -> None:
    """Reject a bundle path that grants POSIX group or other permissions.

    The mode is read with ``lstat`` unless the caller supplies that metadata,
    so a link is judged by its own mode and never by its target's.
    """

    if os.name != "posix":
        return
    if metadata is None:
        try:
            metadata = path.lstat()
        except OSError as error:
            raise error_type(f"{path}: cannot inspect bundle path: {error}") from error
    if stat.S_IMODE(metadata.st_mode) & (stat.S_IRWXG | stat.S_IRWXO):
        raise error_type(f"bundle path {path} grants group or other permissions")


def require_bundle_root(root: Path, *, error_type: BundleError) -> None:
    """Require a private directory, not a link, before reading inside it."""

    try:
        metadata = root.lstat()
    except OSError as error:
        raise error_type(f"{root}: cannot inspect bundle root: {error}") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise error_type(f"{root}: bundle root is not a directory")
    require_private_mode(root, error_type=error_type, metadata=metadata)


def file_entry(
    root: Path,
    relative_path: str,
    *,
    media_type: str,
    visibility: str,
    error_type: BundleError,
) -> JsonObject:
    """Build one inventory entry from materialized bundle bytes."""

    relative = normalized_bundle_path(relative_path, error_type=error_type)
    content = read_regular_file(
        root.joinpath(*relative.parts),
        max_bytes=FILE_BYTE_LIMIT,
        error_type=error_type,
    )
    return {
        "path": relative_path,
        "media_type": media_type,
        "visibility": visibility,
        "size_bytes": len(content),
        "digest": {
            "algorithm": "sha256",
            "value": sha256_bytes(content),
        },
    }


def bundle_identity(bundle_manifest: JsonObject) -> JsonObject:
    """Return the exact manifest projection bound by its bundle identifier."""

    return {
        key: deepcopy(value)
        for key, value in bundle_manifest.items()
        if key != "bundle_id"
    }


def _file_entries(bundle_manifest: JsonObject) -> tuple[JsonObject, ...]:
    value = bundle_manifest["files"]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError("schema-validated field 'files' was not an object array")
    return tuple(cast(JsonObject, item) for item in value)


def _declared_size(entry: JsonObject, relative: str, error_type: BundleError) -> int:
    declared = entry["size_bytes"]
    # The schema's "integer" also admits 3302.0, which canonical JSON would
    # hash differently from the integer 3302.
    if isinstance(declared, bool) or not isinstance(declared, int):
        raise error_type(
            f"bundle inventory file {relative!r} must write size_bytes as an "
            f"integer literal, not {declared!r}"
        )
    if declared > _LIMITS.max_file_bytes:
        raise error_type(
            f"bundle inventory file {relative!r} declares {declared} bytes, "
            f"exceeding limit {_LIMITS.max_file_bytes}"
        )
    return declared


def _check_declared_sizes(
    file_entries: tuple[JsonObject, ...],
    paths: tuple[str, ...],
    error_type: BundleError,
) -> None:
    if len(file_entries) > _LIMITS.max_files:
        raise error_type(
            f"bundle file count {len(file_entries)} exceeds limit {_LIMITS.max_files}"
        )
    declared_total = 0
    for entry, relative in zip(file_entries, paths, strict=True):
        declared_total += _declared_size(entry, relative, error_type)
        if declared_total > _LIMITS.max_total_bytes:
            raise error_type(
                f"bundle inventoried byte total exceeds limit {_LIMITS.max_total_bytes}"
            )


def _inventoried_file(
    root: Path,
    root_resolved: Path,
    relative: str,
    error_type: BundleError,
) -> Path:
    candidate = root.joinpath(
        *normalized_bundle_path(relative, error_type=error_type).parts
    )
    # A manifest path can make these probes fail, for example with a name
    # longer than NAME_MAX; Path.resolve raises RuntimeError on a symlink loop
    # before Python 3.13.
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as error:
        raise error_type(
            f"bundle inventory file {relative!r} cannot be inspected: {error}"
        ) from error
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        raise error_type(
            f"bundle inventory file {relative!r} is missing or not regular"
        )
    require_private_mode(candidate, error_type=error_type, metadata=metadata)
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as error:
        raise error_type(
            f"bundle inventory file {relative!r} cannot be inspected: {error}"
        ) from error
    if not resolved.is_relative_to(root_resolved):
        raise error_type(f"bundle inventory file {relative!r} escapes the bundle root")
    return candidate


def _actual_file_paths(root: Path, error_type: BundleError) -> set[str]:
    """Walk the bundle without following links and return its regular files.

    Directories are allowed, since a builder may create an attempt directory
    before its response arrives; anything else that is not a regular file is
    rejected, as is any path that grants group or other permissions.
    """

    actual_paths: set[str] = set()
    pending = [root]
    entry_count = 0
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as descendants:
                for descendant in descendants:
                    entry_count += 1
                    if entry_count > _LIMITS.max_directory_entries:
                        raise error_type(
                            "bundle directory entry count exceeds limit "
                            f"{_LIMITS.max_directory_entries}"
                        )
                    path = Path(descendant.path)
                    relative = path.relative_to(root).as_posix()
                    metadata = descendant.stat(follow_symlinks=False)
                    if stat.S_ISLNK(metadata.st_mode):
                        raise error_type(
                            f"bundle must not contain symbolic link {relative!r}"
                        )
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(path)
                    elif stat.S_ISREG(metadata.st_mode):
                        actual_paths.add(relative)
                    else:
                        raise error_type(
                            f"bundle contains non-regular entry {relative!r}"
                        )
                    require_private_mode(path, error_type=error_type, metadata=metadata)
        except OSError as error:
            raise error_type(
                f"{directory}: cannot inspect bundle directory: {error}"
            ) from error
    return actual_paths


def validate_inventory(
    root: Path,
    *,
    manifest_name: str,
    validate_manifest: ManifestValidator,
    error_type: BundleError,
) -> ValidatedInventory:
    """Validate private paths, inventory bytes, and the manifest identifier."""

    require_bundle_root(root, error_type=error_type)
    bundle_manifest = deepcopy(
        load_json_object(root / manifest_name, error_type=error_type)
    )
    validate_manifest(bundle_manifest)

    file_entries = _file_entries(bundle_manifest)
    paths = tuple(_string(entry["path"], "path") for entry in file_entries)
    if len(paths) != len(set(paths)):
        raise error_type("bundle file inventory contains duplicates")
    _check_declared_sizes(file_entries, paths, error_type)

    root_resolved = root.resolve()
    for entry, relative in zip(file_entries, paths, strict=True):
        candidate = _inventoried_file(root, root_resolved, relative, error_type)
        content = read_regular_file(
            candidate,
            max_bytes=FILE_BYTE_LIMIT,
            error_type=error_type,
        )
        digest = _object(entry["digest"], "digest")
        if _string(digest["value"], "digest.value") != sha256_bytes(content):
            raise error_type(f"bundle inventory digest mismatch for {relative!r}")
        if entry["size_bytes"] != len(content):
            raise error_type(f"bundle inventory size mismatch for {relative!r}")

    actual_paths = _actual_file_paths(root, error_type)
    expected_paths = set(paths) | {manifest_name}
    if actual_paths != expected_paths:
        missing = sorted(expected_paths.difference(actual_paths))
        extra = sorted(actual_paths.difference(expected_paths))
        raise error_type(
            f"bundle file inventory mismatch; missing={missing}, extra={extra}"
        )

    expected_bundle_id = (
        "urn:sha256:"
        + hashlib.sha256(
            canonical_json_bytes(bundle_identity(bundle_manifest))
        ).hexdigest()
    )
    if bundle_manifest["bundle_id"] != expected_bundle_id:
        raise error_type(
            "bundle_id does not bind the bundle metadata and file inventory"
        )

    entry_by_path = MappingProxyType(
        {_string(entry["path"], "path"): entry for entry in file_entries}
    )
    return ValidatedInventory(
        bundle_manifest=bundle_manifest,
        file_entries=file_entries,
        entry_by_path=entry_by_path,
        paths=frozenset(paths),
    )


def receipt_recomputes(receipt: JsonObject, ledger: Ledger) -> bool:
    """Return whether a receipt is the canonical projection of its ledger."""

    try:
        ReasoningReceiptValidator().validate_against_ledger(receipt, ledger)
    except ReasoningReceiptValidationError:
        return False
    return True


def recordable_status_code(status_code: int | None) -> int | None:
    """Return an observed HTTP status that a v1 attempt report can hold.

    v1 attempt reports bound ``status_code`` to 100-599, so a status outside
    that range is recorded as null under its ``http_status`` failure kind, and
    the private response artifact keeps the bytes that came with it.
    """

    if status_code is None or 100 <= status_code <= 599:
        return status_code
    return None


def quarantine_path(destination: Path) -> Path:
    """Return the sibling path that keeps an unpublished, captured attempt."""

    return destination.with_name(destination.name + _QUARANTINE_SUFFIX)


def require_unused_destination(destination: Path) -> None:
    """Refuse a destination, or link, that exists or whose attempt was kept.

    A quarantined attempt still used its registered slot, so a new attempt at
    the same destination is refused before any request is sent.
    """

    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    quarantine = quarantine_path(destination)
    if quarantine.exists() or quarantine.is_symlink():
        raise FileExistsError(
            f"{quarantine}: an earlier attempt at this destination was quarantined"
        )


def _holds_files(directory: Path) -> bool:
    return any(files for _, _, files in os.walk(directory))


def close_staging(
    staging: Path | None,
    destination: Path,
    exception: BaseException | None,
) -> None:
    """Remove an empty, unpublished staging directory, or quarantine it.

    Staging that holds any file may hold a captured provider response, which
    no failure may delete.  It is renamed to the quarantine path beside the
    destination, or left in place if that rename fails, and the path that
    holds it is named in an ``AttemptQuarantinedError`` raised from
    ``exception``.  A ``BaseException`` that is not an ``Exception``, such as
    ``KeyboardInterrupt``, keeps propagating after the attempt is moved.
    """

    if staging is None or not staging.exists():
        return
    if not _holds_files(staging):
        shutil.rmtree(staging)
        return
    if exception is None:
        detail = "the bundle builder exited without publishing its attempt"
    else:
        detail = str(exception) or type(exception).__name__
    kept = quarantine_path(destination)
    try:
        publish_path_no_replace(staging, kept)
    except OSError as error:
        kept = staging
        detail = f"{detail}; it could not be moved beside the destination: {error}"
    if exception is None or isinstance(exception, Exception):
        raise AttemptQuarantinedError(kept, detail) from exception
