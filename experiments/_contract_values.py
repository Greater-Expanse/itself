# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Runtime type narrowing for schema-validated experiment values."""

from __future__ import annotations

from typing import cast

from itself import JsonObject, JsonValue


def _subject(field: str | None) -> str:
    return "value" if field is None else f"field {field!r}"


def object_value(value: JsonValue, field: str | None = None) -> JsonObject:
    """Narrow a schema-validated object field."""

    if not isinstance(value, dict):
        raise TypeError(f"schema-validated {_subject(field)} was not an object")
    return value


def array_value(value: JsonValue, field: str | None = None) -> list[JsonValue]:
    """Narrow a schema-validated array field."""

    if not isinstance(value, list):
        raise TypeError(f"schema-validated {_subject(field)} was not an array")
    return value


def string_value(value: JsonValue, field: str | None = None) -> str:
    """Narrow a schema-validated string field."""

    if not isinstance(value, str):
        raise TypeError(f"schema-validated {_subject(field)} was not a string")
    return value


def string_values(
    value: JsonValue,
    field: str | None = None,
) -> tuple[str, ...]:
    """Narrow a schema-validated string-array field."""

    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"schema-validated {_subject(field)} was not a string array")
    return tuple(cast(str, item) for item in value)


def integer_value(value: JsonValue, field: str | None = None) -> int:
    """Narrow a schema-validated integer field without accepting booleans."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"schema-validated {_subject(field)} was not an integer")
    return value


def optional_number_value(
    value: JsonValue,
    field: str | None = None,
) -> float | None:
    """Narrow a nullable schema-validated number field."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"schema-validated {_subject(field)} was not a number")
    return float(value)


def number_value(value: JsonValue, field: str | None = None) -> float:
    """Narrow a required schema-validated number field."""

    number = optional_number_value(value, field)
    if number is None:
        raise TypeError(f"schema-validated {_subject(field)} was null")
    return number


def optional_integer_value(
    value: JsonValue,
    field: str | None = None,
) -> int | None:
    """Narrow a nullable schema-validated integer field."""

    if value is None:
        return None
    return integer_value(value, field)
