# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Minimal narrative-diagnosis contract for comparative condition A."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, cast

from jsonschema import Draft202012Validator, FormatChecker

from experiments._contract_values import integer_value as _integer
from experiments._contract_values import number_value as _number
from experiments._contract_values import object_value as _object
from experiments._contract_values import string_value as _string
from experiments._schema_validation import SchemaValidator as _SchemaValidator
from experiments._schema_validation import schema_errors as _schema_errors
from experiments.diagnostician import (
    DiagnosisRequest,
    DiagnosisUsage,
    DiagnosticianContractValidator,
    DiagnosticianIdentity,
    RawOutputArtifact,
)
from experiments.model_adapters import (
    ModelAdapterError,
    ModelAdapterFailure,
    StructuredModelOutput,
    StructuredOutputProfile,
)
from itself import ActorType, JsonObject, JsonValue

NARRATIVE_CONTRACT_VERSION: Final = "0.2.0"
NARRATIVE_PROFILE_ID: Final = "narrative-assertion-v0.2.0"
NARRATIVE_SCHEMA_NAME: Final = "itself_narrative_assertion_v0_2_0"

_CONTRACT_ROOT: Final = Path(__file__).resolve().parent / "contracts" / "v1"
_ASSERTION_SCHEMA_PATH: Final = _CONTRACT_ROOT / "narrative-assertion.schema.json"
_RESULT_SCHEMA_PATH: Final = _CONTRACT_ROOT / "narrative-result.schema.json"
_SYSTEM_PROMPT: Final = """You are diagnosing the cause of a failed workflow.

Return only the requested minimally structured narrative assertion. Select exactly one hypothesis identifier supplied in the request and give a concise public explanation for that selection.

Do not retain or rank alternatives, make predictions, select a test, claim that a test ran, emit evidence or a verdict, assign an epistemic state, or provide private chain-of-thought."""


class StructuredModelInvoker(Protocol):
    """Provider-neutral strict-output invocation surface used by this profile."""

    def invoke(
        self,
        input_value: JsonValue,
        profile: StructuredOutputProfile,
    ) -> StructuredModelOutput: ...


def _load_schema(path: Path, *, provider_facing: bool = False) -> JsonObject:
    value = cast(JsonValue, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise TypeError(f"contract schema {path.name} must contain a JSON object")
    Draft202012Validator.check_schema(value)
    schema = dict(value)
    if provider_facing:
        for annotation in ("$schema", "$id", "title"):
            schema.pop(annotation, None)
    return schema


def _validator(path: Path) -> _SchemaValidator:
    return cast(
        _SchemaValidator,
        Draft202012Validator(
            _load_schema(path),
            format_checker=FormatChecker(),
        ),
    )


def narrative_assertion_schema() -> JsonObject:
    """Return the strict provider-facing Condition-A assertion schema."""

    return _load_schema(_ASSERTION_SCHEMA_PATH, provider_facing=True)


def narrative_output_profile() -> StructuredOutputProfile:
    """Return the strict-output instructions for a narrative diagnosis."""

    return StructuredOutputProfile(
        profile_id=NARRATIVE_PROFILE_ID,
        schema_name=NARRATIVE_SCHEMA_NAME,
        system_prompt=_SYSTEM_PROMPT,
        user_instruction=(
            "Select one causal diagnosis for the following request. Respond only "
            "with JSON matching the supplied schema."
        ),
        input_label="DIAGNOSIS REQUEST",
        schema=narrative_assertion_schema(),
    )


@dataclass(frozen=True, slots=True)
class NarrativeAssertion:
    """One narrative diagnosis selection with no test or verdict authority."""

    selected_hypothesis_id: str
    public_explanation: str
    expressed_confidence: float | None = None

    def to_json_object(self) -> JsonObject:
        """Return the strict wire representation, including nullable confidence."""

        return {
            "selected_hypothesis_id": self.selected_hypothesis_id,
            "public_explanation": self.public_explanation,
            "expressed_confidence": self.expressed_confidence,
        }


@dataclass(frozen=True, slots=True)
class NarrativeResult:
    """Validated narrative assertion plus adapter-owned provenance and cost."""

    diagnostician: DiagnosticianIdentity
    assertion: NarrativeAssertion
    raw_output_artifact: RawOutputArtifact
    usage: DiagnosisUsage
    contract_version: str = NARRATIVE_CONTRACT_VERSION

    def to_json_object(self) -> JsonObject:
        """Return the complete language-neutral result representation."""

        return {
            "contract_version": self.contract_version,
            "diagnostician": self.diagnostician.to_json_object(),
            "assertion": self.assertion.to_json_object(),
            "raw_output_artifact": self.raw_output_artifact.to_json_object(),
            "usage": self.usage.to_json_object(),
        }


class NarrativeDiagnostician(Protocol):
    """Minimal callable boundary for comparative narrative condition A."""

    def diagnose(self, request: DiagnosisRequest, /) -> NarrativeResult: ...


class NarrativeContractError(ValueError):
    """Raised when a narrative assertion or result violates its contract."""

    def __init__(self, issues: Sequence[str]) -> None:
        self.issues: tuple[str, ...] = tuple(issues)
        super().__init__("\n".join(self.issues))


class NarrativeContractValidator:
    """Validate narrative shape and bind its selected identifier to a request."""

    def __init__(self) -> None:
        self._assertion_validator = _validator(_ASSERTION_SCHEMA_PATH)
        self._result_validator = _validator(_RESULT_SCHEMA_PATH)

    @staticmethod
    def validate_request(request: DiagnosisRequest) -> None:
        """Apply the common diagnostician request contract."""

        DiagnosticianContractValidator().validate_request(request)

    def assertion_errors(
        self,
        request: DiagnosisRequest,
        assertion: NarrativeAssertion,
        *,
        path: str = "$",
    ) -> list[str]:
        """Return shape and request-relative errors for one assertion."""

        issues = _schema_errors(
            self._assertion_validator,
            assertion.to_json_object(),
        )
        hypothesis_ids = {option.id for option in request.hypothesis_options}
        if assertion.selected_hypothesis_id not in hypothesis_ids:
            issues.append(
                f"{path}.selected_hypothesis_id: unknown hypothesis id "
                f"{assertion.selected_hypothesis_id!r}"
            )
        confidence = assertion.expressed_confidence
        if confidence is not None and not math.isfinite(confidence):
            issues.append(f"{path}.expressed_confidence: value must be finite")
        return issues

    def decode_assertion(
        self,
        request: DiagnosisRequest,
        value: JsonValue,
    ) -> NarrativeAssertion:
        """Decode untrusted JSON and bind the selection to a declared option."""

        self.validate_request(request)
        issues = _schema_errors(self._assertion_validator, value)
        if issues:
            raise NarrativeContractError(issues)
        root = _object(value)
        confidence_value = root["expressed_confidence"]
        assertion = NarrativeAssertion(
            selected_hypothesis_id=_string(root["selected_hypothesis_id"]),
            public_explanation=_string(root["public_explanation"]),
            expressed_confidence=(
                None if confidence_value is None else _number(confidence_value)
            ),
        )
        issues = self.assertion_errors(request, assertion)
        if issues:
            raise NarrativeContractError(issues)
        return assertion

    def result_errors(
        self,
        request: DiagnosisRequest,
        result: NarrativeResult,
    ) -> list[str]:
        """Return complete result and request-relative errors."""

        issues = _schema_errors(self._result_validator, result.to_json_object())
        issues.extend(
            self.assertion_errors(request, result.assertion, path="$.assertion")
        )
        return issues

    def validate_result(
        self,
        request: DiagnosisRequest,
        result: NarrativeResult,
    ) -> None:
        """Raise when a narrative result is not conformant."""

        issues = self.result_errors(request, result)
        if issues:
            raise NarrativeContractError(issues)

    def decode_result(
        self,
        request: DiagnosisRequest,
        value: JsonValue,
    ) -> NarrativeResult:
        """Decode complete external JSON after schema validation."""

        self.validate_request(request)
        issues = _schema_errors(self._result_validator, value)
        if issues:
            raise NarrativeContractError(issues)
        root = _object(value)
        identity = _object(root["diagnostician"])
        assertion_value = _object(root["assertion"])
        artifact = _object(root["raw_output_artifact"])
        digest = _object(artifact["digest"])
        usage = _object(root["usage"])
        confidence_value = assertion_value["expressed_confidence"]
        result = NarrativeResult(
            contract_version=_string(root["contract_version"]),
            diagnostician=DiagnosticianIdentity(
                actor_id=_string(identity["actor_id"]),
                actor_type=ActorType(_string(identity["actor_type"])),
                adapter_id=_string(identity["adapter_id"]),
                adapter_version=_string(identity["adapter_version"]),
            ),
            assertion=NarrativeAssertion(
                selected_hypothesis_id=_string(
                    assertion_value["selected_hypothesis_id"]
                ),
                public_explanation=_string(assertion_value["public_explanation"]),
                expressed_confidence=(
                    None if confidence_value is None else _number(confidence_value)
                ),
            ),
            raw_output_artifact=RawOutputArtifact(
                uri=_string(artifact["uri"]),
                media_type=_string(artifact["media_type"]),
                sha256=_string(digest["value"]),
                captured_at=_string(artifact["captured_at"]),
            ),
            usage=DiagnosisUsage(
                model_input_tokens=_integer(usage["model_input_tokens"]),
                model_output_tokens=_integer(usage["model_output_tokens"]),
                wall_time_ms=_integer(usage["wall_time_ms"]),
                human_minutes=_number(usage["human_minutes"]),
            ),
        )
        self.validate_result(request, result)
        return result

    def invoke(
        self,
        diagnostician: NarrativeDiagnostician,
        request: DiagnosisRequest,
    ) -> NarrativeResult:
        """Validate a request, invoke a diagnostician, and validate its result."""

        self.validate_request(request)
        result = diagnostician.diagnose(request)
        self.validate_result(request, result)
        return result


@dataclass(frozen=True, slots=True)
class ModelNarrativeDiagnostician:
    """Decode a generic strict model invocation as a narrative diagnosis."""

    invoker: StructuredModelInvoker

    def diagnose(self, request: DiagnosisRequest, /) -> NarrativeResult:
        """Perform one model attempt and return a validated narrative result."""

        validator = NarrativeContractValidator()
        validator.validate_request(request)
        output = self.invoker.invoke(
            request.to_json_object(),
            narrative_output_profile(),
        )
        try:
            assertion = validator.decode_assertion(request, output.value)
        except NarrativeContractError:
            raise ModelAdapterError(
                ModelAdapterFailure.ASSERTION_CONTRACT,
                "model assertion violated the narrative diagnosis contract",
                artifact=output.raw_output_artifact,
            ) from None
        result = NarrativeResult(
            contract_version=NARRATIVE_CONTRACT_VERSION,
            diagnostician=output.diagnostician,
            assertion=assertion,
            raw_output_artifact=output.raw_output_artifact,
            usage=output.usage,
        )
        validator.validate_result(request, result)
        return result
