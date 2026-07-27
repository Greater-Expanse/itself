# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from experiments.cases.cache_key_diagnostician import CacheKeyFixtureDiagnostician
from experiments.cases.cache_key_exchange import build_exchange_request
from experiments.cases.cache_key_model_trial import (
    run_cache_key_model_trial,
    write_trial_report,
)
from experiments.model_adapters import HttpRequest, HttpResponse, OpenAIChatEndpoint
from itself import JsonObject

SECRET = "trial-secret-that-must-not-leak"
NOW = datetime(2026, 7, 22, 16, 0, tzinfo=UTC)


def _endpoint() -> OpenAIChatEndpoint:
    return OpenAIChatEndpoint(
        actor_id="trial-model",
        base_url="https://private-model-gateway.example.test/v1",
        model="example/model-1",
        api_key_env="TRIAL_API_KEY",
    )


def _completion_body() -> bytes:
    request = build_exchange_request()
    assertion = CacheKeyFixtureDiagnostician().diagnose(request).assertion
    envelope: JsonObject = {
        "id": "trial-completion",
        "model": "example/model-1",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(assertion.to_model_json_object()),
                },
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        },
    }
    return json.dumps(envelope).encode()


@dataclass(frozen=True, slots=True)
class StaticTransport:
    response: HttpResponse

    def send(self, request: HttpRequest, /) -> HttpResponse:
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        return self.response


def test_success_report_is_replayable_and_secret_free(tmp_path: Path) -> None:
    report = run_cache_key_model_trial(
        _endpoint(),
        tmp_path / "artifacts",
        transport=StaticTransport(
            HttpResponse(200, {"Content-Type": "application/json"}, _completion_body())
        ),
        environment={"TRIAL_API_KEY": SECRET},
        now=lambda: NOW,
        monotonic_ns=iter([1_000_000, 6_000_000]).__next__,
    )

    assert report["outcome"] == "conformant"
    assert report["attempt_count"] == 1
    assert report["profile_id"] == "diagnosis-assertion-v0.2.0"
    assert report["completed_at"] == "2026-07-22T16:00:00Z"
    assert len(cast(str, report["request_sha256"])) == 64
    assert len(cast(str, report["assertion_schema_sha256"])) == 64
    endpoint = cast(JsonObject, report["endpoint"])
    assert endpoint["model"] == "example/model-1"
    assert endpoint["max_output_tokens"] == 2_048
    assert endpoint["stream"] is False
    assert endpoint["timeout_seconds"] == 90.0
    assert len(cast(str, endpoint["base_url_sha256"])) == 64

    serialized = json.dumps(report, sort_keys=True)
    assert SECRET not in serialized
    assert "TRIAL_API_KEY" not in serialized
    assert "private-model-gateway.example.test" not in serialized


def test_trial_can_bind_categorical_observation_profile(tmp_path: Path) -> None:
    report = run_cache_key_model_trial(
        _endpoint(),
        tmp_path / "artifacts",
        transport=StaticTransport(
            HttpResponse(200, {"Content-Type": "application/json"}, _completion_body())
        ),
        environment={"TRIAL_API_KEY": SECRET},
        now=lambda: NOW,
        monotonic_ns=iter([1_000_000, 6_000_000]).__next__,
        expected_observation_values=("A", "B"),
    )

    assert report["outcome"] == "conformant"
    assert report["profile_id"] == "diagnosis-assertion-v0.3.0"


def test_failure_report_preserves_stage_and_artifact_without_error_body(
    tmp_path: Path,
) -> None:
    private_error = b'{"error":"private provider body"}'
    report = run_cache_key_model_trial(
        _endpoint(),
        tmp_path / "artifacts",
        transport=StaticTransport(
            HttpResponse(401, {"Content-Type": "application/json"}, private_error)
        ),
        environment={"TRIAL_API_KEY": SECRET},
        now=lambda: NOW,
        monotonic_ns=iter([1_000_000, 2_000_000]).__next__,
    )

    assert report["outcome"] == "failed"
    failure = cast(JsonObject, report["failure"])
    assert failure["kind"] == "http_authentication"
    assert failure["status_code"] == 401
    assert failure["retryable"] is False
    serialized = json.dumps(report, sort_keys=True)
    assert SECRET not in serialized
    assert "private provider body" not in serialized


def test_trial_report_write_is_atomic_and_private(tmp_path: Path) -> None:
    report: JsonObject = {"outcome": "failed"}
    path = tmp_path / "nested" / "report.json"

    write_trial_report(path, report)

    assert json.loads(path.read_text()) == report
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
