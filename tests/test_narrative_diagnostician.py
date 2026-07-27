# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import cast

import pytest

from experiments.cases.cache_key_diagnostician import build_cache_key_request
from experiments.cases.cache_key_omission import CaseEnvironment, Mechanism
from experiments.diagnostician import (
    DiagnosisRequest,
    DiagnosisUsage,
    DiagnosticianIdentity,
    RawOutputArtifact,
)
from experiments.model_adapters import (
    ModelAdapterError,
    ModelAdapterFailure,
    StructuredModelOutput,
    StructuredOutputProfile,
)
from experiments.narrative_diagnostician import (
    ModelNarrativeDiagnostician,
    NarrativeAssertion,
    NarrativeContractError,
    NarrativeContractValidator,
    NarrativeResult,
    narrative_assertion_schema,
    narrative_output_profile,
)
from itself import ActorType, JsonObject, JsonValue

CAPTURED_AT = "2026-07-22T20:00:00Z"


def _request() -> DiagnosisRequest:
    context = CaseEnvironment(
        Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION
    ).diagnostic_context()
    return build_cache_key_request(context)


def _artifact() -> RawOutputArtifact:
    return RawOutputArtifact(
        uri="artifacts/sha256-" + "a" * 64 + ".json",
        media_type="application/json",
        sha256="a" * 64,
        captured_at=CAPTURED_AT,
    )


def _identity() -> DiagnosticianIdentity:
    return DiagnosticianIdentity(
        actor_id="test-narrative-model",
        actor_type=ActorType.MODEL,
        adapter_id="openai-chat-completions",
        adapter_version="0.2.0",
    )


def _assertion_value() -> JsonObject:
    return {
        "selected_hypothesis_id": "cache_key_omits_source_revision",
        "public_explanation": "The repeated stale result is consistent with reuse.",
        "expressed_confidence": 0.6,
    }


def _result() -> NarrativeResult:
    return NarrativeResult(
        diagnostician=_identity(),
        assertion=NarrativeAssertion(
            selected_hypothesis_id="cache_key_omits_source_revision",
            public_explanation=("The repeated stale result is consistent with reuse."),
            expressed_confidence=0.6,
        ),
        raw_output_artifact=_artifact(),
        usage=DiagnosisUsage(
            model_input_tokens=20,
            model_output_tokens=10,
            wall_time_ms=50,
        ),
    )


def _calls() -> list[tuple[JsonValue, StructuredOutputProfile]]:
    return []


@dataclass(slots=True)
class RecordingInvoker:
    value: JsonValue
    calls: list[tuple[JsonValue, StructuredOutputProfile]] = field(
        default_factory=_calls
    )

    def invoke(
        self,
        input_value: JsonValue,
        profile: StructuredOutputProfile,
    ) -> StructuredModelOutput:
        self.calls.append((input_value, profile))
        return StructuredModelOutput(
            value=self.value,
            diagnostician=_identity(),
            raw_output_artifact=_artifact(),
            usage=DiagnosisUsage(
                model_input_tokens=20,
                model_output_tokens=10,
                wall_time_ms=50,
            ),
        )


def test_narrative_assertion_round_trips_and_binds_selection() -> None:
    assertion = NarrativeContractValidator().decode_assertion(
        _request(),
        _assertion_value(),
    )

    assert assertion.selected_hypothesis_id == (
        Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION.value
    )
    assert assertion.expressed_confidence == 0.6
    assert assertion.to_json_object() == _assertion_value()


def test_narrative_assertion_maps_null_confidence() -> None:
    value = _assertion_value()
    value["expressed_confidence"] = None

    assertion = NarrativeContractValidator().decode_assertion(_request(), value)

    assert assertion.expressed_confidence is None


@pytest.mark.parametrize("forbidden", ["predictions", "selected_test_id", "verdict"])
def test_narrative_schema_rejects_epistemic_or_test_fields(forbidden: str) -> None:
    value = _assertion_value()
    value[forbidden] = [] if forbidden == "predictions" else "forbidden"

    with pytest.raises(NarrativeContractError, match=forbidden):
        NarrativeContractValidator().decode_assertion(_request(), value)


def test_narrative_assertion_rejects_unknown_hypothesis() -> None:
    value = _assertion_value()
    value["selected_hypothesis_id"] = "unknown_hypothesis"

    with pytest.raises(NarrativeContractError, match="unknown hypothesis id"):
        NarrativeContractValidator().decode_assertion(_request(), value)


def test_narrative_result_round_trips_through_strict_decoder() -> None:
    original = _result()

    decoded = NarrativeContractValidator().decode_result(
        _request(),
        original.to_json_object(),
    )

    assert decoded == original


def test_narrative_result_revalidates_request_relative_selection() -> None:
    invalid = replace(
        _result(),
        assertion=replace(
            _result().assertion,
            selected_hypothesis_id="unknown_hypothesis",
        ),
    )

    with pytest.raises(NarrativeContractError, match="unknown hypothesis id"):
        NarrativeContractValidator().validate_result(_request(), invalid)


def test_model_narrative_wrapper_uses_minimal_profile_and_adapter_metadata() -> None:
    invoker = RecordingInvoker(_assertion_value())

    result = ModelNarrativeDiagnostician(invoker).diagnose(_request())

    assert result == _result()
    assert len(invoker.calls) == 1
    input_value, profile = invoker.calls[0]
    assert input_value == _request().to_json_object()
    assert profile.profile_id == "narrative-assertion-v0.2.0"
    assert profile.schema_json() == narrative_assertion_schema()
    properties = cast(JsonObject, profile.schema_json()["properties"])
    assert "selected_test_id" not in properties
    assert "do not retain or rank alternatives" in profile.system_prompt.lower()


def test_model_narrative_wrapper_maps_contract_failure_to_adapter_failure() -> None:
    invalid = _assertion_value()
    invalid["selected_test_id"] = "bypass_caching_proxy"

    with pytest.raises(ModelAdapterError) as captured:
        ModelNarrativeDiagnostician(RecordingInvoker(invalid)).diagnose(_request())

    assert captured.value.failure is ModelAdapterFailure.ASSERTION_CONTRACT
    assert captured.value.artifact == _artifact()


def test_narrative_profile_schema_is_defensive() -> None:
    profile = narrative_output_profile()
    first = profile.schema_json()
    first["type"] = "array"

    assert cast(str, profile.schema_json()["type"]) == "object"


def test_narrative_artifact_timestamp_is_valid_utc_fixture() -> None:
    parsed = datetime.fromisoformat(_artifact().captured_at.replace("Z", "+00:00"))

    assert parsed.tzinfo is UTC
