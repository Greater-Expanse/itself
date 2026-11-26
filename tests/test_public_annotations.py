# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import inspect
import typing
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

import itself
import itself.inference as inference

# Classes defined in private modules whose __module__ names itself.inference,
# so their annotations resolve in that module's namespace.
MOVED_INFERENCE_CLASSES: Final = (
    "ArtifactReference",
    "ArtifactSink",
    "DirectoryArtifactSink",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "InferenceError",
    "InferenceFailure",
    "UrllibHttpTransport",
)


def _annotated(name: str, value: object) -> Iterator[tuple[str, object]]:
    """Yield a public object and every function or accessor its class defines."""

    yield name, value
    if not inspect.isclass(value):
        return
    members: dict[str, object] = dict(vars(value))
    for attribute, member in members.items():
        if isinstance(member, staticmethod | classmethod):
            member = typing.cast(object, member.__func__)  # pyright: ignore[reportUnknownMemberType]
        if isinstance(member, property):
            for accessor in (member.fget, member.fset, member.fdel):
                if accessor is not None:
                    yield f"{name}.{attribute}", accessor
        elif inspect.isfunction(member):
            yield f"{name}.{attribute}", member


def _public_annotated() -> list[tuple[str, object]]:
    # Type aliases such as JsonObject are neither, and get_type_hints takes
    # only modules, classes, and callables defined in Python.
    return [
        annotated
        for name in itself.__all__
        if inspect.isclass(value := getattr(itself, name)) or inspect.isfunction(value)
        for annotated in _annotated(name, value)
    ]


PUBLIC_ANNOTATED: Final = _public_annotated()


def test_public_annotations_are_collected() -> None:
    names = {name for name, _ in PUBLIC_ANNOTATED}

    assert len(PUBLIC_ANNOTATED) > 600
    assert {"ValidationPolicy", "BundleValidator.errors", "Ledger.records"} <= names


@pytest.mark.parametrize(
    ("name", "value"), PUBLIC_ANNOTATED, ids=[name for name, _ in PUBLIC_ANNOTATED]
)
def test_public_annotations_resolve_at_runtime(name: str, value: object) -> None:
    typing.get_type_hints(value)


@pytest.mark.parametrize("name", MOVED_INFERENCE_CLASSES)
def test_moved_inference_classes_resolve_in_the_inference_module(name: str) -> None:
    cls = getattr(inference, name)

    assert cls.__module__ == "itself.inference"
    for _, member in _annotated(name, cls):
        typing.get_type_hints(member)


def test_directory_artifact_sink_resolves_its_path_field() -> None:
    assert typing.get_type_hints(inference.DirectoryArtifactSink) == {
        "root": Path,
        "uri_prefix": str | None,
    }
