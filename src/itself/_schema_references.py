# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""JSON Schema validators that resolve references only within their schema.

Left to its defaults, jsonschema retrieves a referenced schema it does not
hold, over the network or from a file, while it validates an instance.  The
validators here never retrieve anything: a schema whose references name a
resource it does not contain is refused before use, and a reference that
still cannot be resolved fails validation instead of being fetched.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from functools import cache
from typing import TYPE_CHECKING, Final, NoReturn, cast
from urllib.parse import urldefrag, urljoin

from jsonschema import Draft202012Validator, FormatChecker

from .types import JsonValue

if TYPE_CHECKING:
    from referencing.jsonschema import SchemaRegistry

_REFERENCE_KEYWORDS: Final = ("$ref", "$dynamicRef")
# jsonschema 4.18 replaced RefResolver with the referencing library's
# registries; the package supports releases on both sides of that change.
_ACCEPTS_REGISTRY: Final = (
    "registry" in inspect.signature(Draft202012Validator).parameters
)


class SchemaReferenceError(ValueError):
    """Raised when a schema refers to a resource that it does not contain."""


def _resource(base: str, reference: str) -> str:
    # As in the referencing library, a fragment-only reference stays in the
    # current resource even when urljoin cannot resolve against its base.
    if reference.startswith("#"):
        return urldefrag(base).url
    return urldefrag(urljoin(base, reference)).url


def require_local_references(schema: Mapping[str, JsonValue]) -> None:
    """Refuse a schema with a reference to a resource it does not contain.

    Every ``$ref`` and ``$dynamicRef`` must name the root schema or a
    subschema that declares a matching ``$id``, so a reference to a JSON
    Schema meta-schema is refused too.  The check reads these keywords and
    ``$id`` anywhere in the document, including inside ``const`` or ``enum``
    values that validation ignores, so it is a construction-time refusal, not
    the guarantee: ``offline_validator`` still never retrieves a reference
    this check let through.
    """

    resources: set[str] = set()
    if not isinstance(schema.get("$id"), str):
        resources.add("")
    references: list[tuple[str, str]] = []
    pending: list[tuple[JsonValue, str]] = [(dict(schema), "")]
    while pending:
        value, base = pending.pop()
        if isinstance(value, list):
            pending.extend((item, base) for item in value)
            continue
        if not isinstance(value, dict):
            continue
        identifier = value.get("$id")
        if isinstance(identifier, str):
            base = _resource(base, identifier)
            resources.add(base)
        for keyword in _REFERENCE_KEYWORDS:
            reference = value.get(keyword)
            if isinstance(reference, str):
                references.append((reference, _resource(base, reference)))
        pending.extend((item, base) for item in value.values())

    for reference, resource in references:
        if resource not in resources:
            raise SchemaReferenceError(
                f"schema reference {reference!r} does not resolve within the "
                "schema; referenced schemas are never retrieved"
            )


def _empty_registry() -> SchemaRegistry:
    from referencing import Registry

    return Registry()


def _offline_resolver(schema: Mapping[str, JsonValue]) -> object:
    # Only jsonschema releases before 4.18 reach this branch, where RefResolver
    # is current rather than deprecated.
    from jsonschema import RefResolver  # pyright: ignore[reportDeprecated]
    from jsonschema.exceptions import RefResolutionError

    class OfflineRefResolver(RefResolver):  # pyright: ignore[reportDeprecated]
        def resolve_remote(self, uri: str) -> NoReturn:
            raise RefResolutionError(f"schema reference {uri!r} is not retrieved")

    # The stubs leave from_schema's return type unannotated.
    from_schema = cast(
        Callable[..., object],
        OfflineRefResolver.from_schema,  # pyright: ignore[reportUnknownMemberType]
    )
    return from_schema(dict(schema), id_of=Draft202012Validator.ID_OF)


def offline_validator(
    schema: Mapping[str, JsonValue],
    *,
    format_checker: FormatChecker,
) -> Draft202012Validator:
    """Return a Draft 2020-12 validator that never retrieves a reference."""

    if _ACCEPTS_REGISTRY:
        # jsonschema adds its bundled meta-schemas to the registry it is given.
        return Draft202012Validator(
            schema,
            format_checker=format_checker,
            registry=_empty_registry(),
        )
    return Draft202012Validator(
        schema,
        resolver=_offline_resolver(schema),
        format_checker=format_checker,
    )


@cache
def unresolved_reference_errors() -> tuple[type[Exception], ...]:
    """Return the exceptions a validator raises for an unresolvable reference."""

    if _ACCEPTS_REGISTRY:
        from referencing.exceptions import Unresolvable

        return (Unresolvable,)
    from jsonschema.exceptions import RefResolutionError

    return (RefResolutionError,)
