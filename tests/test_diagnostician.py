# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from typing import cast

import pytest

from experiments.cases.cache_key_diagnostician import (
    CacheKeyFixtureDiagnostician,
    build_cache_key_request,
)
from experiments.cases.cache_key_omission import (
    CaseEnvironment,
    Intervention,
    Mechanism,
)
from experiments.diagnostician import (
    DiagnosisAssertion,
    DiagnosisContractError,
    DiagnosisRequest,
    DiagnosticianContractValidator,
    DiagnosticPrediction,
    HypothesisOption,
    SuppliedResultDiagnostician,
)
from itself import JsonObject, JsonValue


def _request(
    mechanism: Mechanism = Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION,
) -> DiagnosisRequest:
    return build_cache_key_request(CaseEnvironment(mechanism).diagnostic_context())


def test_request_is_identical_for_every_hidden_mechanism() -> None:
    requests = {
        json.dumps(_request(mechanism).to_json_object(), sort_keys=True)
        for mechanism in Mechanism
    }

    assert len(requests) == 1


def test_fixture_result_conforms_and_is_deterministic() -> None:
    request = _request()
    validator = DiagnosticianContractValidator()

    first = validator.invoke(CacheKeyFixtureDiagnostician(), request)
    second = validator.invoke(CacheKeyFixtureDiagnostician(), request)

    assert first == second
    assert first.assertion.primary_hypothesis_id == (
        Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION.value
    )
    assert first.assertion.selected_test_id == Intervention.BYPASS_CACHING_PROXY.value


def test_fixture_artifact_digest_covers_canonical_assertion() -> None:
    result = DiagnosticianContractValidator().invoke(
        CacheKeyFixtureDiagnostician(),
        _request(),
    )
    canonical_assertion = json.dumps(
        result.assertion.to_json_object(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert (
        result.raw_output_artifact.sha256
        == hashlib.sha256(canonical_assertion).hexdigest()
    )


def test_result_json_round_trips_through_strict_decoder() -> None:
    request = _request()
    validator = DiagnosticianContractValidator()
    original = validator.invoke(CacheKeyFixtureDiagnostician(), request)
    wire_value = cast(
        JsonValue,
        json.loads(json.dumps(original.to_json_object(), allow_nan=False)),
    )

    decoded = validator.decode_result(request, wire_value)

    assert decoded == original
    assert validator.invoke(SuppliedResultDiagnostician(decoded), request) == original


def test_model_assertion_round_trips_through_strict_decoder() -> None:
    request = _request()
    validator = DiagnosticianContractValidator()
    original = CacheKeyFixtureDiagnostician().diagnose(request).assertion

    decoded = validator.decode_assertion(request, original.to_model_json_object())

    assert decoded == original


def test_model_assertion_maps_null_confidence_to_none() -> None:
    request = _request()
    assertion = CacheKeyFixtureDiagnostician().diagnose(request).assertion
    value = assertion.to_model_json_object()
    value["expressed_confidence"] = None

    decoded = DiagnosticianContractValidator().decode_assertion(request, value)

    assert decoded.expressed_confidence is None


def test_model_assertion_applies_request_relative_reference_checks() -> None:
    request = _request()
    assertion = CacheKeyFixtureDiagnostician().diagnose(request).assertion
    invalid = assertion.to_model_json_object()
    invalid["selected_test_id"] = "unknown_but_well_formed_test"
    predictions = cast(list[JsonValue], invalid["predictions"])
    for item in predictions:
        prediction = cast(JsonObject, item)
        prediction["test_id"] = "unknown_but_well_formed_test"

    with pytest.raises(DiagnosisContractError, match="unknown test id"):
        DiagnosticianContractValidator().decode_assertion(request, invalid)


def test_decoder_rejects_authoritative_field_before_typed_construction() -> None:
    request = _request()
    result = CacheKeyFixtureDiagnostician().diagnose(request).to_json_object()
    invalid = deepcopy(result)
    assertion = cast(JsonObject, invalid["assertion"])
    assertion["verdict"] = "supported"

    with pytest.raises(DiagnosisContractError, match="verdict"):
        DiagnosticianContractValidator().decode_result(request, invalid)


def test_decoder_applies_request_relative_reference_checks() -> None:
    request = _request()
    result = CacheKeyFixtureDiagnostician().diagnose(request).to_json_object()
    invalid = deepcopy(result)
    assertion = cast(JsonObject, invalid["assertion"])
    assertion["selected_test_id"] = "unknown_but_well_formed_test"
    predictions = cast(list[JsonValue], assertion["predictions"])
    for item in predictions:
        prediction = cast(JsonObject, item)
        prediction["test_id"] = "unknown_but_well_formed_test"

    with pytest.raises(DiagnosisContractError, match="unknown test id"):
        DiagnosticianContractValidator().decode_result(request, invalid)


def test_request_rejects_duplicate_hypothesis_option_ids() -> None:
    request = _request()
    duplicate = replace(
        request,
        hypothesis_options=(
            *request.hypothesis_options,
            HypothesisOption(
                id=request.hypothesis_options[0].id,
                statement="Duplicate identifier with another statement.",
            ),
        ),
    )

    with pytest.raises(DiagnosisContractError, match="duplicate id"):
        DiagnosticianContractValidator().validate_request(duplicate)


def test_result_rejects_primary_hypothesis_that_was_not_retained() -> None:
    request = _request()
    result = CacheKeyFixtureDiagnostician().diagnose(request)
    invalid = replace(
        result,
        assertion=replace(
            result.assertion,
            retained_hypothesis_ids=(Mechanism.WORKER_READS_STALE_SOURCE.value,),
            predictions=(
                DiagnosticPrediction(
                    hypothesis_id=Mechanism.WORKER_READS_STALE_SOURCE.value,
                    test_id=Intervention.BYPASS_CACHING_PROXY.value,
                    expected_observation="A",
                    falsified_when="The observed revision is not A.",
                ),
            ),
        ),
    )

    with pytest.raises(
        DiagnosisContractError, match="primary hypothesis is not retained"
    ):
        DiagnosticianContractValidator().validate_result(request, invalid)


def test_result_rejects_unknown_selected_test() -> None:
    request = _request()
    result = CacheKeyFixtureDiagnostician().diagnose(request)
    invalid_assertion = replace(
        result.assertion,
        selected_test_id="arbitrary_shell_command",
    )

    with pytest.raises(DiagnosisContractError, match="unknown test id"):
        DiagnosticianContractValidator().validate_result(
            request,
            replace(result, assertion=invalid_assertion),
        )


def test_result_requires_one_selected_test_prediction_per_retained_hypothesis() -> None:
    request = _request()
    result = CacheKeyFixtureDiagnostician().diagnose(request)
    assertion = result.assertion
    incomplete = DiagnosisAssertion(
        retained_hypothesis_ids=assertion.retained_hypothesis_ids,
        primary_hypothesis_id=assertion.primary_hypothesis_id,
        predictions=assertion.predictions[:-1],
        selected_test_id=assertion.selected_test_id,
        public_explanation=assertion.public_explanation,
    )

    with pytest.raises(DiagnosisContractError, match="exactly one prediction"):
        DiagnosticianContractValidator().validate_result(
            request,
            replace(result, assertion=incomplete),
        )


def test_observation_wire_values_are_defensive_copies() -> None:
    observation = _request().observations[0]
    first = observation.to_json_object()
    result = first["result"]
    assert isinstance(result, dict)
    result["task_result"] = "tampered"

    second = observation.to_json_object()
    assert isinstance(second["result"], dict)
    assert second["result"]["task_result"] == "failed"
