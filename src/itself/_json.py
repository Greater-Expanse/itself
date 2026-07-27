# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Strict JSON decoding shared by every untrusted document boundary."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from decimal import Decimal
from typing import NoReturn, cast

from .types import JsonObject, JsonValue

_MAX_SAFE_INTEGER = 2**53 - 1
_MIN_SAFE_INTEGER = -_MAX_SAFE_INTEGER


class IJsonError(ValueError):
    """Raised when a value cannot be represented unambiguously as I-JSON."""


def format_json_path(path: Iterable[object]) -> str:
    """Render one jsonschema-style absolute path for a diagnostic."""

    values = tuple(path)
    if not values:
        return "$"
    return "$" + "".join(
        f"[{item}]" if isinstance(item, int) else f".{item}" for item in values
    )


def _reject_nonstandard_constant(value: str) -> NoReturn:
    raise ValueError(f"non-standard JSON numeric constant {value!r}")


def _parse_integer(value: str) -> int:
    parsed = int(value)
    if value.startswith("-") and parsed == 0:
        raise IJsonError("negative zero is not accepted")
    if parsed < _MIN_SAFE_INTEGER or parsed > _MAX_SAFE_INTEGER:
        raise IJsonError(
            f"integer {value} exceeds the interoperable IEEE 754 safe range"
        )
    return parsed


def _parse_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise IJsonError(f"number {value} is not a finite IEEE 754 value")
    exact = Decimal(value)
    if parsed == 0:
        if value.startswith("-"):
            raise IJsonError("negative zero is not accepted")
        if exact != 0:
            raise IJsonError(f"number {value} underflows IEEE 754 binary64")
    return parsed


def _object_without_duplicate_keys(
    pairs: list[tuple[str, JsonValue]],
) -> JsonObject:
    value: JsonObject = {}
    for key, item in pairs:
        try:
            key.encode("utf-8")
        except UnicodeEncodeError as error:
            raise IJsonError("object key contains a Unicode surrogate") from error
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


def ensure_i_json(value: JsonValue, *, path: str = "$") -> None:
    """Require the interoperable JSON value domain used by RFC 8785."""

    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if value < _MIN_SAFE_INTEGER or value > _MAX_SAFE_INTEGER:
            raise IJsonError(
                f"{path}: integer exceeds the interoperable IEEE 754 safe range"
            )
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise IJsonError(f"{path}: number is not finite")
        if not value and math.copysign(1.0, value) < 0:
            raise IJsonError(f"{path}: negative zero is not accepted")
        return
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise IJsonError(f"{path}: string contains a Unicode surrogate") from error
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            ensure_i_json(item, path=f"{path}[{index}]")
        return
    for key, item in value.items():
        try:
            key.encode("utf-8")
        except UnicodeEncodeError as error:
            raise IJsonError(
                f"{path}: object key contains a Unicode surrogate"
            ) from error
        ensure_i_json(item, path=f"{path}.{key}")


def strict_json_loads(source: str | bytes | bytearray) -> JsonValue:
    """Decode unambiguous I-JSON with duplicate-key rejection."""

    value = cast(
        JsonValue,
        json.loads(
            source,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
            parse_float=_parse_float,
            parse_int=_parse_integer,
        ),
    )
    ensure_i_json(value)
    return value
