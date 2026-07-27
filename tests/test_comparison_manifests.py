# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator

from experiments.cases.cache_key_comparison import (
    COMPARISON_METRICS,
    build_comparison_request,
)
from experiments.cases.cache_key_omission import CaseEnvironment, Mechanism
from experiments.comparison_manifests import (
    ComparisonManifestFormatError,
    ComparisonManifestValidationError,
    ComparisonManifestValidator,
    build_comparison_manifest,
    diagnosis_profile_from_comparison_manifest,
    endpoint_from_comparison_manifest,
    load_comparison_manifest,
)
from experiments.diagnostician import DiagnosisRequest
from experiments.model_adapters import OpenAIChatEndpoint
from itself import JsonObject, JsonValue

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = ROOT / "experiments" / "contracts" / "v1"
MANIFEST_PATH = CONTRACT_ROOT / "examples" / "comparison-manifest.json"


def _request() -> DiagnosisRequest:
    context = CaseEnvironment(
        Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION
    ).diagnostic_context()
    return build_comparison_request(context)


def _endpoint() -> OpenAIChatEndpoint:
    return OpenAIChatEndpoint(
        actor_id="together-gpt-oss-20b-case-001-comparison",
        base_url="https://api.together.ai/v1",
        model="openai/gpt-oss-20b",
        api_key_env="TOGETHER_API_KEY",
        timeout_seconds=90.0,
        max_output_tokens=2_048,
        extra_body={"seed": 2_026_072_202, "temperature": 0.0},
    )


def _manifest() -> JsonObject:
    return load_comparison_manifest(MANIFEST_PATH)


def test_comparison_manifest_schema_and_frozen_example_are_valid() -> None:
    schema_value = cast(
        JsonValue,
        json.loads(
            (CONTRACT_ROOT / "comparison-manifest.schema.json").read_text(
                encoding="utf-8"
            )
        ),
    )
    assert isinstance(schema_value, dict)
    Draft202012Validator.check_schema(schema_value)
    ComparisonManifestValidator().validate(_manifest())


def test_frozen_comparison_manifest_matches_both_exact_model_payloads() -> None:
    ComparisonManifestValidator().validate_against(
        _manifest(),
        _endpoint(),
        _request(),
    )


@pytest.mark.parametrize(
    ("section", "field", "replacement"),
    [
        ("case", "request_sha256", "0" * 64),
        ("diagnostician", "model", "other/model"),
        ("generation", "seed", 42),
        ("generation", "temperature", 0.5),
        ("execution", "timeout_seconds", 30.0),
    ],
)
def test_comparison_manifest_binding_rejects_execution_drift(
    section: str,
    field: str,
    replacement: JsonValue,
) -> None:
    manifest = deepcopy(_manifest())
    section_value = cast(JsonObject, manifest[section])
    section_value[field] = replacement

    with pytest.raises(ComparisonManifestValidationError, match=field):
        ComparisonManifestValidator().validate_against(
            manifest,
            _endpoint(),
            _request(),
        )


@pytest.mark.parametrize("profile", ["narrative", "structured"])
def test_comparison_manifest_binding_rejects_profile_payload_drift(
    profile: str,
) -> None:
    manifest = deepcopy(_manifest())
    profiles = cast(JsonObject, manifest["profiles"])
    selected = cast(JsonObject, profiles[profile])
    selected["prompt_payload_sha256"] = "1" * 64

    with pytest.raises(
        ComparisonManifestValidationError,
        match=rf"profiles\.{profile}\.prompt_payload_sha256",
    ):
        ComparisonManifestValidator().validate_against(
            manifest,
            _endpoint(),
            _request(),
        )


def test_comparison_manifest_requires_exact_ordered_metrics() -> None:
    manifest = deepcopy(_manifest())
    manifest["metrics"] = list(reversed(COMPARISON_METRICS))

    with pytest.raises(
        ComparisonManifestValidationError,
        match="must exactly match the ordered comparison scorecard fields",
    ):
        ComparisonManifestValidator().validate(manifest)


def test_comparison_manifest_rejects_unregistered_request_options() -> None:
    endpoint = OpenAIChatEndpoint(
        actor_id="together-gpt-oss-20b-case-001-comparison",
        base_url="https://api.together.ai/v1",
        model="openai/gpt-oss-20b",
        api_key_env="TOGETHER_API_KEY",
        extra_body={
            "seed": 2_026_072_202,
            "temperature": 0.0,
            "top_k": 40,
        },
    )

    with pytest.raises(
        ComparisonManifestValidationError,
        match="unregistered options: top_k",
    ):
        ComparisonManifestValidator().validate_against(
            _manifest(),
            endpoint,
            _request(),
        )


def test_comparison_manifest_rejects_extra_headers() -> None:
    endpoint = OpenAIChatEndpoint(
        actor_id="together-gpt-oss-20b-case-001-comparison",
        base_url="https://api.together.ai/v1",
        model="openai/gpt-oss-20b",
        api_key_env="TOGETHER_API_KEY",
        extra_body={"seed": 2_026_072_202, "temperature": 0.0},
        extra_headers={"X-Experimental-Route": "alpha"},
    )

    with pytest.raises(ComparisonManifestValidationError, match="extra_headers"):
        ComparisonManifestValidator().validate_against(
            _manifest(),
            endpoint,
            _request(),
        )


@pytest.mark.parametrize(
    "source",
    [
        '{"manifest_version":"0.2.0","manifest_version":"0.2.0"}',
        '{"value":NaN}',
        "[]",
    ],
)
def test_comparison_manifest_loader_rejects_non_strict_json(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(source, encoding="utf-8")

    with pytest.raises(ComparisonManifestFormatError):
        load_comparison_manifest(path)


def test_endpoint_factory_reconstructs_registered_public_configuration() -> None:
    endpoint = endpoint_from_comparison_manifest(
        _manifest(),
        base_url="https://api.together.ai/v1",
        api_key_env="TOGETHER_API_KEY",
    )

    assert endpoint == _endpoint()
    ComparisonManifestValidator().validate_against(
        _manifest(),
        endpoint,
        _request(),
    )


def test_manifest_builder_binds_repeated_reasoning_enabled_study() -> None:
    endpoint = OpenAIChatEndpoint(
        actor_id="generic-gpt-oss-120b-case-001-comparison",
        base_url="https://inference.example.com/v1",
        model="organization/gpt-oss-120b",
        api_key_env="INFERENCE_API_KEY",
        timeout_seconds=180.0,
        max_output_tokens=8_192,
        stream=True,
        extra_body={
            "reasoning_effort": "medium",
            "temperature": 0.2,
            "top_p": 0.95,
        },
    )

    manifest = build_comparison_manifest(
        endpoint,
        _request(),
        comparison_series_id="case-001-generic-gpt-oss-120b-study-v1",
        registered_at=datetime(2026, 7, 24, 18, tzinfo=UTC),
        replicate_count=10,
        study_phase="preregistered_study",
        limitations=("One transparent case does not establish transfer.",),
    )

    design = cast(JsonObject, manifest["design"])
    generation = cast(JsonObject, manifest["generation"])
    assert design["replicate_count"] == 10
    assert generation["reasoning_effort"] == "medium"
    assert generation["stream"] is True
    assert generation["temperature"] == 0.2
    assert generation["top_p"] == 0.95
    assert generation["seed"] is None
    ComparisonManifestValidator().validate_against(
        manifest,
        endpoint,
        _request(),
    )
    assert (
        endpoint_from_comparison_manifest(
            manifest,
            base_url="https://inference.example.com/v1",
            api_key_env="INFERENCE_API_KEY",
        )
        == endpoint
    )


def test_manifest_binding_rejects_reasoning_effort_drift() -> None:
    manifest = deepcopy(_manifest())
    generation = cast(JsonObject, manifest["generation"])
    generation["reasoning_effort"] = "medium"

    with pytest.raises(
        ComparisonManifestValidationError,
        match="generation.reasoning_effort",
    ):
        ComparisonManifestValidator().validate_against(
            manifest,
            _endpoint(),
            _request(),
        )


def test_manifest_binding_rejects_top_p_drift() -> None:
    manifest = deepcopy(_manifest())
    generation = cast(JsonObject, manifest["generation"])
    generation["top_p"] = 0.95

    with pytest.raises(
        ComparisonManifestValidationError,
        match="generation.top_p",
    ):
        ComparisonManifestValidator().validate_against(
            manifest,
            _endpoint(),
            _request(),
        )


def test_manifest_binding_rejects_streaming_mode_drift() -> None:
    manifest = deepcopy(_manifest())
    generation = cast(JsonObject, manifest["generation"])
    generation["stream"] = True

    with pytest.raises(
        ComparisonManifestValidationError,
        match="generation.stream",
    ):
        ComparisonManifestValidator().validate_against(
            manifest,
            _endpoint(),
            _request(),
        )


def test_manifest_binds_and_reconstructs_categorical_observation_profile() -> None:
    manifest = build_comparison_manifest(
        _endpoint(),
        _request(),
        comparison_series_id="case-001-categorical-observation-study-v3",
        registered_at=datetime(2026, 7, 24, 21, tzinfo=UTC),
        replicate_count=5,
        study_phase="preregistered_study",
        limitations=("One transparent case does not establish transfer.",),
        expected_observation_values=("A", "B"),
    )

    profiles = cast(JsonObject, manifest["profiles"])
    structured = cast(JsonObject, profiles["structured"])
    profile = diagnosis_profile_from_comparison_manifest(manifest)
    schema = profile.schema_json()
    definitions = cast(JsonObject, schema["$defs"])
    prediction = cast(JsonObject, definitions["prediction"])
    properties = cast(JsonObject, prediction["properties"])
    expected_observation = cast(JsonObject, properties["expected_observation"])

    assert structured["profile_id"] == "diagnosis-assertion-v0.3.0"
    assert structured["expected_observation_values"] == ["A", "B"]
    assert expected_observation["enum"] == ["A", "B"]
    ComparisonManifestValidator().validate_against(
        manifest,
        _endpoint(),
        _request(),
    )


def test_manifest_rejects_categorical_observation_profile_drift() -> None:
    manifest = build_comparison_manifest(
        _endpoint(),
        _request(),
        comparison_series_id="case-001-categorical-observation-study-v3",
        registered_at=datetime(2026, 7, 24, 21, tzinfo=UTC),
        replicate_count=5,
        study_phase="preregistered_study",
        limitations=("One transparent case does not establish transfer.",),
        expected_observation_values=("A", "B"),
    )
    profiles = cast(JsonObject, manifest["profiles"])
    structured = cast(JsonObject, profiles["structured"])
    structured["expected_observation_values"] = ["A", "C"]

    with pytest.raises(
        ComparisonManifestValidationError,
        match="profiles.structured",
    ):
        ComparisonManifestValidator().validate_against(
            manifest,
            _endpoint(),
            _request(),
        )


def test_comparison_manifest_contains_no_endpoint_url_or_credential_reference() -> None:
    serialized = json.dumps(_manifest(), sort_keys=True)

    assert "api.together.ai" not in serialized
    assert "TOGETHER_API_KEY" not in serialized
    assert "Authorization" not in serialized
