# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Content-addressed capture of untrusted provider response bytes."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from ._filesystem import (
    open_observed_regular_file,
    publish_path_no_replace,
    read_and_close,
    sync_directory_after_rename,
)

# JsonValue is unused here, but typing.get_type_hints needs it to resolve the
# recursive JsonObject alias in this module's annotations.
from .types import (
    JsonObject,
    JsonValue,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """Immutable reference to one captured provider response."""

    uri: str
    media_type: str
    sha256: str
    captured_at: str

    def to_json_object(self) -> JsonObject:
        """Return the protocol-friendly artifact representation."""

        return {
            "uri": self.uri,
            "media_type": self.media_type,
            "digest": {
                "algorithm": "sha256",
                "value": self.sha256,
            },
            "captured_at": self.captured_at,
        }


class ArtifactSink(Protocol):
    """Capture an untrusted provider response under an operator policy."""

    def capture(
        self,
        content: bytes,
        /,
        *,
        media_type: str,
        captured_at: datetime,
    ) -> ArtifactReference:
        """Persist bytes and return their immutable reference and digest."""

        ...


def _require_private_artifact_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OSError("artifact root must be a directory, not a symbolic link")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & (
        stat.S_IRWXG | stat.S_IRWXO
    ):
        raise PermissionError("artifact root must not grant group or other permissions")


def _validate_existing_artifact(path: Path, content: bytes) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OSError("artifact path must be a regular file, not a symlink")
    # Unlike the other guarded reads, a captured artifact must also be private.
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & (
        stat.S_IRWXG | stat.S_IRWXO
    ):
        raise PermissionError("artifact file must not grant group or other permissions")
    descriptor, _ = open_observed_regular_file(
        path,
        metadata,
        changed=lambda: OSError("artifact path changed while opening"),
    )
    observed, final_size = read_and_close(descriptor, limit=len(content))
    if (
        metadata.st_size != len(content)
        or final_size != metadata.st_size
        or observed != content
    ):
        raise OSError("content-addressed artifact digest collision")


@dataclass(frozen=True, slots=True)
class DirectoryArtifactSink:
    """Content-addressed local response sink with private file permissions."""

    root: Path
    uri_prefix: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        if self.uri_prefix is None:
            return
        prefix = self.uri_prefix
        if (
            not prefix
            or prefix != prefix.strip()
            or prefix.startswith("/")
            or prefix.endswith("/")
            or "\\" in prefix
            or any(part in {"", ".", ".."} for part in prefix.split("/"))
        ):
            raise ValueError("uri_prefix must be a normalized relative URI path")
        parsed_prefix = urlsplit(prefix)
        if parsed_prefix.scheme or parsed_prefix.netloc or parsed_prefix.query:
            raise ValueError("uri_prefix must be a normalized relative URI path")
        if parsed_prefix.fragment:
            raise ValueError("uri_prefix must not contain a fragment")

    def capture(
        self,
        content: bytes,
        /,
        *,
        media_type: str,
        captured_at: datetime,
    ) -> ArtifactReference:
        """Atomically capture exact response bytes under their SHA-256 digest.

        A new artifact's directory is synchronized after it is published where
        the filesystem supports it; an OSError from that sync, such as EIO,
        propagates with the artifact already published.
        """

        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        normalized_media_type = media_type.strip()
        if not normalized_media_type:
            raise ValueError("media_type must be non-empty")

        digest = hashlib.sha256(content).hexdigest()
        suffix = ".json" if normalized_media_type == "application/json" else ".bin"
        _require_private_artifact_directory(self.root)
        path = self.root / f"sha256-{digest}{suffix}"

        try:
            metadata = path.lstat()
        except FileNotFoundError:
            metadata = None

        if metadata is not None:
            _validate_existing_artifact(path, content)
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.root,
                prefix=".artifact-",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    # Windows has no fchmod before Python 3.13 and no POSIX modes.
                    if os.name == "posix":
                        os.fchmod(handle.fileno(), stat.S_IRUSR | stat.S_IWUSR)
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    publish_path_no_replace(temporary_path, path)
                except FileExistsError:
                    _validate_existing_artifact(path, content)
                else:
                    sync_directory_after_rename(self.root)
            finally:
                temporary_path.unlink(missing_ok=True)

        timestamp = captured_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        uri = (
            path.resolve().as_uri()
            if self.uri_prefix is None
            else f"{self.uri_prefix}/{path.name}"
        )
        return ArtifactReference(
            uri=uri,
            media_type=normalized_media_type,
            sha256=digest,
            captured_at=timestamp,
        )


# Callers import these from itself.inference, which they keep as their module
# so that pickles, tracebacks, and reprs name the same place as before.
ArtifactReference.__module__ = "itself.inference"
ArtifactSink.__module__ = "itself.inference"
DirectoryArtifactSink.__module__ = "itself.inference"
