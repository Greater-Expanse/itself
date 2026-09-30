# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""The JSON Schema format checker for Itself's own schemas."""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from jsonschema import FormatChecker

_LINE_END_SENSITIVE_FORMATS: Final = ("date-time", "uri-reference")


def _without_final_line_break(
    check: Callable[[object], bool],
) -> Callable[[object], bool]:
    def strict(instance: object) -> bool:
        if isinstance(instance, str) and instance.endswith("\n"):
            return False
        return check(instance)

    return strict


def schema_format_checker() -> FormatChecker:
    """Return a format checker that refuses values ending in a line break.

    jsonschema checks ``date-time`` and ``uri-reference`` with anchored regular
    expressions whose ``$`` also matches before a final newline, so a timestamp
    or URI followed by a newline would otherwise pass.
    """

    checker = FormatChecker()
    for name in _LINE_END_SENSITIVE_FORMATS:
        check, raises = checker.checkers[name]
        checker.checkers[name] = (_without_final_line_break(check), raises)
    return checker
