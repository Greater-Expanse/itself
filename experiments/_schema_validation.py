# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Shared typed diagnostics for experiment JSON Schema contracts."""

from __future__ import annotations

import json
from collections.abc import Iterator
from copy import deepcopy
from functools import cache
from pathlib import Path
from typing import Protocol, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from itself import JsonObject, JsonValue
from itself._json import format_json_path


class SchemaValidator(Protocol):
    """Typed surface used from the partially typed jsonschema dependency."""

    def iter_errors(self, instance: JsonValue) -> Iterator[ValidationError]: ...


@cache
def _checked_schema(path: Path) -> JsonObject:
    value = cast(JsonValue, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise TypeError(f"contract schema {path.name} must contain a JSON object")
    Draft202012Validator.check_schema(value)
    return value


def contract_schema(path: Path) -> JsonObject:
    """Return a defensive copy of one meta-validated contract schema.

    Each schema file is read and checked against its metaschema once per
    process.
    """

    return deepcopy(_checked_schema(path))


@cache
def contract_validator(path: Path) -> SchemaValidator:
    """Return the validator for one contract schema, compiled once per process."""

    return cast(
        SchemaValidator,
        Draft202012Validator(_checked_schema(path), format_checker=FormatChecker()),
    )


def schema_errors(
    validator: SchemaValidator,
    instance: JsonValue,
) -> list[str]:
    """Return stable, path-qualified errors from one schema validator."""

    issues = sorted(
        validator.iter_errors(instance),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    return [
        f"{format_json_path(issue.absolute_path)}: {issue.message}" for issue in issues
    ]
