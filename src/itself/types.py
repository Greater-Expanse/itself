# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Public structural types shared by the reference SDK."""

from __future__ import annotations

from os import PathLike
from typing import TypeAlias

JsonScalar: TypeAlias = None | bool | int | float | str
"""A scalar value representable in JSON."""

JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
"""A recursively typed, in-memory JSON value."""

JsonObject: TypeAlias = dict[str, JsonValue]
"""A JSON object used as the wire representation of a protocol record."""

StrPath: TypeAlias = str | PathLike[str]
"""A path accepted by public file-oriented APIs."""
