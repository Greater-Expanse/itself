# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""JSON Schema validation for protocol records."""

from __future__ import annotations

from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path
from typing import Protocol, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from ._json import IJsonError, ensure_i_json, strict_json_loads
from ._json import format_json_path as _format_path
from .types import JsonValue, StrPath


class _SchemaValidator(Protocol):
    """Typed surface used from the partially typed jsonschema dependency."""

    def iter_errors(self, instance: JsonValue) -> Iterator[ValidationError]: ...


class ProtocolValidationError(ValueError):
    """Raised when a record does not conform to the protocol schema."""


class ProtocolValidator:
    """Validate provider-neutral protocol records against the canonical schema."""

    def __init__(self, schema_path: StrPath | None = None) -> None:
        if schema_path is None:
            schema_resource = files("itself").joinpath(
                "schemas", "v0alpha2", "protocol.schema.json"
            )
            schema_value = strict_json_loads(
                schema_resource.read_text(encoding="utf-8")
            )
        else:
            schema_value = strict_json_loads(
                Path(schema_path).read_text(encoding="utf-8")
            )
        if not isinstance(schema_value, dict):
            raise TypeError("protocol schema must contain one JSON object")
        schema = schema_value

        Draft202012Validator.check_schema(schema)
        self._validator = cast(
            _SchemaValidator,
            Draft202012Validator(schema, format_checker=FormatChecker()),
        )

    def errors(self, record: JsonValue) -> list[str]:
        """Return stable, human-readable validation errors."""

        try:
            ensure_i_json(record)
        except IJsonError as error:
            return [f"$: {error}"]
        issues = sorted(
            self._validator.iter_errors(record),
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
