# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Typed reference boundary for replaceable causal diagnosticians."""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol, cast

from jsonschema import Draft202012Validator, FormatChecker

from experiments._contract_values import array_value as _array
from experiments._contract_values import integer_value as _integer
from experiments._contract_values import number_value as _number
from experiments._contract_values import object_value as _object
from experiments._contract_values import string_value as _string
from experiments._schema_validation import SchemaValidator as _SchemaValidator
from experiments._schema_validation import schema_errors as _schema_errors
from itself import ActorType, JsonObject, JsonValue

CONTRACT_VERSION: Final = "0.2.0"
_CONTRACT_ROOT: Final = Path(__file__).resolve().parent / "contracts" / "v1"


def _json_objects(values: Sequence[JsonObject]) -> list[JsonValue]:
    return [value for value in values]


def _load_validator(schema_name: str) -> _SchemaValidator:
    value = cast(
        JsonValue,
        json.loads((_CONTRACT_ROOT / schema_name).read_text(encoding="utf-8")),
    )
    if not isinstance(value, dict):
        raise TypeError(f"contract schema {schema_name} must contain a JSON object")
    Draft202012Validator.check_schema(value)
    return cast(
        _SchemaValidator,
        Draft202012Validator(value, format_checker=FormatChecker()),
    )


def _duplicate_values(values: Sequence[str]) -> tuple[str, ...]:
    counts = Counter(values)
    return tuple(sorted(value for value, count in counts.items() if count > 1))


@dataclass(frozen=True, slots=True)
class DiagnosisObservation:
    """One diagnostician-visible observation with opaque JSON details."""

    id: str
    result: Mapping[str, JsonValue]
    summary: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "result",
            MappingProxyType(deepcopy(dict(self.result))),
        )

    def to_json_object(self) -> JsonObject:
        """Return a defensive JSON-compatible wire representation."""

        value: JsonObject = {
            "id": self.id,
            "result": deepcopy(dict(self.result)),
        }
        if self.summary is not None:
            value["summary"] = self.summary
        return value


@dataclass(frozen=True, slots=True)
class HypothesisOption:
    """A closed-set causal explanation available to a diagnostician."""

    id: str
    statement: str

    def to_json_object(self) -> JsonObject:
        return {"id": self.id, "statement": self.statement}


@dataclass(frozen=True, slots=True)
class DiagnosticTestOption:
    """An opaque controlled action the runner, not the diagnostician, executes."""

    id: str
    question: str
    estimated_cost_units: int

    def to_json_object(self) -> JsonObject:
        return {
            "id": self.id,
            "question": self.question,
            "estimated_cost_units": self.estimated_cost_units,
        }


@dataclass(frozen=True, slots=True)
class DiagnosisRequest:
    """Provider-neutral information authorized for a diagnostician."""

    case_id: str
    task: str
    workflow_components: tuple[str, ...]
    observations: tuple[DiagnosisObservation, ...]
    hypothesis_options: tuple[HypothesisOption, ...]
    test_options: tuple[DiagnosticTestOption, ...]
    contract_version: str = CONTRACT_VERSION

    def to_json_object(self) -> JsonObject:
        return {
            "contract_version": self.contract_version,
            "case_id": self.case_id,
            "task": self.task,
            "workflow_components": list(self.workflow_components),
            "observations": _json_objects(
                tuple(observation.to_json_object() for observation in self.observations)
            ),
            "hypothesis_options": _json_objects(
                tuple(option.to_json_object() for option in self.hypothesis_options)
            ),
            "test_options": _json_objects(
                tuple(option.to_json_object() for option in self.test_options)
            ),
        }


@dataclass(frozen=True, slots=True)
class DiagnosticianIdentity:
    """Provider-neutral identity attached to one diagnostician result."""

    actor_id: str
    actor_type: ActorType
    adapter_id: str
    adapter_version: str

    def to_json_object(self) -> JsonObject:
        return {
            "actor_id": self.actor_id,
            "actor_type": self.actor_type.value,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
        }


@dataclass(frozen=True, slots=True)
class DiagnosticPrediction:
    """A falsifiable outcome declared before test execution."""

    hypothesis_id: str
    test_id: str
    expected_observation: str
    falsified_when: str

    def to_json_object(self) -> JsonObject:
        return {
            "hypothesis_id": self.hypothesis_id,
            "test_id": self.test_id,
            "expected_observation": self.expected_observation,
            "falsified_when": self.falsified_when,
        }


@dataclass(frozen=True, slots=True)
class DiagnosisAssertion:
    """A diagnostician assertion with no evidentiary or verdict authority."""

    retained_hypothesis_ids: tuple[str, ...]
    primary_hypothesis_id: str
    predictions: tuple[DiagnosticPrediction, ...]
    selected_test_id: str
    public_explanation: str
    expressed_confidence: float | None = None

    def to_json_object(self) -> JsonObject:
        value: JsonObject = {
            "retained_hypothesis_ids": list(self.retained_hypothesis_ids),
            "primary_hypothesis_id": self.primary_hypothesis_id,
            "predictions": _json_objects(
                tuple(prediction.to_json_object() for prediction in self.predictions)
            ),
            "selected_test_id": self.selected_test_id,
            "public_explanation": self.public_explanation,
        }
        if self.expressed_confidence is not None:
            value["expressed_confidence"] = self.expressed_confidence
        return value

    def to_model_json_object(self) -> JsonObject:
        """Return the strict model-output representation, including nullable fields."""

        value = self.to_json_object()
        value["expressed_confidence"] = self.expressed_confidence
        return value


def _decode_assertion(value: JsonObject) -> DiagnosisAssertion:
    confidence_value = value.get("expressed_confidence")
    confidence = None if confidence_value is None else _number(confidence_value)
    return DiagnosisAssertion(
        retained_hypothesis_ids=tuple(
            _string(item) for item in _array(value["retained_hypothesis_ids"])
        ),
        primary_hypothesis_id=_string(value["primary_hypothesis_id"]),
        predictions=tuple(
            DiagnosticPrediction(
                hypothesis_id=_string(prediction["hypothesis_id"]),
                test_id=_string(prediction["test_id"]),
                expected_observation=_string(prediction["expected_observation"]),
                falsified_when=_string(prediction["falsified_when"]),
            )
            for prediction in (_object(item) for item in _array(value["predictions"]))
        ),
        selected_test_id=_string(value["selected_test_id"]),
        public_explanation=_string(value["public_explanation"]),
        expressed_confidence=confidence,
    )


@dataclass(frozen=True, slots=True)
class RawOutputArtifact:
    """Reference to captured diagnostician output; never evidence by itself."""

    uri: str
    media_type: str
    sha256: str
    captured_at: str

    def to_json_object(self) -> JsonObject:
        return {
            "uri": self.uri,
            "media_type": self.media_type,
            "digest": {
                "algorithm": "sha256",
                "value": self.sha256,
            },
            "captured_at": self.captured_at,
        }


@dataclass(frozen=True, slots=True)
class DiagnosisUsage:
    """Non-negative invocation costs reported without a composite score."""

    model_input_tokens: int = 0
    model_output_tokens: int = 0
    wall_time_ms: int = 0
    human_minutes: float = 0.0

    @property
    def model_tokens(self) -> int:
        return self.model_input_tokens + self.model_output_tokens

    def to_json_object(self) -> JsonObject:
        return {
            "model_input_tokens": self.model_input_tokens,
            "model_output_tokens": self.model_output_tokens,
            "wall_time_ms": self.wall_time_ms,
            "human_minutes": self.human_minutes,
        }


@dataclass(frozen=True, slots=True)
class DiagnosisResult:
    """Validated assertion, identity, provenance reference, and invocation cost."""

    diagnostician: DiagnosticianIdentity
    assertion: DiagnosisAssertion
    raw_output_artifact: RawOutputArtifact
    usage: DiagnosisUsage
    contract_version: str = CONTRACT_VERSION

    def to_json_object(self) -> JsonObject:
        return {
            "contract_version": self.contract_version,
            "diagnostician": self.diagnostician.to_json_object(),
            "assertion": self.assertion.to_json_object(),
            "raw_output_artifact": self.raw_output_artifact.to_json_object(),
            "usage": self.usage.to_json_object(),
        }


class Diagnostician(Protocol):
    """Minimal callable boundary implemented by software, humans, or models."""

    def diagnose(self, request: DiagnosisRequest, /) -> DiagnosisResult:
        """Return a non-authoritative structured assertion."""

        ...


@dataclass(frozen=True, slots=True)
class SuppliedResultDiagnostician:
    """Offline adapter returning one externally supplied structured result."""

    result: DiagnosisResult

    def diagnose(self, request: DiagnosisRequest, /) -> DiagnosisResult:
        """Return the supplied result; invocation validation binds it to request."""

        del request
        return self.result


class DiagnosisContractError(ValueError):
    """Raised when a request or result violates the diagnostician contract."""

    def __init__(self, issues: Sequence[str]) -> None:
        self.issues: tuple[str, ...] = tuple(issues)
        super().__init__("\n".join(self.issues))


class DiagnosticianContractValidator:
    """Validate wire shape and cross-reference semantics for one invocation."""

    def __init__(self) -> None:
        self._request_validator = _load_validator("diagnosis-request.schema.json")
        self._assertion_validator = _load_validator("diagnosis-assertion.schema.json")
        self._result_validator = _load_validator("diagnosis-result.schema.json")

    def request_errors(self, request: DiagnosisRequest) -> list[str]:
        """Return deterministic request-shape and identifier-integrity issues."""

        issues = _schema_errors(self._request_validator, request.to_json_object())
        for field, values in (
            ("observations", tuple(item.id for item in request.observations)),
            (
                "hypothesis_options",
                tuple(item.id for item in request.hypothesis_options),
            ),
            ("test_options", tuple(item.id for item in request.test_options)),
        ):
            for duplicate in _duplicate_values(values):
                issues.append(f"$.{field}: duplicate id {duplicate!r}")
        return issues

    def validate_request(self, request: DiagnosisRequest) -> None:
        """Raise DiagnosisContractError if a request is not conformant."""

        issues = self.request_errors(request)
        if issues:
            raise DiagnosisContractError(issues)

    def result_errors(
        self,
        request: DiagnosisRequest,
        result: DiagnosisResult,
    ) -> list[str]:
        """Return result-shape and cross-reference issues for a request."""

        issues = _schema_errors(self._result_validator, result.to_json_object())
        issues.extend(self._assertion_errors(request, result.assertion, "$.assertion"))
        return issues

    def _assertion_errors(
        self,
        request: DiagnosisRequest,
        assertion: DiagnosisAssertion,
        path: str,
    ) -> list[str]:
        """Return request-relative semantic issues for one decoded assertion."""

        issues: list[str] = []
        hypothesis_ids = {option.id for option in request.hypothesis_options}
        test_ids = {option.id for option in request.test_options}
        retained_ids = set(assertion.retained_hypothesis_ids)

        for reference in sorted(retained_ids - hypothesis_ids):
            issues.append(
                f"{path}.retained_hypothesis_ids: unknown hypothesis id {reference!r}"
            )
        if assertion.primary_hypothesis_id not in retained_ids:
            issues.append(
                f"{path}.primary_hypothesis_id: primary hypothesis is not retained"
            )
        if assertion.selected_test_id not in test_ids:
            issues.append(
                f"{path}.selected_test_id: "
                f"unknown test id {assertion.selected_test_id!r}"
            )

        prediction_pairs = tuple(
            (prediction.hypothesis_id, prediction.test_id)
            for prediction in assertion.predictions
        )
        for duplicate in _duplicate_values(
            tuple(
                f"{hypothesis_id}\0{test_id}"
                for hypothesis_id, test_id in prediction_pairs
            )
        ):
            hypothesis_id, test_id = duplicate.split("\0", maxsplit=1)
            issues.append(
                f"{path}.predictions: duplicate prediction for "
                f"hypothesis {hypothesis_id!r} and test {test_id!r}"
            )

        for position, prediction in enumerate(assertion.predictions):
            if prediction.hypothesis_id not in retained_ids:
                issues.append(
                    f"{path}.predictions[{position}].hypothesis_id: "
                    "prediction hypothesis is not retained"
                )
            if prediction.test_id not in test_ids:
                issues.append(
                    f"{path}.predictions[{position}].test_id: "
                    f"unknown test id {prediction.test_id!r}"
                )

        selected_prediction_counts = Counter(
            prediction.hypothesis_id
            for prediction in assertion.predictions
            if prediction.test_id == assertion.selected_test_id
        )
        for hypothesis_id in assertion.retained_hypothesis_ids:
            if selected_prediction_counts[hypothesis_id] != 1:
                issues.append(
                    f"{path}.predictions: retained hypothesis "
                    f"{hypothesis_id!r} must have exactly one prediction for selected "
                    f"test {assertion.selected_test_id!r}"
                )

        confidence = assertion.expressed_confidence
        if confidence is not None and not math.isfinite(confidence):
            issues.append(f"{path}.expressed_confidence: value must be finite")
        return issues

    def decode_assertion(
        self,
        request: DiagnosisRequest,
        value: JsonValue,
    ) -> DiagnosisAssertion:
        """Decode an untrusted model assertion and bind it to a request."""

        self.validate_request(request)
        issues = _schema_errors(self._assertion_validator, value)
        if issues:
            raise DiagnosisContractError(issues)

        assertion = _decode_assertion(_object(value))
        issues = self._assertion_errors(request, assertion, "$")
        if issues:
            raise DiagnosisContractError(issues)
        return assertion

    def validate_result(
        self,
        request: DiagnosisRequest,
        result: DiagnosisResult,
    ) -> None:
        """Raise DiagnosisContractError if a result is not conformant."""

        issues = self.result_errors(request, result)
        if issues:
            raise DiagnosisContractError(issues)

    def decode_result(
        self,
        request: DiagnosisRequest,
        value: JsonValue,
    ) -> DiagnosisResult:
        """Decode external JSON only after shape validation, then bind to request."""

        self.validate_request(request)
        issues = _schema_errors(self._result_validator, value)
        if issues:
            raise DiagnosisContractError(issues)

        root = _object(value)
        identity_value = _object(root["diagnostician"])
        assertion_value = _object(root["assertion"])
        artifact_value = _object(root["raw_output_artifact"])
        digest_value = _object(artifact_value["digest"])
        usage_value = _object(root["usage"])

        result = DiagnosisResult(
            contract_version=_string(root["contract_version"]),
            diagnostician=DiagnosticianIdentity(
                actor_id=_string(identity_value["actor_id"]),
                actor_type=ActorType(_string(identity_value["actor_type"])),
                adapter_id=_string(identity_value["adapter_id"]),
                adapter_version=_string(identity_value["adapter_version"]),
            ),
            assertion=_decode_assertion(assertion_value),
            raw_output_artifact=RawOutputArtifact(
                uri=_string(artifact_value["uri"]),
                media_type=_string(artifact_value["media_type"]),
                sha256=_string(digest_value["value"]),
                captured_at=_string(artifact_value["captured_at"]),
            ),
            usage=DiagnosisUsage(
                model_input_tokens=_integer(usage_value["model_input_tokens"]),
                model_output_tokens=_integer(usage_value["model_output_tokens"]),
                wall_time_ms=_integer(usage_value["wall_time_ms"]),
                human_minutes=_number(usage_value["human_minutes"]),
            ),
        )
        self.validate_result(request, result)
        return result

    def invoke(
        self,
        diagnostician: Diagnostician,
        request: DiagnosisRequest,
    ) -> DiagnosisResult:
        """Validate a request, invoke an adapter, then validate its result."""

        self.validate_request(request)
        result = diagnostician.diagnose(request)
        self.validate_result(request, result)
        return result
