# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from experiments.cases.cache_key_diagnostician import CacheKeyFixtureDiagnostician
from experiments.cases.cache_key_exchange import build_exchange_request
from experiments.cases.cache_key_scripted_run import (
    SCRIPTED_RUN_METRICS,
    ScriptedRunResult,
    run_scripted_case,
)
from experiments.diagnostician import DiagnosisRequest, DiagnosisResult
from experiments.model_adapters import (
    HttpRequest,
    HttpResponse,
    ModelAdapterError,
    OpenAIChatDiagnostician,
    OpenAIChatEndpoint,
    build_chat_completion_payload,
)
from experiments.trial_bundles import (
    AttemptQuarantinedError,
    TrialBundleBuilder,
    TrialBundleValidationError,
    TrialBundleValidator,
    parse_bundle_relative_path,
)
from experiments.trial_manifests import (
    TrialManifestValidator,
    canonical_json_bytes,
    endpoint_from_trial_manifest,
    json_sha256,
    load_trial_manifest,
)
from itself import (
    EvidenceBundleLimits,
    JsonlLedgerStore,
    JsonObject,
    Ledger,
    build_reasoning_receipt,
)
from itself.receipts import canonical_ledger_bytes

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


def _scripted_result(builder: TrialBundleBuilder) -> ScriptedRunResult:
    """Run the case once against a fake provider that writes into ``builder``."""

    adapter = OpenAIChatDiagnostician(
        endpoint=_endpoint(),
        artifact_sink=builder.artifact_sink,
        transport=StaticTransport(
            HttpResponse(200, {"Content-Type": "application/json"}, _completion_body())
        ),
        environment={"TOGETHER_API_KEY": SECRET},
        now=lambda: NOW,
        monotonic_ns=iter([1_000_000, 6_000_000]).__next__,
    )
    return run_scripted_case(adapter)


def _build_bundle(path: Path) -> tuple[JsonObject, bytes]:
    manifest = load_trial_manifest(MANIFEST_PATH)
    TrialManifestValidator().validate_against(
        manifest, _endpoint(), build_exchange_request()
    )
    with TrialBundleBuilder(
        path, manifest, now=lambda: NOW, metric_fields=SCRIPTED_RUN_METRICS
    ) as builder:
        bundle_manifest = builder.complete(_scripted_result(builder))
    return bundle_manifest, _completion_body()


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

    with TrialBundleBuilder(
        path, manifest, now=lambda: NOW, metric_fields=SCRIPTED_RUN_METRICS
    ) as builder:
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

    with TrialBundleBuilder(
        path, manifest, now=lambda: NOW, metric_fields=SCRIPTED_RUN_METRICS
    ) as builder:
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
        TrialBundleBuilder(
            path, manifest, now=lambda: NOW, metric_fields=SCRIPTED_RUN_METRICS
        ),
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
            now=lambda: NOW,
            metric_fields=SCRIPTED_RUN_METRICS,
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


def _load(path: Path) -> JsonObject:
    return cast(JsonObject, json.loads(path.read_text(encoding="utf-8")))


def _write(path: Path, value: JsonObject) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", "utf-8")


def _rebind(root: Path) -> None:
    """Re-digest every inventoried file and recompute the bundle identifier.

    This is what a forger who controls the bundle bytes can do, so only checks
    beyond the inventory can reject what follows.
    """

    manifest_path = root / "bundle.json"
    manifest = _load(manifest_path)
    for entry in cast(list[JsonObject], manifest["files"]):
        content = (root / cast(str, entry["path"])).read_bytes()
        entry["size_bytes"] = len(content)
        entry["digest"] = {
            "algorithm": "sha256",
            "value": hashlib.sha256(content).hexdigest(),
        }
    del manifest["bundle_id"]
    digest = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    manifest["bundle_id"] = f"urn:sha256:{digest}"
    _write(manifest_path, manifest)


def test_validator_recomputes_the_scorecard_from_the_diagnosis(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trial-001"
    _build_bundle(path)
    scorecard = _load(path / "scorecard.json")
    scorecard["ground_truth_identified"] = False
    scorecard["predictions_correct"] = 0
    scorecard["false_promotions_accepted"] = 7
    _write(path / "scorecard.json", scorecard)
    _rebind(path)

    with pytest.raises(TrialBundleValidationError, match="scorecard"):
        TrialBundleValidator().validate(path, build_exchange_request())


def test_validator_recomputes_the_ledger_from_the_diagnosis(tmp_path: Path) -> None:
    path = tmp_path / "trial-001"
    _build_bundle(path)
    ledger = JsonlLedgerStore(path / "ledger.jsonl").load()
    shortened = Ledger(ledger.records[:-1])
    (path / "ledger.jsonl").write_bytes(canonical_ledger_bytes(shortened))
    _write(path / "reasoning-receipt.json", build_reasoning_receipt(shortened))
    _rebind(path)

    with pytest.raises(TrialBundleValidationError, match="ledger"):
        TrialBundleValidator().validate(path, build_exchange_request())


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("case", "case_id", "some-other-case"),
        ("case", "request_sha256", "0" * 64),
        ("diagnostician", "model", "a-different/model"),
        ("diagnostician", "actor_id", "someone-else"),
    ],
)
def test_validator_binds_the_trial_manifest_to_its_request_and_diagnostician(
    tmp_path: Path,
    section: str,
    field: str,
    value: str,
) -> None:
    path = tmp_path / "trial-001"
    _build_bundle(path)
    manifest = _load(path / "trial-manifest.json")
    cast(JsonObject, manifest[section])[field] = value
    TrialManifestValidator().validate(manifest)
    _write(path / "trial-manifest.json", manifest)
    _rebind(path)

    with pytest.raises(TrialBundleValidationError, match="does not match"):
        TrialBundleValidator().validate(path, build_exchange_request())


def test_validator_rejects_a_non_regular_entry_in_the_bundle(tmp_path: Path) -> None:
    path = tmp_path / "trial-001"
    _build_bundle(path)
    os.mkfifo(path / "artifacts" / "pipe", 0o600)

    with pytest.raises(TrialBundleValidationError, match="non-regular"):
        TrialBundleValidator().validate(path, build_exchange_request())


def test_validator_does_not_open_a_fifo_in_place_of_its_manifest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trial-001"
    _build_bundle(path)
    manifest_path = path / "bundle.json"
    manifest_path.unlink()
    os.mkfifo(manifest_path, 0o600)
    finished = threading.Event()

    def release_a_blocked_reader() -> None:
        # Opening the pipe would block a reader until a writer arrives; open
        # and close a writer so a regression fails instead of hanging.
        while not finished.wait(0.05):
            try:
                descriptor = os.open(manifest_path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                continue
            os.close(descriptor)

    releaser = threading.Thread(target=release_a_blocked_reader, daemon=True)
    releaser.start()
    try:
        with pytest.raises(TrialBundleValidationError, match="regular file"):
            TrialBundleValidator().validate(path, build_exchange_request())
    finally:
        finished.set()
        releaser.join()


def test_validator_bounds_the_manifest_it_reads(tmp_path: Path) -> None:
    path = tmp_path / "trial-001"
    _build_bundle(path)
    manifest_path = path / "bundle.json"
    padding = b" " * EvidenceBundleLimits().max_manifest_bytes
    manifest_path.write_bytes(manifest_path.read_bytes() + padding)

    with pytest.raises(TrialBundleValidationError, match="exceeds limit"):
        TrialBundleValidator().validate(path, build_exchange_request())


@pytest.mark.parametrize("entry_point", ["validate", "validate_completed"])
def test_validator_refuses_a_symbolic_link_as_the_bundle_root(
    tmp_path: Path,
    entry_point: str,
) -> None:
    path = tmp_path / "trial-001"
    _build_bundle(path)
    link = tmp_path / "linked-trial"
    link.symlink_to(path, target_is_directory=True)
    validator = TrialBundleValidator()
    validate = (
        validator.validate
        if entry_point == "validate"
        else validator.validate_completed
    )

    with pytest.raises(TrialBundleValidationError, match="root is not a directory"):
        validate(link, build_exchange_request())


def test_validator_bounds_the_directory_entries_it_walks(tmp_path: Path) -> None:
    # Directories are allowed, so without the entry limit these empty ones
    # would leave the bundle valid.
    path = tmp_path / "trial-001"
    _build_bundle(path)
    limit = EvidenceBundleLimits().max_directory_entries
    for index in range(limit):
        (path / f"empty-{index:04d}").mkdir(mode=0o700)

    with pytest.raises(TrialBundleValidationError, match=f"exceeds limit {limit}"):
        TrialBundleValidator().validate(path, build_exchange_request())


@dataclass(frozen=True, slots=True)
class _ReshapedMetrics:
    value: JsonObject

    def to_json_object(self) -> JsonObject:
        return dict(self.value)


@dataclass(frozen=True, slots=True)
class _ReshapedTrial:
    request: DiagnosisRequest
    diagnosis: DiagnosisResult
    ledger: Ledger
    metrics: _ReshapedMetrics


def test_builder_refuses_metrics_other_than_the_declared_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trial-001"
    manifest = load_trial_manifest(MANIFEST_PATH)

    with (
        pytest.raises(
            AttemptQuarantinedError,
            match="runner metrics differ from the metric fields given to the builder",
        ),
        TrialBundleBuilder(
            path, manifest, now=lambda: NOW, metric_fields=SCRIPTED_RUN_METRICS
        ) as builder,
    ):
        result = _scripted_result(builder)
        metrics = result.metrics.to_json_object()
        metrics["ledger_records"] = metrics.pop("ledger_record_count")
        builder.complete(
            _ReshapedTrial(
                request=result.request,
                diagnosis=result.diagnosis,
                ledger=result.ledger,
                metrics=_ReshapedMetrics(metrics),
            )
        )

    assert not path.exists()


def test_validator_refuses_a_request_that_is_not_the_case_input(
    tmp_path: Path,
) -> None:
    # The manifest is rebound to the other request, so only the replay can
    # tell that this request is not case 001's input.
    path = tmp_path / "trial-001"
    _build_bundle(path)
    request = replace(build_exchange_request(), task="Explain a different failure.")
    manifest = _load(path / "trial-manifest.json")
    endpoint = endpoint_from_trial_manifest(
        manifest,
        base_url="https://api.together.ai/v1",
        api_key_env="TOGETHER_API_KEY",
    )
    cast(JsonObject, manifest["case"])["request_sha256"] = json_sha256(
        request.to_json_object()
    )
    cast(JsonObject, manifest["generation"])["prompt_payload_sha256"] = json_sha256(
        build_chat_completion_payload(endpoint, request)
    )
    _write(path / "trial-manifest.json", manifest)
    _rebind(path)

    with pytest.raises(TrialBundleValidationError, match="frozen case-001 input"):
        TrialBundleValidator().validate(path, request)
