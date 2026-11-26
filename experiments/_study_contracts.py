# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Versions of the study-record contract and the records that only v2 keeps.

A study record is a manifest, a bundle manifest, or an attempt report.  Each
declares its document version, and that version alone chooses the schema set
that validates it.  The diagnosis and narrative contracts that a model sees
are not study records: every study contract version uses the ``v1`` copies,
so a v2 study sends a model the same request bytes as a v1 study.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from experiments.model_adapters import (
    OpenAIChatEndpoint,
    ShippedAdapterIdentity,
    running_shipped_adapter,
)
from itself import JsonObject, JsonValue

_CONTRACTS_ROOT: Final = Path(__file__).resolve().parent / "contracts"
_REDACTED: Final = "[redacted]"
# A sanitized runner error is one line, short enough to read in a report.
RUNNER_ERROR_MESSAGE_LIMIT: Final = 500

STREAM_USAGE_OPTION: Final = "stream_options"


@dataclass(frozen=True, slots=True)
class StudyContract:
    """One version of the study-record schemas, found under one directory."""

    directory: str
    document_version: str

    def schema_path(self, name: str) -> Path:
        """Return the path of one named study-record schema in this version."""

        return _CONTRACTS_ROOT / self.directory / f"{name}.schema.json"


V1_CONTRACT: Final = StudyContract(directory="v1", document_version="0.2.0")
V2_CONTRACT: Final = StudyContract(directory="v2", document_version="0.3.0")
STUDY_CONTRACTS: Final = (V1_CONTRACT, V2_CONTRACT)


class UnsupportedStudyContractError(ValueError):
    """Raised when a document declares a version that no study contract has."""


def declared_contract(document: JsonValue, version_field: str) -> StudyContract:
    """Return the study contract whose schemas validate ``document``.

    A document that declares a known version is validated by that version.
    One that is not an object, or omits the field, is reported against v1,
    whose schemas require the field, so it fails with the error a v1 reader
    has always given.  Any other declared value is refused.
    """

    if not isinstance(document, dict) or version_field not in document:
        return V1_CONTRACT
    declared = document[version_field]
    for contract in STUDY_CONTRACTS:
        if declared == contract.document_version:
            return contract
    supported = ", ".join(contract.document_version for contract in STUDY_CONTRACTS)
    raise UnsupportedStudyContractError(
        f"$.{version_field}: unsupported version {declared!r}; "
        f"expected one of {supported}"
    )


def stream_usage_request() -> JsonObject:
    """Return the request option that asks a provider for usage on a stream."""

    return {"include_usage": True}


def stream_usage_requested(extra_body: Mapping[str, JsonValue]) -> bool | None:
    """Return whether request options ask for usage on a stream.

    ``None`` means the options carry ``stream_options`` in a form that no
    study contract registers.
    """

    if STREAM_USAGE_OPTION not in extra_body:
        return False
    if extra_body[STREAM_USAGE_OPTION] == stream_usage_request():
        return True
    return None


def v2_execution_issues(
    generation: JsonObject,
    execution: JsonObject,
    endpoint: OpenAIChatEndpoint,
) -> list[str]:
    """Return how an endpoint differs from a v2 manifest's added settings.

    These are whether a stream asks for token usage and the request deadline,
    which no v1 manifest records.
    """

    issues: list[str] = []
    expected_pairs: list[tuple[str, JsonValue, JsonValue]] = [
        (
            "execution.deadline_seconds",
            execution["deadline_seconds"],
            endpoint.deadline_seconds,
        ),
    ]
    usage_requested = stream_usage_requested(endpoint.extra_body)
    if usage_requested is None:
        issues.append(
            "endpoint extra_body stream_options must be "
            '{"include_usage": true} when present'
        )
    else:
        expected_pairs.append(
            (
                "generation.stream_include_usage",
                generation["stream_include_usage"],
                usage_requested,
            )
        )
    issues.extend(
        f"{field} does not match the bound execution"
        for field, observed, expected in expected_pairs
        if observed != expected
    )
    return issues


def pinned_shipped_adapter(manifest: JsonObject) -> ShippedAdapterIdentity:
    """Return the shipped client that a schema-valid v2 manifest pins."""

    diagnostician = manifest["diagnostician"]
    if not isinstance(diagnostician, dict):
        raise TypeError("diagnostician must be a JSON object")
    return ShippedAdapterIdentity.from_json_object(
        diagnostician["shipped_adapter"],
        "diagnostician.shipped_adapter",
    )


def running_shipped_adapter_issue(pinned: ShippedAdapterIdentity) -> str | None:
    """Describe how the running shipped client differs from a pin, if it does."""

    running = running_shipped_adapter()
    if running == pinned:
        return None
    return (
        "diagnostician.shipped_adapter pins "
        f"{pinned.adapter_id} {pinned.adapter_version}, but the running "
        f"shipped client is {running.adapter_id} {running.adapter_version}"
    )


def _one_line(text: str) -> str:
    lines = (line.strip() for line in text.splitlines())
    joined = "; ".join(line for line in lines if line)
    return "".join(
        character if character.isprintable() else " " for character in joined
    )


def sanitized_error_message(
    error: BaseException,
    *,
    secrets: Sequence[str] = (),
) -> str:
    """Return one bounded, printable line describing ``error``.

    Every non-empty secret is replaced before anything else, so a credential
    that an exception quotes is never recorded, even in part by truncation.
    """

    text = str(error)
    for secret in secrets:
        if secret:
            text = text.replace(secret, _REDACTED)
    line = _one_line(text) or type(error).__name__
    if len(line) > RUNNER_ERROR_MESSAGE_LIMIT:
        line = line[: RUNNER_ERROR_MESSAGE_LIMIT - 3] + "..."
    return line


@dataclass(frozen=True, slots=True)
class RunnerFailure:
    """A sanitized failure of the harness itself after a request was sent.

    An interrupt is recorded with kind ``interrupted``; any other exception is
    a ``runner_error``.  Neither is a provider or model-output failure.
    """

    kind: Literal["runner_error", "interrupted"]
    error_type: str
    message: str

    @classmethod
    def from_exception(
        cls,
        error: BaseException,
        *,
        secrets: Sequence[str] = (),
    ) -> RunnerFailure:
        """Classify and sanitize one exception raised by a runner."""

        return cls(
            kind="interrupted"
            if isinstance(error, KeyboardInterrupt)
            else "runner_error",
            error_type=type(error).__name__,
            message=sanitized_error_message(error, secrets=secrets),
        )

    def error_json_object(self) -> JsonObject:
        """Return the shareable error description an attempt report keeps."""

        return {"type": self.error_type, "message": self.message}


def publish_runner_failure(
    error: BaseException,
    publish: Callable[[], JsonObject],
) -> JsonObject:
    """Publish the runner failure ``error``, then raise it again if an interrupt.

    A runner error returns its published bundle; if publishing fails, that
    failure propagates and the caller's builder quarantines the attempt.  An
    interrupt is raised again once its bundle is published, or once
    publishing has failed, with that failure added as a note, so an interrupt
    always reaches the caller.
    """

    if not isinstance(error, KeyboardInterrupt):
        return publish()
    try:
        publish()
    except Exception as publish_error:
        error.add_note(
            "the interrupted attempt could not be published: "
            + sanitized_error_message(publish_error)
        )
    raise error
