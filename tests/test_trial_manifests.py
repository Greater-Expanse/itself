# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator

from experiments.cases.cache_key_exchange import build_exchange_request
from experiments.model_adapters import OpenAIChatEndpoint
from experiments.trial_manifests import (
    TrialManifestFormatError,
    TrialManifestValidationError,
    TrialManifestValidator,
    canonical_json_bytes,
    json_sha256,
    load_trial_manifest,
)
from itself import JsonObject, JsonValue

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = ROOT / "experiments" / "contracts" / "v1"
MANIFEST_PATH = CONTRACT_ROOT / "examples" / "trial-manifest.json"


def _endpoint() -> OpenAIChatEndpoint:
    return OpenAIChatEndpoint(
        actor_id="together-gpt-oss-20b-case-001",
        base_url="https://api.together.ai/v1",
        model="openai/gpt-oss-20b",
        api_key_env="TOGETHER_API_KEY",
        timeout_seconds=90.0,
        max_output_tokens=2_048,
        extra_body={"seed": 20_260_722, "temperature": 0.0},
    )


def _manifest() -> JsonObject:
    return load_trial_manifest(MANIFEST_PATH)


def test_trial_manifest_schema_and_frozen_example_are_valid() -> None:
    schema_value = cast(
        JsonValue,
        json.loads(
            (CONTRACT_ROOT / "trial-manifest.schema.json").read_text(encoding="utf-8")
        ),
    )
    assert isinstance(schema_value, dict)
    Draft202012Validator.check_schema(schema_value)
    TrialManifestValidator().validate(_manifest())


def test_frozen_manifest_matches_exact_endpoint_and_request() -> None:
    TrialManifestValidator().validate_against(
        _manifest(),
        _endpoint(),
        build_exchange_request(),
    )


@pytest.mark.parametrize(
    ("section", "field", "replacement"),
    [
        ("case", "request_sha256", "0" * 64),
        ("diagnostician", "model", "other/model"),
        ("generation", "seed", 42),
        ("generation", "temperature", 0.5),
        ("generation", "prompt_payload_sha256", "1" * 64),
        ("execution", "timeout_seconds", 30.0),
    ],
)
def test_manifest_binding_rejects_execution_drift(
    section: str,
    field: str,
    replacement: JsonValue,
) -> None:
    manifest = deepcopy(_manifest())
    section_value = cast(JsonObject, manifest[section])
    section_value[field] = replacement

    with pytest.raises(TrialManifestValidationError, match=field):
        TrialManifestValidator().validate_against(
            manifest,
            _endpoint(),
            build_exchange_request(),
        )


def test_manifest_binding_rejects_unregistered_request_options() -> None:
    endpoint = OpenAIChatEndpoint(
        actor_id="together-gpt-oss-20b-case-001",
        base_url="https://api.together.ai/v1",
        model="openai/gpt-oss-20b",
        api_key_env="TOGETHER_API_KEY",
        extra_body={
            "seed": 20_260_722,
            "temperature": 0.0,
            "top_p": 0.9,
        },
    )

    with pytest.raises(
        TrialManifestValidationError,
        match="unregistered options: top_p",
    ):
        TrialManifestValidator().validate_against(
            _manifest(),
            endpoint,
            build_exchange_request(),
        )


def test_manifest_binding_rejects_extra_headers() -> None:
    endpoint = OpenAIChatEndpoint(
        actor_id="together-gpt-oss-20b-case-001",
        base_url="https://api.together.ai/v1",
        model="openai/gpt-oss-20b",
        api_key_env="TOGETHER_API_KEY",
        extra_body={"seed": 20_260_722, "temperature": 0.0},
        extra_headers={"X-Experimental-Route": "alpha"},
    )

    with pytest.raises(TrialManifestValidationError, match="extra_headers"):
        TrialManifestValidator().validate_against(
            _manifest(),
            endpoint,
            build_exchange_request(),
        )


@pytest.mark.parametrize(
    "source",
    [
        '{"manifest_version":"0.2.0","manifest_version":"0.2.0"}',
        '{"value":NaN}',
        "[]",
    ],
)
def test_manifest_loader_rejects_non_strict_json(tmp_path: Path, source: str) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(source, encoding="utf-8")

    with pytest.raises(TrialManifestFormatError):
        load_trial_manifest(path)


def test_canonical_manifest_digest_is_stable() -> None:
    manifest = _manifest()

    assert canonical_json_bytes(manifest) == canonical_json_bytes(
        cast(JsonValue, json.loads(json.dumps(manifest, sort_keys=False)))
    )
    assert len(json_sha256(manifest)) == 64


def test_manifest_contains_no_endpoint_url_or_credential_reference() -> None:
    serialized = json.dumps(_manifest(), sort_keys=True)

    assert "api.together.ai" not in serialized
    assert "TOGETHER_API_KEY" not in serialized
    assert "Authorization" not in serialized
