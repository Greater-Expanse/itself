# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Strict JSON decoding shared by every untrusted document boundary."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Iterator
from typing import NoReturn, TypeAlias, cast

from .types import JsonObject, JsonValue

_MAX_SAFE_INTEGER = 2**53 - 1
_MIN_SAFE_INTEGER = -_MAX_SAFE_INTEGER
_MAX_NESTING_DEPTH = 128
_CONTAINERS: tuple[type[object], ...] = (list, dict)

_OpenValue: TypeAlias = tuple[Iterator[tuple[object, object]], bool]
"""An open array or object: its member iterator and whether it has keys."""


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
    if parsed == 0:
        if value.startswith("-"):
            raise IJsonError("negative zero is not accepted")
        # A zero result from a nonzero mantissa underflowed.  Reading the
        # digits keeps any exponent JSON allows in range, which Decimal's is not.
        mantissa = value.lower().partition("e")[0]
        if any(digit in "123456789" for digit in mantissa):
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


def _scalar_violation(value: object) -> str | None:
    """Return why a value other than an array or object is not I-JSON, or None."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value < _MIN_SAFE_INTEGER or value > _MAX_SAFE_INTEGER:
            return "integer exceeds the interoperable IEEE 754 safe range"
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return "number is not finite"
        if not value and math.copysign(1.0, value) < 0:
            return "negative zero is not accepted"
        return None
    if isinstance(value, str):
        # An ASCII string cannot hold a surrogate, so only others are encoded.
        if not value.isascii():
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                return "string contains a Unicode surrogate"
        return None
    return f"{type(value).__name__} is not a JSON value"


def _object_key_violation(key: object) -> str | None:
    """Return why an object key is not I-JSON, or None."""

    if not isinstance(key, str):
        return "object key is not a string"
    if not key.isascii():
        try:
            key.encode("utf-8")
        except UnicodeEncodeError:
            return "object key contains a Unicode surrogate"
    return None


def _opened(value: object) -> _OpenValue | None:
    if isinstance(value, list):
        return enumerate(cast(list[object], value)), False
    if isinstance(value, dict):
        return iter(cast(dict[object, object], value).items()), True
    return None


def _segment(key: object, is_object: bool) -> str:
    return f".{key}" if is_object else f"[{key}]"


def _require_i_json(value: object, path: str, max_depth: int) -> None:
    opened = _opened(value)
    if opened is None:
        violation = _scalar_violation(value)
        if violation is not None:
            raise IJsonError(f"{path}: {violation}")
        return
    # The stack holds one member iterator per open array or object, so its
    # height is the nesting depth.  Each iterator resumes where it paused, which
    # keeps checks in document order.  Each level keeps only the path segment
    # that leads to it, and a full path is rendered only for a violation, so
    # memory grows linearly with depth.  An open container that is reached
    # again contains itself, and no finite JSON text can represent it.
    open_values = [opened]
    segments = [path]
    open_ids = [id(value)]
    while open_values:
        members, is_object = open_values[-1]
        for key, member in members:
            if is_object:
                key_violation = _object_key_violation(key)
                if key_violation is not None:
                    raise IJsonError(f"{''.join(segments)}: {key_violation}")
            nested = _opened(member)
            if nested is not None:
                if id(member) in open_ids:
                    raise IJsonError(
                        f"{''.join(segments)}{_segment(key, is_object)}: "
                        "value contains itself"
                    )
                if len(open_values) >= max_depth:
                    raise IJsonError(f"JSON nesting exceeds {max_depth} levels")
                open_values.append(nested)
                segments.append(_segment(key, is_object))
                open_ids.append(id(member))
                break
            violation = _scalar_violation(member)
            if violation is not None:
                raise IJsonError(
                    f"{''.join(segments)}{_segment(key, is_object)}: {violation}"
                )
        else:
            open_values.pop()
            segments.pop()
            open_ids.pop()


def ensure_i_json(value: JsonValue, *, path: str = "$") -> None:
    """Require the interoperable JSON value domain used by RFC 8785.

    Values may nest at most 128 levels deep, the limit the strict reader
    enforces, so every value that passes can be written and read back.  The
    walk keeps its own stack, so nesting cannot exhaust the interpreter's; it
    refuses a value that contains itself and reports the first violation in
    document order.
    """

    _require_i_json(value, path, _MAX_NESTING_DEPTH)


def strict_json_loads(source: str | bytes | bytearray) -> JsonValue:
    """Decode unambiguous I-JSON with duplicate-key rejection.

    Bytes must be UTF-8, as I-JSON requires, and a byte order mark is rejected
    as it is in text.  Values may nest at most 128 levels deep.
    """

    runtime_source = cast(object, source)
    if isinstance(runtime_source, str):
        text = runtime_source
    elif isinstance(runtime_source, bytes | bytearray):
        text = runtime_source.decode("utf-8")
    else:
        raise TypeError("JSON source must be str, bytes, or bytearray")
    try:
        value = cast(
            JsonValue,
            json.loads(
                text,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_nonstandard_constant,
                parse_float=_parse_float,
                parse_int=_parse_integer,
            ),
        )
    except RecursionError:
        raise IJsonError("JSON nesting exceeds the supported depth") from None
    _require_i_json(value, "$", _MAX_NESTING_DEPTH)
    return value
