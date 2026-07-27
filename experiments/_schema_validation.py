# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Shared typed diagnostics for experiment JSON Schema contracts."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from jsonschema.exceptions import ValidationError

from itself import JsonValue
from itself._json import format_json_path


class SchemaValidator(Protocol):
    """Typed surface used from the partially typed jsonschema dependency."""

    def iter_errors(self, instance: JsonValue) -> Iterator[ValidationError]: ...


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
