# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from experiments.cases.cache_key_diagnostician import CacheKeyFixtureDiagnostician
from experiments.cases.cache_key_exchange import build_exchange_request
from experiments.cases.cache_key_scripted_run import run_scripted_case
from experiments.model_adapters import (
    HttpRequest,
    HttpResponse,
    ModelAdapterError,
    OpenAIChatDiagnostician,
    OpenAIChatEndpoint,
)
from experiments.trial_bundles import (
    TrialBundleBuilder,
    TrialBundleValidationError,
    TrialBundleValidator,
    parse_bundle_relative_path,
)
from experiments.trial_manifests import (
    TrialManifestValidator,
    load_trial_manifest,
)
from itself import JsonObject

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = (
    ROOT / "experiments" / "contracts" / "v1" / "examples" / "trial-manifest.json"
)
SECRET = "bundle-secret-that-must-not-leak"
NOW = datetime(2026, 7, 22, 18, 0, tzinfo=UTC)


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


def _completion_body() -> bytes:
    request = build_exchange_request()
    assertion = CacheKeyFixtureDiagnostician().diagnose(request).assertion
    envelope: JsonObject = {
        "id": "bundle-completion",
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


def _build_bundle(path: Path) -> tuple[JsonObject, bytes]:
    endpoint = _endpoint()
    manifest = load_trial_manifest(MANIFEST_PATH)
    request = build_exchange_request()
    TrialManifestValidator().validate_against(manifest, endpoint, request)
    raw_response = _completion_body()
    with TrialBundleBuilder(path, manifest, now=NOW) as builder:
        adapter = OpenAIChatDiagnostician(
            endpoint=endpoint,
            artifact_sink=builder.artifact_sink,
            transport=StaticTransport(
                HttpResponse(200, {"Content-Type": "application/json"}, raw_response)
            ),
            environment={"TOGETHER_API_KEY": SECRET},
            now=lambda: NOW,
            monotonic_ns=iter([1_000_000, 6_000_000]).__next__,
        )
        result = run_scripted_case(adapter)
        bundle_manifest = builder.complete(result)
    return bundle_manifest, raw_response


def test_completed_bundle_is_atomic_private_and_recomputable(tmp_path: Path) -> None:
    path = tmp_path / "trial-001"

    bundle_manifest, raw_response = _build_bundle(path)
    verified = TrialBundleValidator().validate_completed(
        path,
        build_exchange_request(),
    )

    assert bundle_manifest["outcome"] == "completed"
    assert verified.bundle_manifest == bundle_manifest
    assert verified.scorecard["ground_truth_identified"] is True
    assert verified.scorecard["receipt_recomputes"] is True
    assert verified.scorecard["model_input_tokens"] == 100
    assert verified.scorecard["model_output_tokens"] == 50
    assert verified.scorecard["wall_time_ms"] == 5
    assert stat.S_IMODE(path.stat().st_mode) == 0o700

    diagnosis = verified.diagnosis
    artifact_path = path / diagnosis.raw_output_artifact.uri
    assert artifact_path.read_bytes() == raw_response
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((path / "bundle.json").stat().st_mode) == 0o600


def test_bundle_contains_no_secret_endpoint_or_absolute_artifact_path(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trial-001"
    _build_bundle(path)

    all_content = b"".join(
        item.read_bytes() for item in path.rglob("*") if item.is_file()
    )
    diagnosis = cast(
        JsonObject,
        json.loads((path / "diagnosis-result.json").read_text(encoding="utf-8")),
    )
    artifact = cast(JsonObject, diagnosis["raw_output_artifact"])

    assert SECRET.encode() not in all_content
    assert b"TOGETHER_API_KEY" not in all_content
    assert b"api.together.ai" not in all_content
    assert cast(str, artifact["uri"]).startswith("artifacts/")
    assert not cast(str, artifact["uri"]).startswith("/")


def test_bundle_validation_detects_tampered_file(tmp_path: Path) -> None:
    path = tmp_path / "trial-001"
    _build_bundle(path)
    scorecard_path = path / "scorecard.json"
    scorecard = cast(
        JsonObject,
        json.loads(scorecard_path.read_text(encoding="utf-8")),
    )
    scorecard["ground_truth_identified"] = False
    scorecard_path.write_text(json.dumps(scorecard), encoding="utf-8")

    with pytest.raises(TrialBundleValidationError, match="digest mismatch"):
        TrialBundleValidator().validate_completed(path, build_exchange_request())


def test_bundle_validation_rejects_uninventoried_file(tmp_path: Path) -> None:
    path = tmp_path / "trial-001"
    _build_bundle(path)
    unexpected = path / "unexpected.txt"
    unexpected.write_text("not declared", encoding="utf-8")
    unexpected.chmod(0o600)

    with pytest.raises(TrialBundleValidationError, match="inventory mismatch"):
        TrialBundleValidator().validate_completed(path, build_exchange_request())


def test_bundle_validation_rejects_public_file_permissions(tmp_path: Path) -> None:
    path = tmp_path / "trial-001"
    _build_bundle(path)
    (path / "scorecard.json").chmod(0o644)

    with pytest.raises(TrialBundleValidationError, match="group or other"):
        TrialBundleValidator().validate_completed(path, build_exchange_request())


def test_bundle_id_binds_non_file_metadata(tmp_path: Path) -> None:
    path = tmp_path / "trial-001"
    _build_bundle(path)
    bundle_path = path / "bundle.json"
    bundle = cast(
        JsonObject,
        json.loads(bundle_path.read_text(encoding="utf-8")),
    )
    bundle["created_at"] = "2026-07-22T18:00:01Z"
    bundle_path.write_text(
        json.dumps(bundle, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    bundle_path.chmod(0o600)

    with pytest.raises(TrialBundleValidationError, match="bundle metadata"):
        TrialBundleValidator().validate_completed(path, build_exchange_request())


def test_failed_attempt_bundle_retains_sanitized_error_and_private_body(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trial-failed"
    manifest = load_trial_manifest(MANIFEST_PATH)
    endpoint = _endpoint()
    private_body = b'{"error":"private provider detail"}'

    with TrialBundleBuilder(path, manifest, now=NOW) as builder:
        adapter = OpenAIChatDiagnostician(
            endpoint=endpoint,
            artifact_sink=builder.artifact_sink,
            transport=StaticTransport(
                HttpResponse(401, {"Content-Type": "application/json"}, private_body)
            ),
            environment={"TOGETHER_API_KEY": SECRET},
            now=lambda: NOW,
            monotonic_ns=iter([1_000_000, 2_000_000]).__next__,
        )
        with pytest.raises(ModelAdapterError) as captured:
            adapter.diagnose(build_exchange_request())
        builder.fail(captured.value)

    verified = TrialBundleValidator().validate_failure(path)
    failure = cast(JsonObject, verified.attempt_report["failure"])
    artifact = cast(JsonObject, failure["response_artifact"])
    report_text = (path / "attempt-report.json").read_text(encoding="utf-8")

    assert failure["kind"] == "http_authentication"
    assert failure["status_code"] == 401
    assert "private provider detail" not in report_text
    assert SECRET not in report_text
    assert (path / cast(str, artifact["uri"])).read_bytes() == private_body


def test_failed_attempt_without_response_has_no_private_artifact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trial-failed"
    manifest = load_trial_manifest(MANIFEST_PATH)

    with TrialBundleBuilder(path, manifest, now=NOW) as builder:
        adapter = OpenAIChatDiagnostician(
            endpoint=_endpoint(),
            artifact_sink=builder.artifact_sink,
            transport=StaticTransport(HttpResponse(200, {}, b"{}")),
            environment={},
            now=lambda: NOW,
        )
        with pytest.raises(ModelAdapterError) as captured:
            adapter.diagnose(build_exchange_request())
        builder.fail(captured.value)

    verified = TrialBundleValidator().validate_failure(path)

    assert verified.bundle_manifest["contains_private_artifacts"] is False
    assert set(item.name for item in path.iterdir()) == {
        "attempt-report.json",
        "bundle.json",
        "trial-manifest.json",
    }


def test_builder_does_not_publish_partial_bundle_on_exception(tmp_path: Path) -> None:
    path = tmp_path / "trial-001"
    manifest = load_trial_manifest(MANIFEST_PATH)

    with (
        pytest.raises(RuntimeError, match="simulated interruption"),
        TrialBundleBuilder(path, manifest, now=NOW),
    ):
        raise RuntimeError("simulated interruption")

    assert not path.exists()
    assert not tuple(tmp_path.glob(".trial-001.*"))


def test_builder_never_overwrites_existing_destination(tmp_path: Path) -> None:
    path = tmp_path / "trial-001"
    path.mkdir()
    marker = path / "owner-data.txt"
    marker.write_text("preserve", encoding="utf-8")

    with (
        pytest.raises(FileExistsError),
        TrialBundleBuilder(
            path,
            load_trial_manifest(MANIFEST_PATH),
            now=NOW,
        ),
    ):
        pass

    assert marker.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize(
    "value",
    ["../secret", "/absolute", "artifacts/../secret", "a\\b", "C:/secret"],
)
def test_bundle_rejects_path_smuggling(value: str) -> None:
    with pytest.raises(TrialBundleValidationError, match="normalized relative"):
        parse_bundle_relative_path(value)
