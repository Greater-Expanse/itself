# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""The JSON Schema format checker for Itself's own schemas."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final

from jsonschema import FormatChecker

# The URI-reference rule of RFC 3986, appendix A, one named rule at a time.
# The pattern uses only syntax that Python and JavaScript read the same way,
# so the JavaScript conformance runner can use it verbatim.  ABNF strings are
# case-insensitive, so IPvFuture's "v" also matches "V".
_HEXDIG: Final = "0-9A-Fa-f"
_UNRESERVED: Final = "A-Za-z0-9._~\\-"
_SUB_DELIMS: Final = "!$&'()*+,;="
_PCT_ENCODED: Final = f"%[{_HEXDIG}]{{2}}"
_PCHAR: Final = f"(?:[{_UNRESERVED}{_SUB_DELIMS}:@]|{_PCT_ENCODED})"
_SEGMENT: Final = f"{_PCHAR}*"
_SEGMENT_NZ: Final = f"{_PCHAR}+"
_SEGMENT_NZ_NC: Final = f"(?:[{_UNRESERVED}{_SUB_DELIMS}@]|{_PCT_ENCODED})+"
_PATH_ABEMPTY: Final = f"(?:/{_SEGMENT})*"
_PATH_ABSOLUTE: Final = f"/(?:{_SEGMENT_NZ}(?:/{_SEGMENT})*)?"
_PATH_NOSCHEME: Final = f"{_SEGMENT_NZ_NC}(?:/{_SEGMENT})*"
_PATH_ROOTLESS: Final = f"{_SEGMENT_NZ}(?:/{_SEGMENT})*"
_H16: Final = f"[{_HEXDIG}]{{1,4}}"
_DEC_OCTET: Final = "(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9][0-9]|[0-9])"
_IPV4ADDRESS: Final = f"{_DEC_OCTET}(?:\\.{_DEC_OCTET}){{3}}"
_LS32: Final = f"(?:{_H16}:{_H16}|{_IPV4ADDRESS})"
_IPV6ADDRESS: Final = (
    "(?:"
    + "|".join(
        (
            f"(?:{_H16}:){{6}}{_LS32}",
            f"::(?:{_H16}:){{5}}{_LS32}",
            f"(?:{_H16})?::(?:{_H16}:){{4}}{_LS32}",
            f"(?:(?:{_H16}:){{0,1}}{_H16})?::(?:{_H16}:){{3}}{_LS32}",
            f"(?:(?:{_H16}:){{0,2}}{_H16})?::(?:{_H16}:){{2}}{_LS32}",
            f"(?:(?:{_H16}:){{0,3}}{_H16})?::{_H16}:{_LS32}",
            f"(?:(?:{_H16}:){{0,4}}{_H16})?::{_LS32}",
            f"(?:(?:{_H16}:){{0,5}}{_H16})?::{_H16}",
            f"(?:(?:{_H16}:){{0,6}}{_H16})?::",
        )
    )
    + ")"
)
_IPVFUTURE: Final = f"[Vv][{_HEXDIG}]+\\.[{_UNRESERVED}{_SUB_DELIMS}:]+"
_IP_LITERAL: Final = f"\\[(?:{_IPV6ADDRESS}|{_IPVFUTURE})\\]"
_REG_NAME: Final = f"(?:[{_UNRESERVED}{_SUB_DELIMS}]|{_PCT_ENCODED})*"
_HOST: Final = f"(?:{_IP_LITERAL}|{_IPV4ADDRESS}|{_REG_NAME})"
_USERINFO: Final = f"(?:[{_UNRESERVED}{_SUB_DELIMS}:]|{_PCT_ENCODED})*"
_AUTHORITY: Final = f"(?:{_USERINFO}@)?{_HOST}(?::[0-9]*)?"
_QUERY: Final = f"(?:{_PCHAR}|[/?])*"
_SCHEME: Final = "[A-Za-z][A-Za-z0-9+.\\-]*"
_HIER_PART: Final = (
    f"(?://{_AUTHORITY}{_PATH_ABEMPTY}|{_PATH_ABSOLUTE}|{_PATH_ROOTLESS}|)"
)
_RELATIVE_PART: Final = (
    f"(?://{_AUTHORITY}{_PATH_ABEMPTY}|{_PATH_ABSOLUTE}|{_PATH_NOSCHEME}|)"
)
_URI: Final = f"{_SCHEME}:{_HIER_PART}(?:\\?{_QUERY})?(?:#{_QUERY})?"
_RELATIVE_REF: Final = f"{_RELATIVE_PART}(?:\\?{_QUERY})?(?:#{_QUERY})?"

URI_REFERENCE_PATTERN: Final = f"(?:{_URI}|{_RELATIVE_REF})"
"""RFC 3986 URI-reference, unanchored; it must match a whole string."""

_URI_REFERENCE: Final = re.compile(URI_REFERENCE_PATTERN)


def _is_uri_reference(instance: object) -> bool:
    return (
        not isinstance(instance, str) or _URI_REFERENCE.fullmatch(instance) is not None
    )


def _without_final_line_break(
    check: Callable[[object], bool],
) -> Callable[[object], bool]:
    def strict(instance: object) -> bool:
        if isinstance(instance, str) and instance.endswith("\n"):
            return False
        return check(instance)

    return strict


def schema_format_checker() -> FormatChecker:
    """Return the format checker for Itself's schemas.

    ``uri-reference`` is checked against RFC 3986 with the SDK's own pattern,
    whichever URI library is installed.  jsonschema checks ``date-time`` with
    an anchored regular expression whose ``$`` also matches before a final
    newline, so a timestamp followed by a newline is refused here.
    """

    checker = FormatChecker()
    date_time, raises = checker.checkers["date-time"]
    checker.checkers["date-time"] = (_without_final_line_break(date_time), raises)
    checker.checkers["uri-reference"] = (_is_uri_reference, ())
    return checker
