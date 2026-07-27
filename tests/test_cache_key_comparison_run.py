# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from experiments.cases.cache_key_comparison import (
    NARRATIVE_ATTEMPT_ID,
    STRUCTURED_ATTEMPT_ID,
    build_comparison_request,
)
from experiments.cases.cache_key_comparison_run import (
    main,
    run_cache_key_comparison,
)
from experiments.cases.cache_key_diagnostician import (
    CacheKeyFixtureDiagnostician,
    CacheKeyNarrativeFixtureDiagnostician,
)
from experiments.cases.cache_key_omission import CaseEnvironment, Mechanism
from experiments.comparison_bundles import ComparisonBundleValidator
from experiments.comparison_manifests import build_comparison_manifest
from experiments.diagnostician import DiagnosisRequest
from experiments.model_adapters import HttpRequest, HttpResponse, OpenAIChatEndpoint
from itself import JsonObject

SECRET = "runner-secret-that-must-not-leak"
NOW = datetime(2026, 7, 24, 19, 0, tzinfo=UTC)


def _request() -> DiagnosisRequest:
    context = CaseEnvironment(
        Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION
    ).diagnostic_context()
    return build_comparison_request(context)


def _endpoint() -> OpenAIChatEndpoint:
    return OpenAIChatEndpoint(
        actor_id="together-glm-5p2-case-001-comparison",
        base_url="https://api.together.xyz/v1",
        model="zai-org/GLM-5.2",
        api_key_env="TOGETHER_API_KEY",
        timeout_seconds=180.0,
        max_output_tokens=4_096,
        extra_body={"temperature": 0.0},
    )


def _manifest(
    *,
    expected_observation_values: tuple[str, ...] = (),
) -> JsonObject:
    return build_comparison_manifest(
        _endpoint(),
        _request(),
        comparison_series_id="case-001-together-glm-5p2-comparison-v1",
        registered_at=NOW,
        replicate_count=3,
        study_phase="preregistered_study",
        limitations=("One transparent case does not establish behavioral transfer.",),
        expected_observation_values=expected_observation_values,
    )


def _manifest_path(tmp_path: Path, manifest: JsonObject | None = None) -> Path:
    path = tmp_path / "comparison-manifest.json"
    path.write_text(
        json.dumps(_manifest() if manifest is None else manifest),
        encoding="utf-8",
    )
    return path


def _completion_body(
    assertion: JsonObject,
    *,
    input_tokens: int,
    output_tokens: int,
) -> bytes:
    envelope: JsonObject = {
        "id": "comparison-runner-completion",
        "model": "zai-org/GLM-5.2",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(assertion),
                },
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }
    return json.dumps(envelope).encode()


def _narrative_body() -> bytes:
    assertion = CacheKeyNarrativeFixtureDiagnostician().diagnose(_request()).assertion
    return _completion_body(
        assertion.to_json_object(),
        input_tokens=90,
        output_tokens=30,
    )


def _structured_body() -> bytes:
    assertion = CacheKeyFixtureDiagnostician().diagnose(_request()).assertion
    return _completion_body(
        assertion.to_model_json_object(),
        input_tokens=150,
        output_tokens=100,
    )


def _request_list() -> list[HttpRequest]:
    return []


@dataclass(slots=True)
class SequenceTransport:
    responses: list[HttpResponse]
    requests: list[HttpRequest] = field(default_factory=_request_list)

    def send(self, request: HttpRequest, /) -> HttpResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("transport received an unregistered request")
        return self.responses.pop(0)


@dataclass(slots=True)
class SequenceClock:
    values: list[int]

    def __call__(self) -> int:
        if not self.values:
            raise AssertionError("clock exhausted")
        return self.values.pop(0)


def _success_transport() -> SequenceTransport:
    return SequenceTransport(
        [
            HttpResponse(
                200,
                {"Content-Type": "application/json"},
                _narrative_body(),
            ),
            HttpResponse(
                200,
                {"Content-Type": "application/json"},
                _structured_body(),
            ),
        ]
    )


def test_runner_executes_exactly_two_registered_attempts(
    tmp_path: Path,
) -> None:
    transport = _success_transport()
    bundle_path = tmp_path / "replicate-002"

    bundle_manifest = run_cache_key_comparison(
        _manifest_path(tmp_path),
        bundle_path,
        replicate_index=2,
        base_url=_endpoint().base_url,
        api_key_env="TOGETHER_API_KEY",
        transport=transport,
        environment={"TOGETHER_API_KEY": SECRET},
        now=lambda: NOW,
        monotonic_ns=SequenceClock([1_000_000, 4_000_000, 5_000_000, 12_000_000]),
    )

    assert bundle_manifest["outcome"] == "completed"
    assert len(transport.requests) == 2
    payloads = [
        cast(JsonObject, json.loads(request.body)) for request in transport.requests
    ]
    schema_names = [
        cast(
            str,
            cast(
                JsonObject,
                cast(JsonObject, payload["response_format"])["json_schema"],
            )["name"],
        )
        for payload in payloads
    ]
    assert schema_names == [
        "itself_narrative_assertion_v0_2_0",
        "itself_diagnosis_assertion_v0_2_0",
    ]
    assert all(payload["model"] == "zai-org/GLM-5.2" for payload in payloads)
    assert all(payload["temperature"] == 0.0 for payload in payloads)
    assert all(
        request.headers["Authorization"] == f"Bearer {SECRET}"
        for request in transport.requests
    )

    verified = ComparisonBundleValidator().validate_completed(
        bundle_path,
        _request(),
    )
    assert verified.bundle_manifest == bundle_manifest


def test_runner_uses_manifest_bound_categorical_observation_profile(
    tmp_path: Path,
) -> None:
    transport = _success_transport()
    manifest = _manifest(expected_observation_values=("A", "B"))
    manifest_path = _manifest_path(tmp_path, manifest)

    bundle_manifest = run_cache_key_comparison(
        manifest_path,
        tmp_path / "replicate-001",
        replicate_index=1,
        base_url=_endpoint().base_url,
        api_key_env="TOGETHER_API_KEY",
        transport=transport,
        environment={"TOGETHER_API_KEY": SECRET},
        now=lambda: NOW,
        monotonic_ns=SequenceClock([1_000_000, 4_000_000, 5_000_000, 12_000_000]),
    )

    assert bundle_manifest["outcome"] == "completed"
    structured_payload = cast(JsonObject, json.loads(transport.requests[1].body))
    response_format = cast(JsonObject, structured_payload["response_format"])
    schema_wrapper = cast(JsonObject, response_format["json_schema"])
    schema = cast(JsonObject, schema_wrapper["schema"])
    definitions = cast(JsonObject, schema["$defs"])
    prediction = cast(JsonObject, definitions["prediction"])
    properties = cast(JsonObject, prediction["properties"])
    expected_observation = cast(JsonObject, properties["expected_observation"])
    assert expected_observation["enum"] == ["A", "B"]


def test_narrative_failure_makes_no_structured_attempt(tmp_path: Path) -> None:
    transport = SequenceTransport(
        [
            HttpResponse(
                429,
                {"Content-Type": "application/json"},
                b'{"error":"rate limited"}',
            )
        ]
    )

    bundle_manifest = run_cache_key_comparison(
        _manifest_path(tmp_path),
        tmp_path / "replicate-001",
        replicate_index=1,
        base_url=_endpoint().base_url,
        api_key_env="TOGETHER_API_KEY",
        transport=transport,
        environment={"TOGETHER_API_KEY": SECRET},
        now=lambda: NOW,
        monotonic_ns=SequenceClock([1_000_000, 2_000_000]),
    )

    assert len(transport.requests) == 1
    assert bundle_manifest["attempt_outcomes"] == {
        NARRATIVE_ATTEMPT_ID: "adapter_failed",
        STRUCTURED_ATTEMPT_ID: "not_run",
    }


def test_structured_failure_retains_narrative_and_stops(tmp_path: Path) -> None:
    transport = SequenceTransport(
        [
            HttpResponse(
                200,
                {"Content-Type": "application/json"},
                _narrative_body(),
            ),
            HttpResponse(
                503,
                {"Content-Type": "application/json"},
                b'{"error":"server unavailable"}',
            ),
        ]
    )

    bundle_manifest = run_cache_key_comparison(
        _manifest_path(tmp_path),
        tmp_path / "replicate-003",
        replicate_index=3,
        base_url=_endpoint().base_url,
        api_key_env="TOGETHER_API_KEY",
        transport=transport,
        environment={"TOGETHER_API_KEY": SECRET},
        now=lambda: NOW,
        monotonic_ns=SequenceClock([1_000_000, 4_000_000, 5_000_000, 6_000_000]),
    )

    assert len(transport.requests) == 2
    assert bundle_manifest["attempt_outcomes"] == {
        NARRATIVE_ATTEMPT_ID: "completed",
        STRUCTURED_ATTEMPT_ID: "adapter_failed",
    }


def test_manifest_drift_fails_before_network_or_bundle(tmp_path: Path) -> None:
    manifest = deepcopy(_manifest())
    diagnostician = cast(JsonObject, manifest["diagnostician"])
    diagnostician["base_url_sha256"] = "0" * 64
    transport = _success_transport()
    bundle_path = tmp_path / "replicate-001"

    try:
        run_cache_key_comparison(
            _manifest_path(tmp_path, manifest),
            bundle_path,
            replicate_index=1,
            base_url=_endpoint().base_url,
            api_key_env="TOGETHER_API_KEY",
            transport=transport,
            environment={"TOGETHER_API_KEY": SECRET},
            now=lambda: NOW,
        )
    except ValueError as error:
        assert "base_url_sha256" in str(error)
    else:
        raise AssertionError("manifest drift was accepted")

    assert not transport.requests
    assert not bundle_path.exists()


def test_validate_cli_recomputes_existing_bundle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle_path = tmp_path / "replicate-001"
    run_cache_key_comparison(
        _manifest_path(tmp_path),
        bundle_path,
        replicate_index=1,
        base_url=_endpoint().base_url,
        api_key_env="TOGETHER_API_KEY",
        transport=_success_transport(),
        environment={"TOGETHER_API_KEY": SECRET},
        now=lambda: NOW,
        monotonic_ns=SequenceClock([1_000_000, 4_000_000, 5_000_000, 12_000_000]),
    )

    assert main(["validate", "--bundle", str(bundle_path)]) == 0
    summary = cast(JsonObject, json.loads(capsys.readouterr().out))
    assert summary["outcome"] == "completed"
    assert summary["replicate_index"] == 1
