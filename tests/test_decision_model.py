# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import copy
import hashlib
import json
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, cast

import pytest

from itself import (
    COUNTERBALANCED_ORDERS,
    DECISION_MODEL_ADAPTER_ID,
    DECISION_MODEL_ADAPTER_VERSION,
    ArtifactReference,
    ChoiceQuestion,
    DecisionModelAdapter,
    DecisionModelAnswer,
    DecisionModelClient,
    DecisionModelEndpoint,
    DecisionModelEstimate,
    DecisionQuestion,
    DecisionQuestionType,
    DirectoryArtifactSink,
    EnvironmentCredential,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    InferenceError,
    InferenceFailure,
    JsonObject,
    JsonValue,
    NoulQuestion,
    OpenAICompatibleEndpoint,
    OptionOrder,
    ScoreQuestion,
    StructuredInferenceClient,
    StructuredOutputProfile,
    build_decision_payload,
    canonical_bundle_json,
)

CAPTURED_AT = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SECRET = "decision-secret-that-must-not-leak"
CREDENTIAL = EnvironmentCredential("TEST_DECISION_KEY")
STATE = (
    "Requirement R1: reject empty input.\n\n"
    "def first_word(text):\n    return text.split()[0]"
)


def _json_body(value: JsonValue) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def _questions() -> dict[str, DecisionQuestion]:
    return {
        "r1": ChoiceQuestion(
            "Does the implementation satisfy rule R1: reject empty input?",
            {
                "true": "Yes. The implementation satisfies R1.",
                "false": "No. The implementation violates R1.",
            },
        ),
        "severity": ScoreQuestion(
            "How severe is the worst violation?",
            ["Minor", "Moderate", "Severe"],
        ),
        "x1": NoulQuestion(
            "Does the implementation raise an error for an empty string?",
            true_description="Yes, it raises an error.",
        ),
    }


def _biased_answer(question: JsonObject) -> JsonObject:
    """Favor whichever option is shown last, like a position-biased model."""

    question_type = question["type"]
    if question_type == "noul":
        return {"type": "noul", "noul": 0.8}
    if question_type == "choice":
        keys = list(cast(JsonObject, question["criteria"]))
        rest = 0.3 / (len(keys) - 1)
        probabilities: JsonObject = {key: rest for key in keys}
        probabilities[keys[-1]] = 0.7
        return {
            "type": "choice",
            "choice": keys[-1],
            "probabilities": probabilities,
            "confidence": 0.4,
        }
    levels = cast(list[JsonValue], question["criteria"])
    values = [0.4 / (len(levels) - 1)] * (len(levels) - 1) + [0.6]
    return {
        "type": "score",
        "score": sum(index * value for index, value in enumerate(values)),
        "probabilities": {str(index): value for index, value in enumerate(values)},
        "legend": {str(index): level for index, level in enumerate(levels)},
        "confidence": 0.3,
    }


def _biased_response(request: HttpRequest, model: str | None) -> JsonObject:
    payload = cast(JsonObject, json.loads(request.body))
    questions = cast(JsonObject, payload["questions"])
    response: JsonObject = {
        "answers": {
            question_id: _biased_answer(cast(JsonObject, question))
            for question_id, question in questions.items()
        },
        "usage": {"input_tokens": 100, "output_tokens": 0},
    }
    if model is not None:
        response["model"] = model
    return response


def _request_list() -> list[HttpRequest]:
    return []


def _model_names() -> list[str | None]:
    return ["example-decider-2b"]


@dataclass(slots=True)
class BiasedServer:
    """Answer like a decision model that favors the last option shown."""

    models: list[str | None] = field(default_factory=_model_names)
    transform: Callable[[JsonObject], bytes] = _json_body
    requests: list[HttpRequest] = field(default_factory=_request_list)

    def send(self, request: HttpRequest, /) -> HttpResponse:
        model = self.models[len(self.requests) % len(self.models)]
        self.requests.append(request)
        return HttpResponse(
            status_code=200,
            headers={"Content-Type": "application/json; charset=utf-8"},
            body=self.transform(_biased_response(request, model)),
        )


@dataclass(slots=True)
class FixedTransport:
    response: HttpResponse
    requests: list[HttpRequest] = field(default_factory=_request_list)

    def send(self, request: HttpRequest, /) -> HttpResponse:
        self.requests.append(request)
        return self.response


@dataclass(slots=True)
class Clock:
    value: int = 0

    def __call__(self) -> int:
        current = self.value
        self.value += 250_000_000
        return current


def _endpoint(
    *,
    base_url: str = "https://decisions.example.test/v1/",
    credential: EnvironmentCredential | None = CREDENTIAL,
    probability_tolerance: float = 1e-6,
    extra_body: Mapping[str, JsonValue] | None = None,
) -> DecisionModelEndpoint:
    return DecisionModelEndpoint(
        actor_id="decision-model-under-test",
        base_url=base_url,
        model="decider-latest",
        credential=credential,
        timeout_seconds=30.0,
        probability_tolerance=probability_tolerance,
        extra_body={} if extra_body is None else extra_body,
    )


def _client(
    tmp_path: Path,
    transport: HttpTransport,
    *,
    endpoint: DecisionModelEndpoint | None = None,
    environment: Mapping[str, str] | None = None,
) -> DecisionModelClient:
    return DecisionModelClient(
        endpoint=_endpoint() if endpoint is None else endpoint,
        artifact_sink=DirectoryArtifactSink(tmp_path / "artifacts"),
        transport=transport,
        environment=(
            {"TEST_DECISION_KEY": SECRET} if environment is None else environment
        ),
        now=lambda: CAPTURED_AT,
        monotonic_ns=Clock(),
    )


def _artifact_bytes(reference: ArtifactReference) -> bytes:
    return Path(reference.uri.removeprefix("file://")).read_bytes()


def _payload(request: HttpRequest) -> JsonObject:
    return cast(JsonObject, json.loads(request.body))


def _criteria(request: HttpRequest, question_id: str) -> JsonValue:
    questions = cast(JsonObject, _payload(request)["questions"])
    return cast(JsonObject, questions[question_id]).get("criteria")


def _listed(payload: JsonObject, question_id: str) -> JsonValue:
    questions = cast(JsonObject, payload["questions"])
    return cast(JsonObject, questions[question_id])["criteria"]


def test_client_is_assignable_to_decision_model_adapter_protocol(
    tmp_path: Path,
) -> None:
    adapter: DecisionModelAdapter = _client(tmp_path, BiasedServer())

    result = adapter.decide(STATE, _questions())

    assert set(result.estimates) == {"r1", "severity", "x1"}


def test_endpoint_renders_the_systemone_url_and_hides_extensions() -> None:
    endpoint = DecisionModelEndpoint(
        actor_id="local-decider",
        base_url="http://127.0.0.1:8000/v1/",
        model="decider-latest",
        allow_insecure_http=True,
        extra_headers={"X-Trace": "trace-7"},
        extra_body={"temperature": 1.0},
    )

    assert endpoint.url == "http://127.0.0.1:8000/v1/systemone"
    assert "X-Trace" not in repr(endpoint)
    assert "temperature" not in repr(endpoint)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"actor_id": "not an identifier"}, "actor_id"),
        ({"base_url": "http://decisions.example.test/v1"}, "must use HTTPS"),
        ({"base_url": "https://user:pw@decisions.example.test"}, "credentials"),
        ({"base_url": "https://decisions.example.test/v1?key=1"}, "query"),
        ({"base_url": "https://decisions.example.test:0/v1"}, "valid port"),
        ({"base_url": "https://decisions.example.test:99999/v1"}, "valid port"),
        ({"base_url": "https://decisions.example.test:api/v1"}, "valid port"),
        ({"model": " decider-latest"}, "model"),
        ({"resource_path": "v1/../admin"}, "resource_path"),
        ({"timeout_seconds": 0.0}, "timeout_seconds"),
        ({"probability_tolerance": -1e-9}, "probability_tolerance"),
        ({"probability_tolerance": 0.02}, "probability_tolerance"),
        ({"probability_tolerance": float("nan")}, "probability_tolerance"),
        ({"probability_tolerance": True}, "probability_tolerance"),
        ({"deadline_seconds": 0.0}, "deadline_seconds"),
        ({"deadline_seconds": float("inf")}, "deadline_seconds"),
        ({"deadline_seconds": True}, "deadline_seconds"),
        ({"extra_headers": {"content-type": "text/plain"}}, "cannot override"),
        ({"extra_headers": {"authorization": "Bearer x"}}, "cannot override"),
        ({"extra_headers": {"X-Trace": "trace\n7"}}, "single lines"),
        ({"extra_headers": {"X-Trace": "caf\u00e9"}}, "printable ASCII"),
        ({"extra_body": {"questions": {}}}, "reserved fields: questions"),
        ({"extra_body": {"state": "x", "model": "y"}}, "reserved fields: model, state"),
        ({"extra_body": {"temperature": float("inf")}}, "finite JSON"),
    ],
)
def test_endpoint_rejects_unsafe_or_ambiguous_configuration(
    changes: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "actor_id": "decision-model",
        "base_url": "https://decisions.example.test/v1",
        "model": "decider-latest",
        "credential": CREDENTIAL,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        DecisionModelEndpoint(**values)  # type: ignore[arg-type]


def test_insecure_http_is_limited_to_explicit_credential_free_loopback() -> None:
    with pytest.raises(ValueError, match="allow_insecure_http=True"):
        DecisionModelEndpoint(
            actor_id="local-decider",
            base_url="http://127.0.0.1:8000/v1",
            model="decider-latest",
        )
    with pytest.raises(ValueError, match="loopback"):
        DecisionModelEndpoint(
            actor_id="remote-decider",
            base_url="http://decisions.example.test:8700/v1",
            model="decider-latest",
            allow_insecure_http=True,
        )


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: ChoiceQuestion("Pick one.", {"only": None}), "between 2"),
        (lambda: ChoiceQuestion("Pick one.", {"a b": None, "c": None}), "keys"),
        (lambda: ChoiceQuestion("Pick one.", {"a": "", "b": None}), "descriptions"),
        (lambda: ChoiceQuestion(" Pick one.", {"a": None, "b": None}), "instructions"),
        (lambda: NoulQuestion(""), "instructions"),
        (lambda: NoulQuestion("True?", false_description=" no"), "false_description"),
        (lambda: ScoreQuestion("Rate it.", ["Only"]), "between 2"),
        (lambda: ScoreQuestion("Rate it.", "Low"), "sequence"),
        (lambda: ScoreQuestion("Rate it.", ["Low", ""]), "score levels"),
    ],
)
def test_questions_reject_malformed_declarations(
    build: Callable[[], object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build()


def test_questions_keep_their_declared_order_immutably() -> None:
    choice = ChoiceQuestion("Route it.", {"billing": "Charges", "technical": None})
    score = ScoreQuestion("Rate it.", ["Calm", "Frustrated", "Very angry"])

    assert choice.option_keys == ("billing", "technical")
    assert score.levels == ("Calm", "Frustrated", "Very angry")
    assert score.option_keys == ("0", "1", "2")
    assert NoulQuestion("True?").option_keys == ("true", "false")
    with pytest.raises(TypeError):
        choice.criteria["billing"] = "changed"  # type: ignore[index]


def test_payload_lists_options_in_the_requested_order() -> None:
    endpoint = _endpoint(extra_body={"temperature": 1.0})
    questions = _questions()

    declared = build_decision_payload(endpoint, STATE, questions)
    reversed_payload = build_decision_payload(
        endpoint,
        STATE,
        questions,
        order=OptionOrder.REVERSED,
    )

    assert list(declared) == ["model", "state", "questions", "temperature"]
    assert declared["model"] == "decider-latest"
    assert declared["state"] == STATE
    declared_questions = cast(JsonObject, declared["questions"])
    reversed_questions = cast(JsonObject, reversed_payload["questions"])
    assert list(cast(JsonObject, _listed(declared, "r1"))) == ["true", "false"]
    assert list(cast(JsonObject, _listed(reversed_payload, "r1"))) == [
        "false",
        "true",
    ]
    assert _listed(reversed_payload, "severity") == ["Severe", "Moderate", "Minor"]
    assert (
        reversed_questions["x1"]
        == declared_questions["x1"]
        == {
            "type": "noul",
            "instructions": (
                "Does the implementation raise an error for an empty string?"
            ),
            "criteria": {"true": "Yes, it raises an error."},
        }
    )


def test_ask_captures_both_sides_and_maps_answers_to_declared_terms(
    tmp_path: Path,
) -> None:
    server = BiasedServer()
    client = _client(tmp_path, server)

    attempt = client.ask(STATE, _questions(), order=OptionOrder.REVERSED)

    assert len(server.requests) == 1
    wire_request = server.requests[0]
    assert wire_request.url == "https://decisions.example.test/v1/systemone"
    assert wire_request.headers["Authorization"] == f"Bearer {SECRET}"
    assert wire_request.headers["User-Agent"] == (
        f"itself-decisions/{DECISION_MODEL_ADAPTER_VERSION}"
    )
    assert wire_request.timeout_seconds == 30.0
    assert _criteria(wire_request, "r1") == {
        "false": "No. The implementation violates R1.",
        "true": "Yes. The implementation satisfies R1.",
    }
    assert list(cast(JsonObject, _criteria(wire_request, "r1"))) == ["false", "true"]

    assert attempt.order is OptionOrder.REVERSED
    assert attempt.identity.actor_id == "decision-model-under-test"
    assert attempt.identity.adapter_id == DECISION_MODEL_ADAPTER_ID
    assert attempt.identity.adapter_version == DECISION_MODEL_ADAPTER_VERSION
    assert attempt.identity.requested_model == "decider-latest"
    assert attempt.identity.resolved_model == "example-decider-2b"
    assert attempt.usage.input_tokens == 100
    assert attempt.usage.output_tokens == 0
    assert attempt.usage.total_tokens is None
    assert attempt.usage.wall_time_ms == 250

    assert _artifact_bytes(attempt.request) == wire_request.body
    assert attempt.request.sha256 == hashlib.sha256(wire_request.body).hexdigest()
    assert attempt.request.media_type == "application/json"
    assert attempt.request.captured_at == "2026-09-30T12:00:00Z"
    raw_response = _artifact_bytes(attempt.raw_response)
    assert attempt.raw_response.sha256 == hashlib.sha256(raw_response).hexdigest()
    assert b"example-decider-2b" in raw_response

    r1 = attempt.answers["r1"]
    assert r1.question_type is DecisionQuestionType.CHOICE
    assert r1.probabilities == {"true": 0.7, "false": pytest.approx(0.3)}
    assert list(r1.probabilities) == ["true", "false"]
    assert r1.most_likely == "true"
    assert r1.reported_confidence == 0.4
    severity = attempt.answers["severity"]
    assert severity.probabilities == pytest.approx({"0": 0.6, "1": 0.2, "2": 0.2})
    assert severity.expected_level == pytest.approx(0.6)
    assert severity.most_likely == "0"
    x1 = attempt.answers["x1"]
    assert x1.probabilities == pytest.approx({"true": 0.8, "false": 0.2})
    assert x1.reported_confidence is None
    assert x1.expected_level is None

    for text in (
        repr(client),
        repr(wire_request),
        repr(attempt),
        wire_request.body.decode(),
        raw_response.decode(),
    ):
        assert SECRET not in text


def test_decide_counterbalances_ordered_questions_and_asks_noul_once(
    tmp_path: Path,
) -> None:
    server = BiasedServer()
    client = _client(tmp_path, server)

    result = client.decide(STATE, _questions())

    assert COUNTERBALANCED_ORDERS == (OptionOrder.DECLARED, OptionOrder.REVERSED)
    assert [attempt.order for attempt in result.attempts] == [
        OptionOrder.DECLARED,
        OptionOrder.REVERSED,
    ]
    first, second = (_payload(request) for request in server.requests)
    assert list(cast(JsonObject, first["questions"])) == ["r1", "severity", "x1"]
    assert list(cast(JsonObject, second["questions"])) == ["r1", "severity"]
    declared_request, reversed_request = server.requests
    assert list(cast(JsonObject, _criteria(declared_request, "r1"))) == [
        "true",
        "false",
    ]
    assert list(cast(JsonObject, _criteria(reversed_request, "r1"))) == [
        "false",
        "true",
    ]

    r1 = result.estimates["r1"]
    assert r1.attempt_count == 2
    assert r1.probabilities == pytest.approx({"true": 0.5, "false": 0.5})
    assert r1.order_gap == pytest.approx(0.4)
    assert r1.order_flip is True
    severity = result.estimates["severity"]
    assert severity.probabilities == pytest.approx({"0": 0.4, "1": 0.2, "2": 0.4})
    assert severity.expected_level == pytest.approx(1.0)
    assert severity.order_gap == pytest.approx(0.4)
    assert severity.order_flip is True
    x1 = result.estimates["x1"]
    assert x1.attempt_count == 1
    assert x1.probabilities == pytest.approx({"true": 0.8, "false": 0.2})
    assert x1.order_gap is None
    assert x1.order_flip is None

    assert result.identity == result.attempts[0].identity
    assert result.usage.input_tokens == 200
    assert result.usage.output_tokens == 0
    assert result.usage.total_tokens is None
    assert result.usage.wall_time_ms == 500
    document = result.to_json_object()
    assert json.loads(canonical_bundle_json(document)) == document
    estimates = cast(JsonObject, document["estimates"])
    assert cast(JsonObject, estimates["x1"])["order_gap"] is None


def test_an_unbiased_model_shows_no_order_effect(tmp_path: Path) -> None:
    def order_free(response: JsonObject) -> bytes:
        answers = cast(JsonObject, response["answers"])
        answers["r1"] = {
            "type": "choice",
            "choice": "false",
            "probabilities": {"true": 0.1, "false": 0.9},
        }
        return _json_body(response)

    client = _client(tmp_path, BiasedServer(transform=order_free))

    result = client.decide(STATE, {"r1": _questions()["r1"]})

    estimate = result.estimates["r1"]
    assert estimate.order_gap == 0.0
    assert estimate.order_flip is False
    assert estimate.most_likely == "false"
    assert result.attempts[0].answers["r1"].reported_confidence is None


def test_decide_sends_one_request_when_only_noul_questions_are_asked(
    tmp_path: Path,
) -> None:
    server = BiasedServer()

    result = _client(tmp_path, server).decide(STATE, {"x1": _questions()["x1"]})

    assert len(server.requests) == 1
    assert len(result.attempts) == 1
    assert result.estimates["x1"].order_gap is None


def test_decide_with_one_order_reports_no_order_effect(tmp_path: Path) -> None:
    server = BiasedServer()

    result = _client(tmp_path, server).decide(
        STATE,
        _questions(),
        orders=(OptionOrder.DECLARED,),
    )

    assert len(server.requests) == 1
    assert result.estimates["r1"].order_gap is None
    assert result.estimates["r1"].probabilities == pytest.approx(
        {"true": 0.3, "false": 0.7}
    )


@pytest.mark.parametrize(
    "orders",
    [
        (),
        (OptionOrder.DECLARED, OptionOrder.DECLARED),
        "declared",
        ["declared"],
    ],
)
def test_decide_rejects_invalid_orders_before_sending(
    tmp_path: Path,
    orders: object,
) -> None:
    server = BiasedServer()

    with pytest.raises(InferenceError) as error:
        _client(tmp_path, server).decide(STATE, _questions(), orders=orders)  # type: ignore[arg-type]

    assert error.value.failure is InferenceFailure.CONFIGURATION
    assert server.requests == []


@pytest.mark.parametrize(
    ("state", "questions"),
    [
        ("   ", None),
        ({}, None),
        (3, None),
        (STATE, {}),
        (STATE, {"not an id": NoulQuestion("True?")}),
        (STATE, {"x1": {"type": "noul", "instructions": "True?"}}),
        ({"value": float("nan")}, None),
    ],
)
def test_ask_rejects_invalid_requests_before_sending(
    tmp_path: Path,
    state: JsonValue,
    questions: dict[str, object] | None,
) -> None:
    server = BiasedServer()
    asked = _questions() if questions is None else questions

    with pytest.raises(InferenceError) as error:
        _client(tmp_path, server).ask(state, asked)  # type: ignore[arg-type]

    assert error.value.failure is InferenceFailure.CONFIGURATION
    assert server.requests == []
    assert not (tmp_path / "artifacts").exists()


def test_decide_rejects_a_model_change_between_requests(tmp_path: Path) -> None:
    server = BiasedServer(models=["example-decider-2b", "example-decider-1b"])

    with pytest.raises(InferenceError) as error:
        _client(tmp_path, server).decide(STATE, _questions())

    assert error.value.failure is InferenceFailure.RESPONSE_ENVELOPE
    assert "different model" in str(error.value)
    assert error.value.artifact is not None
    assert len(server.requests) == 2


def _edited(response: JsonObject, path: tuple[str, ...], value: JsonValue) -> bytes:
    edited = copy.deepcopy(response)
    target = edited
    for key in path[:-1]:
        target = cast(JsonObject, target[key])
    target[path[-1]] = value
    return _json_body(edited)


def _removed(response: JsonObject, path: tuple[str, ...]) -> bytes:
    edited = copy.deepcopy(response)
    target = edited
    for key in path[:-1]:
        target = cast(JsonObject, target[key])
    del target[path[-1]]
    return _json_body(edited)


MALFORMED_RESPONSES: dict[
    str, tuple[Callable[[JsonObject], bytes], InferenceFailure]
] = {
    "not-json": (lambda _: b"not json", InferenceFailure.RESPONSE_ENVELOPE),
    "duplicate-key": (
        lambda _: b'{"answers":{},"answers":{}}',
        InferenceFailure.RESPONSE_ENVELOPE,
    ),
    "array-envelope": (lambda _: b"[]", InferenceFailure.RESPONSE_ENVELOPE),
    "missing-answers": (
        lambda response: _removed(response, ("answers",)),
        InferenceFailure.RESPONSE_ENVELOPE,
    ),
    "missing-question": (
        lambda response: _removed(response, ("answers", "x1")),
        InferenceFailure.RESPONSE_ENVELOPE,
    ),
    "extra-question": (
        lambda response: _edited(
            response, ("answers", "x2"), {"type": "noul", "noul": 0.5}
        ),
        InferenceFailure.RESPONSE_ENVELOPE,
    ),
    "wrong-type": (
        lambda response: _edited(response, ("answers", "x1", "type"), "choice"),
        InferenceFailure.RESPONSE_SCHEMA,
    ),
    "noul-out-of-range": (
        lambda response: _edited(response, ("answers", "x1", "noul"), 1.2),
        InferenceFailure.RESPONSE_SCHEMA,
    ),
    "noul-boolean": (
        lambda response: _edited(response, ("answers", "x1", "noul"), True),
        InferenceFailure.RESPONSE_SCHEMA,
    ),
    "choice-missing-option": (
        lambda response: _removed(response, ("answers", "r1", "probabilities", "true")),
        InferenceFailure.RESPONSE_SCHEMA,
    ),
    "choice-unnormalized": (
        lambda response: _edited(
            response,
            ("answers", "r1", "probabilities"),
            {"true": 0.2, "false": 0.7},
        ),
        InferenceFailure.RESPONSE_SCHEMA,
    ),
    "choice-not-most-probable": (
        lambda response: _edited(response, ("answers", "r1", "choice"), "true"),
        InferenceFailure.RESPONSE_SCHEMA,
    ),
    "choice-unknown": (
        lambda response: _edited(response, ("answers", "r1", "choice"), "maybe"),
        InferenceFailure.RESPONSE_SCHEMA,
    ),
    "score-inconsistent": (
        lambda response: _edited(response, ("answers", "severity", "score"), 0.0),
        InferenceFailure.RESPONSE_SCHEMA,
    ),
    "confidence-out-of-range": (
        lambda response: _edited(response, ("answers", "r1", "confidence"), 1.5),
        InferenceFailure.RESPONSE_SCHEMA,
    ),
    "negative-usage": (
        lambda response: _edited(response, ("usage", "input_tokens"), -1),
        InferenceFailure.RESPONSE_USAGE,
    ),
    "usage-not-object": (
        lambda response: _edited(response, ("usage",), []),
        InferenceFailure.RESPONSE_USAGE,
    ),
}


@pytest.mark.parametrize("case", sorted(MALFORMED_RESPONSES))
def test_malformed_answers_fail_closed_with_the_captured_response(
    tmp_path: Path,
    case: str,
) -> None:
    transform, failure = MALFORMED_RESPONSES[case]
    server = BiasedServer(transform=transform)

    with pytest.raises(InferenceError) as error:
        _client(tmp_path, server).ask(STATE, _questions())

    assert error.value.failure is failure
    assert not error.value.retryable
    assert error.value.artifact is not None
    served = transform(_biased_response(server.requests[0], "example-decider-2b"))
    assert _artifact_bytes(error.value.artifact) == served
    assert SECRET not in str(error.value)


def test_tolerance_accepts_rounded_distributions_only_when_declared(
    tmp_path: Path,
) -> None:
    def rounded(response: JsonObject) -> bytes:
        return _edited(
            response,
            ("answers", "r1", "probabilities"),
            {"true": 0.3004, "false": 0.7},
        )

    question = {"r1": _questions()["r1"]}
    with pytest.raises(InferenceError) as error:
        _client(tmp_path, BiasedServer(transform=rounded)).ask(STATE, question)
    assert error.value.failure is InferenceFailure.RESPONSE_SCHEMA

    lenient = _client(
        tmp_path,
        BiasedServer(transform=rounded),
        endpoint=_endpoint(probability_tolerance=0.001),
    )
    attempt = lenient.ask(STATE, question)
    assert attempt.answers["r1"].probabilities["true"] == 0.3004


def _chat_profile() -> StructuredOutputProfile:
    return StructuredOutputProfile(
        profile_id="answer-v1",
        schema_name="answer_v1",
        system_prompt="Return only the requested JSON object.",
        user_instruction="Answer the supplied question.",
        input_label="QUESTION",
        schema={"type": "object"},
    )


@pytest.mark.parametrize(
    "status_code",
    [301, 400, 401, 403, 404, 408, 409, 422, 429, 500, 502, 503, 529],
)
def test_http_failures_are_classified_like_the_chat_client(
    tmp_path: Path,
    status_code: int,
) -> None:
    response = HttpResponse(
        status_code=status_code,
        headers={"Content-Type": "application/json"},
        body=b'{"detail":"Service unavailable."}',
    )
    decision_client = _client(tmp_path / "decision", FixedTransport(response))
    chat_client = StructuredInferenceClient(
        endpoint=OpenAICompatibleEndpoint(
            actor_id="chat-model",
            base_url="https://models.example.test/v1",
            model="org/model-1",
        ),
        artifact_sink=DirectoryArtifactSink(tmp_path / "chat" / "artifacts"),
        transport=FixedTransport(response),
        environment={},
        now=lambda: CAPTURED_AT,
        monotonic_ns=Clock(),
    )

    with pytest.raises(InferenceError) as decision_error:
        decision_client.ask(STATE, _questions())
    with pytest.raises(InferenceError) as chat_error:
        chat_client.invoke({"question": "?"}, _chat_profile())

    assert decision_error.value.failure is chat_error.value.failure
    assert decision_error.value.retryable is chat_error.value.retryable
    assert decision_error.value.status_code == status_code
    assert decision_error.value.artifact is not None
    assert _artifact_bytes(decision_error.value.artifact) == response.body


def test_transport_exceptions_are_sanitized_after_the_request_is_captured(
    tmp_path: Path,
) -> None:
    @dataclass(slots=True)
    class FailingTransport:
        requests: list[HttpRequest] = field(default_factory=_request_list)

        def send(self, request: HttpRequest, /) -> NoReturn:
            self.requests.append(request)
            raise RuntimeError(f"socket closed while sending {SECRET}")

    transport = FailingTransport()

    with pytest.raises(InferenceError) as error:
        _client(tmp_path, transport).ask(STATE, _questions())

    assert error.value.failure is InferenceFailure.TRANSPORT
    assert error.value.retryable
    assert SECRET not in str(error.value)
    captured = list((tmp_path / "artifacts").glob("sha256-*.json"))
    assert [path.read_bytes() for path in captured] == [transport.requests[0].body]


def test_request_capture_failure_prevents_sending(tmp_path: Path) -> None:
    @dataclass(slots=True)
    class RefusingSink:
        def capture(
            self,
            content: bytes,
            /,
            *,
            media_type: str,
            captured_at: datetime,
        ) -> ArtifactReference:
            raise OSError("disk full")

    server = BiasedServer()
    client = DecisionModelClient(
        endpoint=_endpoint(),
        artifact_sink=RefusingSink(),
        transport=server,
        environment={"TEST_DECISION_KEY": SECRET},
    )

    with pytest.raises(InferenceError) as error:
        client.ask(STATE, _questions())

    assert error.value.failure is InferenceFailure.ARTIFACT
    assert server.requests == []


def test_missing_credential_fails_before_capture_or_sending(tmp_path: Path) -> None:
    server = BiasedServer()

    with pytest.raises(InferenceError) as error:
        _client(tmp_path, server, environment={}).ask(STATE, _questions())

    assert error.value.failure is InferenceFailure.CONFIGURATION
    assert server.requests == []
    assert not (tmp_path / "artifacts").exists()


@pytest.mark.parametrize(
    "secret",
    [f"{SECRET}\n", f"{SECRET}\r\n X-Injected: yes", f"{SECRET}\u2019"],
    ids=["newline", "folded", "non-ascii"],
)
def test_malformed_credential_fails_before_capture_without_echoing_it(
    tmp_path: Path,
    secret: str,
) -> None:
    server = BiasedServer()
    client = _client(tmp_path, server, environment={"TEST_DECISION_KEY": secret})

    with pytest.raises(InferenceError) as error:
        client.ask(STATE, _questions())

    assert error.value.failure is InferenceFailure.CONFIGURATION
    assert not error.value.retryable
    assert server.requests == []
    assert not (tmp_path / "artifacts").exists()
    assert SECRET not in "".join(traceback.format_exception(error.value))


def test_answers_and_estimates_break_ties_toward_the_first_declared_option() -> None:
    answer = DecisionModelAnswer(
        DecisionQuestionType.CHOICE,
        {"billing": 0.5, "technical": 0.5},
    )
    estimate = DecisionModelEstimate(
        DecisionQuestionType.NOUL,
        {"true": 0.5, "false": 0.5},
        attempt_count=1,
    )

    assert answer.most_likely == "billing"
    assert answer.expected_level is None
    assert estimate.most_likely == "true"
    with pytest.raises(ValueError, match="attempt_count"):
        DecisionModelEstimate(
            DecisionQuestionType.NOUL,
            {"true": 1.0, "false": 0.0},
            attempt_count=0,
        )


def test_endpoint_deadline_reaches_every_request(tmp_path: Path) -> None:
    server = BiasedServer()
    endpoint = DecisionModelEndpoint(
        actor_id="decision-model-under-test",
        base_url="https://decisions.example.test/v1",
        model="decider-latest",
        credential=CREDENTIAL,
        deadline_seconds=3.0,
    )

    _client(tmp_path, server, endpoint=endpoint).decide(STATE, _questions())

    assert [request.deadline_seconds for request in server.requests] == [3.0, 3.0]
    assert _endpoint().deadline_seconds is None
