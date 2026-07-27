# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from experiments.cases.cache_key_diagnostician import CacheKeyFixtureDiagnostician
from experiments.cases.cache_key_exchange import build_exchange_request
from experiments.cases.cache_key_model_run import main, run_cache_key_model_case
from experiments.model_adapters import HttpRequest, HttpResponse
from experiments.trial_bundles import TrialBundleValidator
from experiments.trial_manifests import TrialManifestValidationError
from itself import JsonObject

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = (
    ROOT / "experiments" / "contracts" / "v1" / "examples" / "trial-manifest.json"
)
SECRET = "model-run-secret-that-must-not-leak"
NOW = datetime(2026, 7, 22, 19, 0, tzinfo=UTC)


def _requests() -> list[HttpRequest]:
    return []


def _completion_body() -> bytes:
    request = build_exchange_request()
    assertion = CacheKeyFixtureDiagnostician().diagnose(request).assertion
    envelope: JsonObject = {
        "id": "model-run-completion",
        "model": "openai/gpt-oss-20b",
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
            "prompt_tokens": 120,
            "completion_tokens": 60,
            "total_tokens": 180,
        },
    }
    return json.dumps(envelope).encode()


@dataclass(slots=True)
class RecordingTransport:
    response: HttpResponse
    requests: list[HttpRequest] = field(default_factory=_requests)

    def send(self, request: HttpRequest, /) -> HttpResponse:
        self.requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        return self.response


def test_full_model_run_executes_evidence_path_and_publishes_bundle(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "bundle"
    transport = RecordingTransport(
        HttpResponse(
            200,
            {"Content-Type": "application/json"},
            _completion_body(),
        )
    )

    bundle_manifest = run_cache_key_model_case(
        MANIFEST_PATH,
        bundle_path,
        base_url="https://api.together.ai/v1/",
        api_key_env="TOGETHER_API_KEY",
        transport=transport,
        environment={"TOGETHER_API_KEY": SECRET},
        now=lambda: NOW,
        monotonic_ns=iter([1_000_000, 10_000_000]).__next__,
    )
    verified = TrialBundleValidator().validate_completed(
        bundle_path,
        build_exchange_request(),
    )

    assert bundle_manifest["outcome"] == "completed"
    assert verified.scorecard["ground_truth_identified"] is True
    assert verified.scorecard["ground_truth_retained"] is True
    assert verified.scorecard["evidence_identifies_ground_truth"] is True
    assert verified.scorecard["false_promotions_accepted"] == 0
    assert verified.scorecard["receipt_recomputes"] is True
    assert len(transport.requests) == 1
    payload = cast(JsonObject, json.loads(transport.requests[0].body))
    assert payload["seed"] == 20_260_722
    assert payload["temperature"] == 0.0


def test_adapter_failure_is_a_retained_single_attempt(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    transport = RecordingTransport(
        HttpResponse(
            401,
            {"Content-Type": "application/json"},
            b'{"error":"private failure"}',
        )
    )

    bundle_manifest = run_cache_key_model_case(
        MANIFEST_PATH,
        bundle_path,
        base_url="https://api.together.ai/v1",
        api_key_env="TOGETHER_API_KEY",
        transport=transport,
        environment={"TOGETHER_API_KEY": SECRET},
        now=lambda: NOW,
        monotonic_ns=iter([1_000_000, 2_000_000]).__next__,
    )
    verified = TrialBundleValidator().validate_failure(bundle_path)

    assert bundle_manifest["outcome"] == "adapter_failed"
    assert verified.attempt_report["attempt_index"] == 1
    assert len(transport.requests) == 1


def test_manifest_drift_fails_before_network_or_bundle(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    transport = RecordingTransport(HttpResponse(200, {}, _completion_body()))

    with pytest.raises(TrialManifestValidationError, match="base_url_sha256"):
        run_cache_key_model_case(
            MANIFEST_PATH,
            bundle_path,
            base_url="https://other-provider.example/v1",
            api_key_env="TOGETHER_API_KEY",
            transport=transport,
            environment={"TOGETHER_API_KEY": SECRET},
            now=lambda: NOW,
        )

    assert not transport.requests
    assert not bundle_path.exists()


def test_validate_cli_recomputes_existing_bundle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle_path = tmp_path / "bundle"
    run_cache_key_model_case(
        MANIFEST_PATH,
        bundle_path,
        base_url="https://api.together.ai/v1",
        api_key_env="TOGETHER_API_KEY",
        transport=RecordingTransport(HttpResponse(200, {}, _completion_body())),
        environment={"TOGETHER_API_KEY": SECRET},
        now=lambda: NOW,
        monotonic_ns=iter([1_000_000, 2_000_000]).__next__,
    )

    exit_code = main(["validate", "--bundle", str(bundle_path)])

    captured = capsys.readouterr()
    summary = cast(JsonObject, json.loads(captured.out))
    assert exit_code == 0
    assert summary["outcome"] == "completed"
    assert summary["bundle"] == str(bundle_path)


def test_validate_cli_returns_one_for_valid_failed_attempt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle_path = tmp_path / "bundle"
    run_cache_key_model_case(
        MANIFEST_PATH,
        bundle_path,
        base_url="https://api.together.ai/v1",
        api_key_env="TOGETHER_API_KEY",
        transport=RecordingTransport(HttpResponse(401, {}, b"private")),
        environment={"TOGETHER_API_KEY": SECRET},
        now=lambda: NOW,
        monotonic_ns=iter([1_000_000, 2_000_000]).__next__,
    )

    exit_code = main(["validate", "--bundle", str(bundle_path)])

    captured = capsys.readouterr()
    summary = cast(JsonObject, json.loads(captured.out))
    assert exit_code == 1
    assert summary["outcome"] == "adapter_failed"
