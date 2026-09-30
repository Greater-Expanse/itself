# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""JSON Schema validation for protocol records."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ._formats import schema_format_checker
from ._json import IJsonError, ensure_i_json, strict_json_loads
from ._json import format_json_path as _format_path
from .types import JsonObject, JsonValue, StrPath


class _SchemaValidator(Protocol):
    """Typed surface used from the partially typed jsonschema dependency."""

    def iter_errors(self, instance: JsonValue) -> Iterator[ValidationError]: ...


class ProtocolValidationError(ValueError):
    """Raised when a record does not conform to the protocol schema."""


def _required_kind(branch: JsonValue) -> str | None:
    """Return the record kind a schema branch pins, following only ``allOf``."""

    if not isinstance(branch, dict):
        return None
    properties = branch.get("properties")
    if isinstance(properties, dict):
        kind = properties.get("kind")
        if isinstance(kind, dict):
            constant = kind.get("const")
            if isinstance(constant, str):
                return constant
    parts = branch.get("allOf")
    if isinstance(parts, list):
        for part in parts:
            constant = _required_kind(part)
            if constant is not None:
                return constant
    return None


def _branches_by_kind(schema: JsonObject) -> dict[str, str]:
    """Map each record kind to its root ``oneOf`` branch reference.

    The map is empty unless every branch is a local reference that pins a
    distinct kind.  Only then is validating a record against the branch for
    its kind equivalent to validating it against the whole ``oneOf``.
    """

    branches = schema.get("oneOf")
    definitions = schema.get("$defs")
    if not isinstance(branches, list) or not isinstance(definitions, dict):
        return {}
    references: dict[str, str] = {}
    for branch in branches:
        reference = branch.get("$ref") if isinstance(branch, dict) else None
        if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
            return {}
        kind = _required_kind(definitions.get(reference.removeprefix("#/$defs/")))
        if kind is None or kind in references:
            return {}
        references[kind] = reference
    return references


@dataclass(frozen=True, slots=True)
class _CompiledSchema:
    root: _SchemaValidator
    branches: Mapping[str, _SchemaValidator]


def _compiled(schema_value: JsonValue) -> _CompiledSchema:
    if not isinstance(schema_value, dict):
        raise TypeError("protocol schema must contain one JSON object")
    schema = schema_value
    Draft202012Validator.check_schema(schema)
    format_checker = schema_format_checker()
    root_fields = {key: value for key, value in schema.items() if key != "oneOf"}
    return _CompiledSchema(
        root=cast(
            _SchemaValidator,
            Draft202012Validator(schema, format_checker=format_checker),
        ),
        branches=MappingProxyType(
            {
                kind: cast(
                    _SchemaValidator,
                    Draft202012Validator(
                        {**root_fields, "$ref": reference},
                        format_checker=format_checker,
                    ),
                )
                for kind, reference in _branches_by_kind(schema).items()
            }
        ),
    )


@cache
def _default_schema() -> _CompiledSchema:
    schema_resource = files("itself").joinpath(
        "schemas", "v0alpha2", "protocol.schema.json"
    )
    return _compiled(strict_json_loads(schema_resource.read_text(encoding="utf-8")))


class ProtocolValidator:
    """Validate provider-neutral protocol records against the canonical schema.

    A record whose ``kind`` names a schema branch is validated against that
    branch alone, which accepts exactly the records the root ``oneOf`` accepts
    and reports the branch's own errors.  The packaged schema is compiled once
    per process.
    """

    def __init__(self, schema_path: StrPath | None = None) -> None:
        self._schema = (
            _default_schema()
            if schema_path is None
            else _compiled(
                strict_json_loads(Path(schema_path).read_text(encoding="utf-8"))
            )
        )

    def errors(self, record: JsonValue) -> list[str]:
        """Return stable, human-readable validation errors."""

        try:
            ensure_i_json(record)
        except IJsonError as error:
            return [f"$: {error}"]
        validator = self._schema.root
        if isinstance(record, dict):
            kind = record.get("kind")
            if isinstance(kind, str):
                validator = self._schema.branches.get(kind, validator)
        issues = sorted(
            validator.iter_errors(record),
            key=lambda error: tuple(str(item) for item in error.absolute_path),
        )
        return [f"{_format_path(issue.path)}: {issue.message}" for issue in issues]

    def validate(self, record: JsonValue) -> None:
        """Validate a record or raise ProtocolValidationError."""

        issues = self.errors(record)
        if issues:
            raise ProtocolValidationError("\n".join(issues))

    def is_valid(self, record: JsonValue) -> bool:
        """Return whether a record conforms to the canonical schema."""

        return not self.errors(record)


def explain_jsonschema_error(error: ValidationError) -> str:
    """Format a raw jsonschema error for integrations that use the validator directly."""

    return f"{_format_path(error.path)}: {error.message}"
