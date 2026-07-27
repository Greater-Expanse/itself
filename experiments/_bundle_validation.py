# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Shared invariant mechanics for private experiment bundles."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import cast

from experiments._contract_values import integer_value as _integer
from experiments._contract_values import object_value as _object
from experiments._contract_values import string_value as _string
from experiments.trial_manifests import canonical_json_bytes
from itself import JsonObject, JsonValue
from itself._json import strict_json_loads

BundleError = type[ValueError]
ManifestValidator = Callable[[JsonValue], None]


@dataclass(frozen=True, slots=True)
class ValidatedInventory:
    """Validated manifest and content-addressed file inventory."""

    bundle_manifest: JsonObject
    file_entries: tuple[JsonObject, ...]
    entry_by_path: Mapping[str, JsonObject]
    paths: frozenset[str]


def load_json_object(path: Path, *, error_type: BundleError) -> JsonObject:
    """Load one strict JSON object and translate failures at the domain edge."""

    try:
        value = strict_json_loads(path.read_bytes())
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


def require_private_mode(path: Path, *, error_type: BundleError) -> None:
    """Reject a bundle path that grants POSIX group or other permissions."""

    if os.name != "posix":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise error_type(f"bundle path {path} grants group or other permissions")


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
    content = root.joinpath(*relative.parts).read_bytes()
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


def validate_inventory(
    root: Path,
    *,
    manifest_name: str,
    validate_manifest: ManifestValidator,
    error_type: BundleError,
) -> ValidatedInventory:
    """Validate private paths, inventory bytes, and the manifest identifier."""

    if not root.is_dir() or root.is_symlink():
        raise error_type(f"{root}: bundle root is not a directory")
    require_private_mode(root, error_type=error_type)
    bundle_manifest = deepcopy(
        load_json_object(root / manifest_name, error_type=error_type)
    )
    validate_manifest(bundle_manifest)

    file_entries = _file_entries(bundle_manifest)
    paths = tuple(_string(entry["path"], "path") for entry in file_entries)
    if len(paths) != len(set(paths)):
        raise error_type("bundle file inventory contains duplicates")

    root_resolved = root.resolve()
    for entry, relative in zip(file_entries, paths, strict=True):
        relative_path = normalized_bundle_path(relative, error_type=error_type)
        candidate = root.joinpath(*relative_path.parts)
        if candidate.is_symlink() or not candidate.is_file():
            raise error_type(
                f"bundle inventory file {relative!r} is missing or not regular"
            )
        require_private_mode(candidate, error_type=error_type)
        if not candidate.resolve().is_relative_to(root_resolved):
            raise error_type(
                f"bundle inventory file {relative!r} escapes the bundle root"
            )
        content = candidate.read_bytes()
        digest = _object(entry["digest"], "digest")
        if _string(digest["value"], "digest.value") != sha256_bytes(content):
            raise error_type(f"bundle inventory digest mismatch for {relative!r}")
        if _integer(entry["size_bytes"], "size_bytes") != len(content):
            raise error_type(f"bundle inventory size mismatch for {relative!r}")

    descendants = tuple(root.rglob("*"))
    if any(item.is_symlink() for item in descendants):
        raise error_type("bundle must not contain symbolic links")
    for item in descendants:
        require_private_mode(item, error_type=error_type)
    actual_paths = {
        item.relative_to(root).as_posix() for item in descendants if item.is_file()
    }
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
