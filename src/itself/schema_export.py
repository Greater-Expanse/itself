# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Deterministic exports for Itself's canonical JSON Schemas."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Final, cast

from ._filesystem import publish_path_no_replace
from .types import JsonObject, JsonValue, StrPath

SCHEMA_CATALOG_VERSION: Final = "1"
SCHEMA_LINES: Final = ("v0alpha", "v0alpha2")
SCHEMA_LINE: Final = "v0alpha2"
SCHEMA_ORIGIN: Final = "https://greaterexpanse.com"
SCHEMA_PUBLIC_ROOT: Final = f"{SCHEMA_ORIGIN}/itself/schemas/{SCHEMA_LINE}"
SCHEMA_NAMES: Final = (
    "evidence-bundle.schema.json",
    "protocol.schema.json",
    "reasoning-receipt.schema.json",
)


class SchemaExportError(ValueError):
    """Raised when canonical schemas cannot be exported safely."""


@dataclass(frozen=True, slots=True)
class PublishedSchema:
    """Content identity and public location for one canonical schema."""

    name: str
    schema_id: str
    title: str
    sha256: str
    relative_path: str

    def to_json_object(self) -> JsonObject:
        """Return the language-neutral catalog representation."""

        return {
            "name": self.name,
            "schema_id": self.schema_id,
            "title": self.title,
            "sha256": self.sha256,
            "path": self.relative_path,
        }


@dataclass(frozen=True, slots=True)
class SchemaExport:
    """One completed deterministic canonical-schema export."""

    destination: Path
    schemas: tuple[PublishedSchema, ...]


def _canonical_json(value: JsonObject) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _schema_object(name: str, content: bytes) -> JsonObject:
    try:
        value = cast(JsonValue, json.loads(content))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SchemaExportError(f"{name} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise SchemaExportError(f"{name} must contain a JSON object")
    return value


def _published_schema(line: str, name: str, content: bytes) -> PublishedSchema:
    schema = _schema_object(name, content)
    expected_id = f"{SCHEMA_ORIGIN}/itself/schemas/{line}/{name}"
    if schema.get("$id") != expected_id:
        raise SchemaExportError(f"{name} must declare canonical $id {expected_id!r}")
    title = schema.get("title")
    if not isinstance(title, str) or not title:
        raise SchemaExportError(f"{name} must declare a non-empty title")
    return PublishedSchema(
        name=name,
        schema_id=expected_id,
        title=title,
        sha256=hashlib.sha256(content).hexdigest(),
        relative_path=f"itself/schemas/{line}/{name}",
    )


def _catalog(
    line: str,
    schemas: tuple[PublishedSchema, ...],
) -> JsonObject:
    return {
        "catalog_version": SCHEMA_CATALOG_VERSION,
        "homepage": "https://greaterexpanse.com/itself",
        "name": "Itself canonical JSON Schemas",
        "schema_line": line,
        "schemas": [schema.to_json_object() for schema in schemas],
    }


def export_schemas(destination: StrPath) -> SchemaExport:
    """Export canonical packaged schemas, catalogs, and checksums.

    The destination must not already exist. It becomes available only after the
    complete export has been written.
    """

    destination_path = Path(destination)
    if destination_path.exists() or destination_path.is_symlink():
        raise SchemaExportError(
            f"schema export destination already exists: {destination_path}"
        )

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination_path.name}.staging-",
            dir=destination_path.parent,
        )
    )
    try:
        catalog_directory = staging / "itself" / "schemas"
        published: list[PublishedSchema] = []
        current_catalog: bytes | None = None
        for line in SCHEMA_LINES:
            schema_directory = catalog_directory / line
            schema_directory.mkdir(parents=True)
            schema_resources = files("itself").joinpath("schemas", line)
            line_schemas: list[PublishedSchema] = []
            for name in SCHEMA_NAMES:
                content = schema_resources.joinpath(name).read_bytes()
                schema = _published_schema(line, name, content)
                (schema_directory / name).write_bytes(content)
                line_schemas.append(schema)
                published.append(schema)

            schemas = tuple(line_schemas)
            catalog = _canonical_json(_catalog(line, schemas))
            (schema_directory / "index.json").write_bytes(catalog)
            (schema_directory / "SHA256SUMS").write_text(
                "".join(f"{schema.sha256}  {schema.name}\n" for schema in schemas),
                encoding="utf-8",
            )
            if line == SCHEMA_LINE:
                current_catalog = catalog
        if current_catalog is None:
            raise RuntimeError("current schema line was not exported")
        (catalog_directory / "index.json").write_bytes(current_catalog)
        try:
            publish_path_no_replace(staging, destination_path)
        except FileExistsError as error:
            raise SchemaExportError(
                f"schema export destination appeared while exporting: "
                f"{destination_path}"
            ) from error
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return SchemaExport(destination=destination_path, schemas=tuple(published))
