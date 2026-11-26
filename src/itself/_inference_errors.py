# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Failure stages and the sanitized error of one structured-inference attempt."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from ._artifact_sinks import ArtifactReference


class InferenceFailure(StrEnum):
    """Stable failure stages for one structured-inference attempt."""

    CONFIGURATION = "configuration"
    TRANSPORT = "transport"
    RESPONSE_TOO_LARGE = "response_too_large"
    HTTP_AUTHENTICATION = "http_authentication"
    HTTP_AUTHORIZATION = "http_authorization"
    HTTP_RATE_LIMIT = "http_rate_limit"
    HTTP_SERVER = "http_server"
    HTTP_STATUS = "http_status"
    REFUSAL = "refusal"
    INCOMPLETE = "incomplete"
    RESPONSE_ENVELOPE = "response_envelope"
    RESPONSE_JSON = "response_json"
    RESPONSE_SCHEMA = "response_schema"
    RESPONSE_USAGE = "response_usage"
    ARTIFACT = "artifact"


_RETRYABLE_FAILURES: Final = frozenset(
    {
        InferenceFailure.TRANSPORT,
        InferenceFailure.HTTP_RATE_LIMIT,
        InferenceFailure.HTTP_SERVER,
    }
)


class InferenceError(RuntimeError):
    """Sanitized failure from one explicit inference attempt.

    ``status_code`` is the HTTP status of the response when one is known: the
    status of an HTTP-status failure, or that of a response the transport
    rejected after its headers arrived, which may be 2xx.  It is ``None``
    when no status is known.  ``failure`` alone classifies the failure.
    """

    def __init__(
        self,
        failure: InferenceFailure,
        detail: str,
        *,
        status_code: int | None = None,
        artifact: ArtifactReference | None = None,
    ) -> None:
        self.failure: InferenceFailure = failure
        self.detail: str = detail
        self.status_code: int | None = status_code
        self.artifact: ArtifactReference | None = artifact
        super().__init__(f"{failure.value}: {detail}")

    def __reduce__(
        self,
    ) -> tuple[type[InferenceError], tuple[InferenceFailure, str], dict[str, object]]:
        # The default reduction replays only the rendered message, which cannot
        # rebuild an error that requires its failure and detail.
        return (type(self), (self.failure, self.detail), self.__dict__)

    @property
    def retryable(self) -> bool:
        """Whether a later, separately recorded attempt may be reasonable."""

        return self.failure in _RETRYABLE_FAILURES


def http_failure(status_code: int) -> InferenceFailure:
    """Classify an HTTP status as the stage of an inference failure."""

    if status_code == 401:
        return InferenceFailure.HTTP_AUTHENTICATION
    if status_code == 403:
        return InferenceFailure.HTTP_AUTHORIZATION
    if status_code == 429:
        return InferenceFailure.HTTP_RATE_LIMIT
    if 500 <= status_code <= 599:
        return InferenceFailure.HTTP_SERVER
    return InferenceFailure.HTTP_STATUS


# Callers import these from itself.inference, which they keep as their module
# so that pickles, tracebacks, and reprs name the same place as before.
InferenceFailure.__module__ = "itself.inference"
InferenceError.__module__ = "itself.inference"
