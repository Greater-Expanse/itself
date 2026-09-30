# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Typed questions for decision models over the System One wire format.

A decision model answers closed questions about a state with probability
distributions over declared options and generates no text.  The client in this
module sends ``choice``, ``noul``, and ``score`` questions to an endpoint that
implements the ``/v1/systemone`` request format.  It captures each exact request
and bounded response before interpretation, validates every distribution
locally, and by default asks each ordered question a second time with its
options reversed, so that position bias is measured rather than absorbed.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from itertools import combinations
from types import MappingProxyType
from typing import Final, Protocol, cast

from ._endpoints import (
    IDENTIFIER_PATTERN,
    content_type,
    defensive_extra_body,
    endpoint_url,
    frozen_extra_headers,
    frozen_query_parameters,
    normalized_base_url,
    normalized_resource_path,
    token_counts,
    validate_deadline,
    validate_timeout,
)
from ._json import strict_json_loads
from .inference import (
    ArtifactReference,
    ArtifactSink,
    EnvironmentCredential,
    HttpRequest,
    HttpTransport,
    InferenceError,
    InferenceFailure,
    InferenceIdentity,
    InferenceUsage,
    UrllibHttpTransport,
)
from .types import JsonObject, JsonValue

DECISION_MODEL_ADAPTER_ID: Final = "systemone-compatible-decisions"
DECISION_MODEL_ADAPTER_VERSION: Final = "0.1.0"

_MAX_OPTIONS: Final = 255
_MAX_PROBABILITY_TOLERANCE: Final = 0.01
_RESERVED_BODY_FIELDS: Final = frozenset({"model", "questions", "state"})


class DecisionQuestionType(StrEnum):
    """Wire type of one decision-model question."""

    CHOICE = "choice"
    NOUL = "noul"
    SCORE = "score"


class OptionOrder(StrEnum):
    """How one request orders each question's options."""

    DECLARED = "declared"
    REVERSED = "reversed"


COUNTERBALANCED_ORDERS: Final[tuple[OptionOrder, ...]] = (
    OptionOrder.DECLARED,
    OptionOrder.REVERSED,
)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty text without outer space")
    return value


def _option_key(value: object) -> str:
    if not isinstance(value, str) or IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError("choice option keys must be valid identifiers")
    return value


def _levels(value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError("levels must be a sequence of level descriptions")
    levels = tuple(cast(Sequence[object], value))
    if not 2 <= len(levels) <= _MAX_OPTIONS:
        raise ValueError(f"a score question needs between 2 and {_MAX_OPTIONS} levels")
    return tuple(_text(level, "score levels") for level in levels)


class DecisionQuestion:
    """Common base of the typed questions a decision model answers.

    The wire format defines three question types, so only ``ChoiceQuestion``,
    ``NoulQuestion``, and ``ScoreQuestion`` are sent.
    """

    __slots__ = ()

    @property
    def question_type(self) -> DecisionQuestionType:
        """The question's wire type."""

        raise NotImplementedError

    @property
    def option_keys(self) -> tuple[str, ...]:
        """Answer keys in declared order."""

        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ChoiceQuestion(DecisionQuestion):
    """Pick one declared option; the answer is a distribution over option keys.

    ``criteria`` maps each option key to an optional description, in the order
    the options are shown.  A two-option choice question is the counterbalanced
    form of a yes-or-no question.
    """

    instructions: str
    criteria: Mapping[str, str | None]

    def __post_init__(self) -> None:
        _text(self.instructions, "instructions")
        criteria = dict(self.criteria)
        if not 2 <= len(criteria) <= _MAX_OPTIONS:
            raise ValueError(
                f"a choice question needs between 2 and {_MAX_OPTIONS} options"
            )
        for key, description in criteria.items():
            _option_key(key)
            if description is not None:
                _text(description, "choice option descriptions")
        object.__setattr__(self, "criteria", MappingProxyType(criteria))

    @property
    def question_type(self) -> DecisionQuestionType:
        return DecisionQuestionType.CHOICE

    @property
    def option_keys(self) -> tuple[str, ...]:
        """Answer keys in declared order."""

        return tuple(self.criteria)


@dataclass(frozen=True, slots=True)
class NoulQuestion(DecisionQuestion):
    """A yes-or-no question; the answer is the probability that it is true.

    Its two answers have no order on the wire, so asking it again with reversed
    options cannot reveal position bias.  Use a two-option ``ChoiceQuestion``
    when order effects need to be measured.
    """

    instructions: str
    true_description: str | None = None
    false_description: str | None = None

    def __post_init__(self) -> None:
        _text(self.instructions, "instructions")
        if self.true_description is not None:
            _text(self.true_description, "true_description")
        if self.false_description is not None:
            _text(self.false_description, "false_description")

    @property
    def question_type(self) -> DecisionQuestionType:
        return DecisionQuestionType.NOUL

    @property
    def option_keys(self) -> tuple[str, ...]:
        """Answer keys: ``true`` then ``false``."""

        return ("true", "false")


@dataclass(frozen=True, slots=True)
class ScoreQuestion(DecisionQuestion):
    """Rate on an ordered rubric; the answer is a distribution over its levels.

    ``levels`` run from lowest to highest.  Answers key each level by its
    declared index, so ``"0"`` is always the lowest level, whatever order a
    request showed the levels in.
    """

    instructions: str
    levels: Sequence[str]

    def __post_init__(self) -> None:
        _text(self.instructions, "instructions")
        object.__setattr__(self, "levels", _levels(self.levels))

    @property
    def question_type(self) -> DecisionQuestionType:
        return DecisionQuestionType.SCORE

    @property
    def option_keys(self) -> tuple[str, ...]:
        """Answer keys: each level's declared index, lowest first."""

        return tuple(str(index) for index in range(len(self.levels)))


def _empty_query() -> Mapping[str, str]:
    return {}


def _empty_headers() -> Mapping[str, str]:
    return {}


def _empty_body() -> Mapping[str, JsonValue]:
    return {}


@dataclass(frozen=True, slots=True)
class DecisionModelEndpoint:
    """One declared decision-model endpoint and model configuration.

    ``probability_tolerance`` bounds how far a returned distribution may sum
    from one, and how far a score may sit from its distribution's expected
    level, before the answer is rejected.
    """

    actor_id: str
    base_url: str
    model: str
    credential: EnvironmentCredential | None = None
    resource_path: str = "systemone"
    query_parameters: Mapping[str, str] = field(
        default_factory=_empty_query,
        repr=False,
    )
    timeout_seconds: float = 90.0
    probability_tolerance: float = 1e-6
    allow_insecure_http: bool = False
    extra_headers: Mapping[str, str] = field(
        default_factory=_empty_headers,
        repr=False,
    )
    extra_body: Mapping[str, JsonValue] = field(
        default_factory=_empty_body,
        repr=False,
    )
    deadline_seconds: float | None = None

    def __post_init__(self) -> None:
        if IDENTIFIER_PATTERN.fullmatch(self.actor_id) is None:
            raise ValueError("actor_id must be a valid identifier")
        object.__setattr__(
            self,
            "base_url",
            normalized_base_url(
                self.base_url,
                credential_bearing=self.credential is not None,
                allow_insecure_http=self.allow_insecure_http,
            ),
        )
        object.__setattr__(
            self,
            "resource_path",
            normalized_resource_path(self.resource_path),
        )
        if not self.model or self.model != self.model.strip():
            raise ValueError("model must be non-empty without outer space")
        validate_timeout(self.timeout_seconds)
        validate_deadline(self.deadline_seconds)
        tolerance = self.probability_tolerance
        if (
            isinstance(tolerance, bool)
            or not math.isfinite(tolerance)
            or not 0 <= tolerance <= _MAX_PROBABILITY_TOLERANCE
        ):
            raise ValueError(
                "probability_tolerance must be between 0 and "
                f"{_MAX_PROBABILITY_TOLERANCE}"
            )
        object.__setattr__(
            self,
            "query_parameters",
            frozen_query_parameters(self.query_parameters),
        )
        object.__setattr__(
            self,
            "extra_headers",
            frozen_extra_headers(
                self.extra_headers,
                credential_header=(
                    None if self.credential is None else self.credential.header
                ),
            ),
        )
        object.__setattr__(
            self,
            "extra_body",
            defensive_extra_body(self.extra_body, reserved=_RESERVED_BODY_FIELDS),
        )

    @property
    def url(self) -> str:
        """Return the fully rendered endpoint URL without credentials."""

        return endpoint_url(self.base_url, self.resource_path, self.query_parameters)


def _questions(value: object) -> dict[str, DecisionQuestion]:
    if not isinstance(value, Mapping):
        raise ValueError("questions must map question identifiers to questions")
    questions: dict[str, DecisionQuestion] = {}
    for question_id, question in cast(Mapping[object, object], value).items():
        if (
            not isinstance(question_id, str)
            or IDENTIFIER_PATTERN.fullmatch(question_id) is None
        ):
            raise ValueError("question identifiers must be valid identifiers")
        if not isinstance(question, ChoiceQuestion | NoulQuestion | ScoreQuestion):
            raise ValueError(
                "questions must be ChoiceQuestion, NoulQuestion, or ScoreQuestion"
            )
        questions[question_id] = question
    if not questions:
        raise ValueError("at least one question is required")
    return questions


def _state(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("state must not be empty")
    elif isinstance(value, dict | list):
        if not value:
            raise ValueError("state must not be empty")
    else:
        raise ValueError("state must be JSON text, an object, or an array")
    return value


def _order(value: object) -> OptionOrder:
    if not isinstance(value, OptionOrder):
        raise ValueError("order must be an OptionOrder")
    return value


def _orders(value: object) -> tuple[OptionOrder, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError("orders must be a sequence of OptionOrder values")
    orders = tuple(_order(order) for order in cast(Sequence[object], value))
    if not orders:
        raise ValueError("orders must contain at least one OptionOrder")
    if len(orders) != len(set(orders)):
        raise ValueError("orders must not repeat an OptionOrder")
    return orders


def _wire_question(question: DecisionQuestion, order: OptionOrder) -> JsonObject:
    reverse = order is OptionOrder.REVERSED
    if isinstance(question, ChoiceQuestion):
        options = list(question.criteria.items())
        if reverse:
            options.reverse()
        criteria: JsonObject = dict(options)
        return {
            "type": question.question_type.value,
            "instructions": question.instructions,
            "criteria": criteria,
        }
    if isinstance(question, NoulQuestion):
        value: JsonObject = {
            "type": question.question_type.value,
            "instructions": question.instructions,
        }
        descriptions: JsonObject = {}
        if question.true_description is not None:
            descriptions["true"] = question.true_description
        if question.false_description is not None:
            descriptions["false"] = question.false_description
        if descriptions:
            value["criteria"] = descriptions
        return value
    if isinstance(question, ScoreQuestion):
        levels: list[JsonValue] = list(question.levels)
        if reverse:
            levels.reverse()
        return {
            "type": question.question_type.value,
            "instructions": question.instructions,
            "criteria": levels,
        }
    raise TypeError("unsupported decision question type")


def build_decision_payload(
    endpoint: DecisionModelEndpoint,
    state: JsonValue,
    questions: Mapping[str, DecisionQuestion],
    *,
    order: OptionOrder = OptionOrder.DECLARED,
) -> JsonObject:
    """Render one decision request without credentials.

    Each question's options are listed in ``order``.  Serialize the payload
    without sorting keys, or the options lose that order.
    """

    asked = _questions(questions)
    selected_order = _order(order)
    payload: JsonObject = {
        "model": endpoint.model,
        "state": deepcopy(_state(state)),
        "questions": {
            question_id: _wire_question(question, selected_order)
            for question_id, question in asked.items()
        },
    }
    payload.update(endpoint.extra_body)
    return payload


def _most_likely(probabilities: Mapping[str, float]) -> str:
    return max(probabilities, key=probabilities.__getitem__)


def _expected_level(
    question_type: DecisionQuestionType,
    probabilities: Mapping[str, float],
) -> float | None:
    if question_type is not DecisionQuestionType.SCORE:
        return None
    return math.fsum(
        int(level) * probability for level, probability in probabilities.items()
    )


@dataclass(frozen=True, slots=True)
class DecisionModelAnswer:
    """One question's validated answer from one request, in declared terms.

    ``reported_confidence`` is the endpoint's own confidence figure when it
    reports one; implementations define it differently.
    """

    question_type: DecisionQuestionType
    probabilities: Mapping[str, float]
    reported_confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.probabilities:
            raise ValueError("an answer needs at least one probability")
        object.__setattr__(
            self,
            "probabilities",
            MappingProxyType(dict(self.probabilities)),
        )

    @property
    def most_likely(self) -> str:
        """The most probable answer key; ties go to the first declared option."""

        return _most_likely(self.probabilities)

    @property
    def expected_level(self) -> float | None:
        """The probability-weighted level index of a score answer."""

        return _expected_level(self.question_type, self.probabilities)

    def to_json_object(self) -> JsonObject:
        """Return a JSON representation suitable for an artifact."""

        return {
            "type": self.question_type.value,
            "probabilities": dict(self.probabilities),
            "reported_confidence": self.reported_confidence,
        }


@dataclass(frozen=True, slots=True)
class DecisionModelEstimate:
    """One question's answers combined across the requests that asked it.

    ``probabilities`` is the mean distribution.  With more than one request,
    ``order_gap`` is the largest difference between requests in the
    probability of any option, and ``order_flip`` records whether the most
    likely option changed.  Both are ``None`` for a question asked once.
    """

    question_type: DecisionQuestionType
    probabilities: Mapping[str, float]
    attempt_count: int
    order_gap: float | None = None
    order_flip: bool | None = None

    def __post_init__(self) -> None:
        if not self.probabilities:
            raise ValueError("an estimate needs at least one probability")
        if isinstance(self.attempt_count, bool) or self.attempt_count < 1:
            raise ValueError("attempt_count must be a positive integer")
        object.__setattr__(
            self,
            "probabilities",
            MappingProxyType(dict(self.probabilities)),
        )

    @property
    def most_likely(self) -> str:
        """The most probable answer key; ties go to the first declared option."""

        return _most_likely(self.probabilities)

    @property
    def expected_level(self) -> float | None:
        """The probability-weighted level index of a score estimate."""

        return _expected_level(self.question_type, self.probabilities)

    def to_json_object(self) -> JsonObject:
        """Return a JSON representation suitable for an artifact."""

        return {
            "type": self.question_type.value,
            "probabilities": dict(self.probabilities),
            "attempt_count": self.attempt_count,
            "order_gap": self.order_gap,
            "order_flip": self.order_flip,
        }


def _identity_json(identity: InferenceIdentity) -> JsonObject:
    return {
        "actor_id": identity.actor_id,
        "adapter_id": identity.adapter_id,
        "adapter_version": identity.adapter_version,
        "requested_model": identity.requested_model,
        "resolved_model": identity.resolved_model,
    }


def _usage_json(usage: InferenceUsage) -> JsonObject:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "wall_time_ms": usage.wall_time_ms,
    }


@dataclass(frozen=True, slots=True)
class DecisionModelAttempt:
    """One captured request, its captured response, and the validated answers."""

    order: OptionOrder
    identity: InferenceIdentity
    request: ArtifactReference
    raw_response: ArtifactReference
    usage: InferenceUsage
    answers: Mapping[str, DecisionModelAnswer]

    def __post_init__(self) -> None:
        object.__setattr__(self, "answers", MappingProxyType(dict(self.answers)))

    def to_json_object(self) -> JsonObject:
        """Return a JSON representation suitable for an artifact."""

        return {
            "order": self.order.value,
            "identity": _identity_json(self.identity),
            "request": self.request.to_json_object(),
            "raw_response": self.raw_response.to_json_object(),
            "usage": _usage_json(self.usage),
            "answers": {
                question_id: answer.to_json_object()
                for question_id, answer in self.answers.items()
            },
        }


def _total(values: Iterable[int | None]) -> int | None:
    total = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total


@dataclass(frozen=True, slots=True)
class DecisionModelResult:
    """Every attempt behind one decision and the combined estimate per question."""

    identity: InferenceIdentity
    attempts: tuple[DecisionModelAttempt, ...]
    estimates: Mapping[str, DecisionModelEstimate]

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempts", tuple(self.attempts))
        object.__setattr__(
            self,
            "estimates",
            MappingProxyType(dict(self.estimates)),
        )

    @property
    def usage(self) -> InferenceUsage:
        """Token counts and wall time summed over every attempt."""

        return InferenceUsage(
            input_tokens=_total(
                attempt.usage.input_tokens for attempt in self.attempts
            ),
            output_tokens=_total(
                attempt.usage.output_tokens for attempt in self.attempts
            ),
            total_tokens=_total(
                attempt.usage.total_tokens for attempt in self.attempts
            ),
            wall_time_ms=sum(attempt.usage.wall_time_ms for attempt in self.attempts),
        )

    def to_json_object(self) -> JsonObject:
        """Return a JSON representation suitable for an artifact."""

        return {
            "identity": _identity_json(self.identity),
            "attempts": [attempt.to_json_object() for attempt in self.attempts],
            "estimates": {
                question_id: estimate.to_json_object()
                for question_id, estimate in self.estimates.items()
            },
        }


class DecisionModelAdapter(Protocol):
    """Provider-neutral boundary implemented by decision-model adapters."""

    def decide(
        self,
        state: JsonValue,
        questions: Mapping[str, DecisionQuestion],
        *,
        orders: Sequence[OptionOrder] = COUNTERBALANCED_ORDERS,
    ) -> DecisionModelResult:
        """Return every captured attempt and a combined estimate per question."""

        ...


def _probability(value: JsonValue) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("expected a JSON number")
    probability = float(value)
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise ValueError("expected a probability between zero and one")
    return probability


def _optional_probability(value: JsonValue | None) -> float | None:
    return None if value is None else _probability(value)


def _distribution(
    value: JsonValue,
    keys: Sequence[str],
    tolerance: float,
) -> dict[str, float]:
    if not isinstance(value, dict):
        raise TypeError("expected a JSON object")
    if len(value) != len(keys) or any(key not in value for key in keys):
        raise ValueError("distribution keys do not match the question's options")
    probabilities = {key: _probability(value[key]) for key in keys}
    if abs(math.fsum(probabilities.values()) - 1.0) > tolerance:
        raise ValueError("distribution does not sum to one")
    return probabilities


def _answer(
    question: DecisionQuestion,
    order: OptionOrder,
    value: JsonValue,
    tolerance: float,
) -> DecisionModelAnswer:
    if not isinstance(value, dict):
        raise TypeError("expected a JSON object")
    if value.get("type") != question.question_type.value:
        raise ValueError("answer type does not match the question")
    if isinstance(question, NoulQuestion):
        probability_true = _probability(value["noul"])
        return DecisionModelAnswer(
            question.question_type,
            {"true": probability_true, "false": 1.0 - probability_true},
        )
    confidence = _optional_probability(value.get("confidence"))
    if isinstance(question, ChoiceQuestion):
        probabilities = _distribution(
            value["probabilities"],
            question.option_keys,
            tolerance,
        )
        choice = value["choice"]
        if not isinstance(choice, str) or choice not in probabilities:
            raise ValueError("choice is not one of the question's options")
        if probabilities[choice] + tolerance < max(probabilities.values()):
            raise ValueError("choice is not the most probable option")
        return DecisionModelAnswer(question.question_type, probabilities, confidence)
    if not isinstance(question, ScoreQuestion):
        raise TypeError("unsupported decision question type")

    count = len(question.levels)
    shown = _distribution(value["probabilities"], question.option_keys, tolerance)
    score = value["score"]
    if isinstance(score, bool) or not isinstance(score, int | float):
        raise TypeError("expected a JSON number")
    expected = math.fsum(index * shown[str(index)] for index in range(count))
    if abs(score - expected) > tolerance * (count - 1):
        raise ValueError("score is not the expected level of its distribution")
    if order is OptionOrder.REVERSED:
        shown = {str(index): shown[str(count - 1 - index)] for index in range(count)}
    return DecisionModelAnswer(question.question_type, shown, confidence)


@dataclass(frozen=True, slots=True)
class _ParsedResponse:
    answers: dict[str, DecisionModelAnswer]
    resolved_model: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


def _parsed_response(
    body: bytes,
    questions: Mapping[str, DecisionQuestion],
    order: OptionOrder,
    tolerance: float,
    artifact: ArtifactReference,
) -> _ParsedResponse:
    try:
        envelope = strict_json_loads(body)
        if not isinstance(envelope, dict):
            raise TypeError("expected a JSON object")
        answers = envelope["answers"]
        if not isinstance(answers, dict):
            raise TypeError("expected a JSON object")
    except (KeyError, TypeError, ValueError):
        raise InferenceError(
            InferenceFailure.RESPONSE_ENVELOPE,
            "decision endpoint returned a malformed response envelope",
            artifact=artifact,
        ) from None
    if len(answers) != len(questions) or any(
        question_id not in answers for question_id in questions
    ):
        raise InferenceError(
            InferenceFailure.RESPONSE_ENVELOPE,
            "decision endpoint did not answer exactly the asked questions",
            artifact=artifact,
        )

    parsed: dict[str, DecisionModelAnswer] = {}
    for question_id, question in questions.items():
        try:
            parsed[question_id] = _answer(
                question,
                order,
                answers[question_id],
                tolerance,
            )
        except (KeyError, TypeError, ValueError):
            raise InferenceError(
                InferenceFailure.RESPONSE_SCHEMA,
                f"decision answer {question_id!r} was not a valid "
                f"{question.question_type.value} answer",
                artifact=artifact,
            ) from None

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    usage_value = envelope.get("usage")
    if usage_value is not None:
        try:
            input_tokens, output_tokens, total_tokens = token_counts(usage_value)
        except TypeError:
            raise InferenceError(
                InferenceFailure.RESPONSE_USAGE,
                "decision endpoint returned malformed token usage",
                artifact=artifact,
            ) from None
    resolved_model = envelope.get("model")
    return _ParsedResponse(
        answers=parsed,
        resolved_model=resolved_model if isinstance(resolved_model, str) else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _estimate(
    question: DecisionQuestion,
    answers: Sequence[DecisionModelAnswer],
) -> DecisionModelEstimate:
    keys = question.option_keys
    count = len(answers)
    mean = {
        key: math.fsum(answer.probabilities[key] for answer in answers) / count
        for key in keys
    }
    if count == 1:
        return DecisionModelEstimate(question.question_type, mean, attempt_count=1)
    gap = max(
        abs(first.probabilities[key] - second.probabilities[key])
        for first, second in combinations(answers, 2)
        for key in keys
    )
    return DecisionModelEstimate(
        question.question_type,
        mean,
        attempt_count=count,
        order_gap=gap,
        order_flip=len({answer.most_likely for answer in answers}) > 1,
    )


def _http_failure(status_code: int) -> InferenceFailure:
    """Classify an HTTP status exactly as the Chat Completions client does."""

    if status_code == 401:
        return InferenceFailure.HTTP_AUTHENTICATION
    if status_code == 403:
        return InferenceFailure.HTTP_AUTHORIZATION
    if status_code == 429:
        return InferenceFailure.HTTP_RATE_LIMIT
    if 500 <= status_code <= 599:
        return InferenceFailure.HTTP_SERVER
    return InferenceFailure.HTTP_STATUS


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DecisionModelClient:
    """Ask typed questions of one declared decision-model endpoint."""

    endpoint: DecisionModelEndpoint
    artifact_sink: ArtifactSink
    transport: HttpTransport = field(default_factory=UrllibHttpTransport)
    environment: Mapping[str, str] = field(
        default_factory=lambda: os.environ,
        repr=False,
        compare=False,
    )
    now: Callable[[], datetime] = field(
        default=_utc_now,
        repr=False,
        compare=False,
    )
    monotonic_ns: Callable[[], int] = field(
        default=time.monotonic_ns,
        repr=False,
        compare=False,
    )

    def _request(
        self,
        state: JsonValue,
        questions: Mapping[str, DecisionQuestion],
        order: OptionOrder,
    ) -> HttpRequest:
        try:
            payload = build_decision_payload(
                self.endpoint,
                state,
                questions,
                order=order,
            )
        except ValueError as error:
            raise InferenceError(InferenceFailure.CONFIGURATION, str(error)) from error
        try:
            body = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise InferenceError(
                InferenceFailure.CONFIGURATION,
                "decision request could not be serialized as finite JSON",
            ) from error
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"itself-decisions/{DECISION_MODEL_ADAPTER_VERSION}",
            **self.endpoint.extra_headers,
        }
        if self.endpoint.credential is not None:
            name, value = self.endpoint.credential.render(self.environment)
            headers[name] = value
        return HttpRequest(
            url=self.endpoint.url,
            headers=headers,
            body=body,
            timeout_seconds=self.endpoint.timeout_seconds,
            deadline_seconds=self.endpoint.deadline_seconds,
        )

    def _capture(
        self, content: bytes, media_type: str, detail: str
    ) -> ArtifactReference:
        try:
            return self.artifact_sink.capture(
                content,
                media_type=media_type,
                captured_at=self.now(),
            )
        except (OSError, ValueError) as error:
            raise InferenceError(InferenceFailure.ARTIFACT, detail) from error

    def ask(
        self,
        state: JsonValue,
        questions: Mapping[str, DecisionQuestion],
        *,
        order: OptionOrder = OptionOrder.DECLARED,
    ) -> DecisionModelAttempt:
        """Send one request, capture it and its response, and validate each answer.

        The exact request body is captured before it is sent, and the bounded
        response bytes before they are interpreted.  Nothing is retried.
        """

        try:
            asked = _questions(questions)
            selected_order = _order(order)
        except ValueError as error:
            raise InferenceError(InferenceFailure.CONFIGURATION, str(error)) from error
        http_request = self._request(state, asked, selected_order)
        request_artifact = self._capture(
            http_request.body,
            "application/json",
            "decision request artifact capture failed",
        )
        started_at = self.monotonic_ns()
        try:
            response = self.transport.send(http_request)
        except InferenceError:
            raise
        except Exception as error:
            raise InferenceError(
                InferenceFailure.TRANSPORT,
                "custom decision transport failed",
            ) from error
        elapsed_ns = max(0, self.monotonic_ns() - started_at)
        artifact = self._capture(
            response.body,
            content_type(response.headers),
            "decision response artifact capture failed",
        )

        if not 200 <= response.status_code <= 299:
            raise InferenceError(
                _http_failure(response.status_code),
                f"decision endpoint returned HTTP {response.status_code}",
                status_code=response.status_code,
                artifact=artifact,
            )

        parsed = _parsed_response(
            response.body,
            asked,
            selected_order,
            self.endpoint.probability_tolerance,
            artifact,
        )
        return DecisionModelAttempt(
            order=selected_order,
            identity=InferenceIdentity(
                actor_id=self.endpoint.actor_id,
                adapter_id=DECISION_MODEL_ADAPTER_ID,
                adapter_version=DECISION_MODEL_ADAPTER_VERSION,
                requested_model=self.endpoint.model,
                resolved_model=parsed.resolved_model,
            ),
            request=request_artifact,
            raw_response=artifact,
            usage=InferenceUsage(
                input_tokens=parsed.input_tokens,
                output_tokens=parsed.output_tokens,
                total_tokens=parsed.total_tokens,
                wall_time_ms=elapsed_ns // 1_000_000,
            ),
            answers=parsed.answers,
        )

    def decide(
        self,
        state: JsonValue,
        questions: Mapping[str, DecisionQuestion],
        *,
        orders: Sequence[OptionOrder] = COUNTERBALANCED_ORDERS,
    ) -> DecisionModelResult:
        """Ask every question in each order, one request at a time, and combine.

        Choice and score questions are asked once per order.  A noul question
        has no option order, so only the first request asks it, and a later
        request with nothing left to ask is not sent.  Every attempt's request
        and response stay in the artifact sink even if a later attempt fails.
        """

        try:
            asked = _questions(questions)
            sequence = _orders(orders)
        except ValueError as error:
            raise InferenceError(InferenceFailure.CONFIGURATION, str(error)) from error

        attempts: list[DecisionModelAttempt] = []
        for index, order in enumerate(sequence):
            subset = (
                asked
                if index == 0
                else {
                    question_id: question
                    for question_id, question in asked.items()
                    if not isinstance(question, NoulQuestion)
                }
            )
            if subset:
                attempts.append(self.ask(state, subset, order=order))

        identity = attempts[0].identity
        for attempt in attempts[1:]:
            if attempt.identity != identity:
                raise InferenceError(
                    InferenceFailure.RESPONSE_ENVELOPE,
                    "decision endpoint reported a different model between requests",
                    artifact=attempt.raw_response,
                )
        return DecisionModelResult(
            identity=identity,
            attempts=tuple(attempts),
            estimates={
                question_id: _estimate(
                    question,
                    [
                        attempt.answers[question_id]
                        for attempt in attempts
                        if question_id in attempt.answers
                    ],
                )
                for question_id, question in asked.items()
            },
        )
