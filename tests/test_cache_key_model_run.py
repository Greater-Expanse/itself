# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import hashlib
import itertools
import json
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

import experiments.cases.cache_key_model_run as model_run
import experiments.cases.cache_key_scripted_run as scripted_run
import experiments.trial_bundles as trial_bundles
from experiments.cases.cache_key_diagnostician import CacheKeyFixtureDiagnostician
from experiments.cases.cache_key_exchange import build_exchange_request
from experiments.cases.cache_key_model_run import main, run_cache_key_model_case
from experiments.cases.cache_key_omission import Mechanism
from experiments.cases.cache_key_scripted_run import (
    ScriptedRunResult,
    run_scripted_case,
)
from experiments.diagnostician import Diagnostician
from experiments.model_adapters import HttpRequest, HttpResponse
from experiments.trial_bundles import TrialBundleValidator
from experiments.trial_manifests import (
    TrialManifestValidationError,
    TrialManifestValidator,
    canonical_json_bytes,
    load_trial_manifest,
)
from itself import JsonObject, JsonValue, Ledger, build_reasoning_receipt

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


def _run(
    bundle_path: Path,
    transport: RecordingTransport,
    *,
    manifest_path: Path = MANIFEST_PATH,
    now: Callable[[], datetime] = lambda: NOW,
) -> JsonObject:
    return run_cache_key_model_case(
        manifest_path,
        bundle_path,
        base_url="https://api.together.ai/v1",
        api_key_env="TOGETHER_API_KEY",
        transport=transport,
        environment={"TOGETHER_API_KEY": SECRET},
        now=now,
        monotonic_ns=itertools.count(1_000_000, 1_000_000).__next__,
    )


def _completed_transport() -> RecordingTransport:
    return RecordingTransport(
        HttpResponse(200, {"Content-Type": "application/json"}, _completion_body())
    )


def test_a_manifest_that_omits_a_scorecard_metric_is_refused_before_the_request(
    tmp_path: Path,
) -> None:
    manifest = load_trial_manifest(MANIFEST_PATH)
    metrics = cast(list[JsonValue], manifest["metrics"])
    manifest["metrics"] = [metric for metric in metrics if metric != "wall_time_ms"]
    TrialManifestValidator().validate(manifest)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    transport = _completed_transport()

    with pytest.raises(ValueError, match="metrics"):
        _run(tmp_path / "bundle", transport, manifest_path=manifest_path)

    assert not transport.requests
    assert sorted(path.name for path in tmp_path.iterdir()) == ["manifest.json"]


def test_a_dangling_symbolic_link_destination_is_refused_before_the_request(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "bundle"
    bundle_path.symlink_to(tmp_path / "missing")
    transport = _completed_transport()

    with pytest.raises(FileExistsError):
        _run(bundle_path, transport)

    assert not transport.requests
    assert sorted(path.name for path in tmp_path.iterdir()) == ["bundle"]


def test_a_status_outside_the_contract_range_is_retained_as_a_failed_attempt(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "bundle"
    body = b"<html>an unregistered status</html>"
    transport = RecordingTransport(
        HttpResponse(999, {"Content-Type": "text/html"}, body)
    )

    bundle_manifest = _run(bundle_path, transport)

    verified = TrialBundleValidator().validate_failure(bundle_path)
    failure = cast(JsonObject, verified.attempt_report["failure"])
    artifact = cast(JsonObject, failure["response_artifact"])
    assert bundle_manifest["outcome"] == "adapter_failed"
    assert failure["kind"] == "http_status"
    assert failure["status_code"] is None
    assert (bundle_path / cast(str, artifact["uri"])).read_bytes() == body
    assert len(transport.requests) == 1


def test_a_failure_after_the_request_quarantines_the_captured_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_after_the_request(adapter: Diagnostician) -> ScriptedRunResult:
        run_scripted_case(adapter)
        raise RuntimeError("a defect after the provider responded")

    monkeypatch.setattr(model_run, "run_scripted_case", fail_after_the_request)
    bundle_path = tmp_path / "bundle"
    quarantine = tmp_path / "bundle.quarantined"

    with pytest.raises(RuntimeError, match="quarantined") as raised:
        _run(bundle_path, _completed_transport())

    assert str(quarantine) in str(raised.value)
    assert not bundle_path.exists()
    captured = [path.read_bytes() for path in quarantine.rglob("*") if path.is_file()]
    assert captured == [_completion_body()]
    assert stat.S_IMODE(quarantine.stat().st_mode) == stat.S_IRWXU

    # The registered slot stays used, so a second attempt never starts.
    second = _completed_transport()
    with pytest.raises(FileExistsError, match="quarantined"):
        _run(bundle_path, second)
    assert not second.requests


def test_an_interrupt_after_the_request_propagates_and_quarantines_the_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupted_after_the_request(adapter: Diagnostician) -> ScriptedRunResult:
        run_scripted_case(adapter)
        raise KeyboardInterrupt

    monkeypatch.setattr(model_run, "run_scripted_case", interrupted_after_the_request)
    bundle_path = tmp_path / "bundle"
    quarantine = tmp_path / "bundle.quarantined"

    # The interrupt is not converted into an error a caller might handle.
    with pytest.raises(KeyboardInterrupt) as raised:
        _run(bundle_path, _completed_transport())

    assert type(raised.value) is KeyboardInterrupt
    assert not bundle_path.exists()
    captured = [path.read_bytes() for path in quarantine.rglob("*") if path.is_file()]
    assert captured == [_completion_body()]
    assert stat.S_IMODE(quarantine.stat().st_mode) == stat.S_IRWXU


def test_a_receipt_that_does_not_recompute_quarantines_the_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def drifted_receipt(ledger: Ledger) -> JsonObject:
        return build_reasoning_receipt(Ledger(ledger.records[:-1]))

    monkeypatch.setattr(trial_bundles, "build_reasoning_receipt", drifted_receipt)

    with pytest.raises(RuntimeError, match="does not recompute"):
        _run(tmp_path / "bundle", _completed_transport())

    assert (tmp_path / "bundle.quarantined").is_dir()
    assert not (tmp_path / "bundle").exists()


def _accepted_promotion_stand_in(
    selected_hypothesis: Mechanism,
    position: int,
) -> JsonObject:
    """Return a record the ledger accepts in place of the premature promotion."""

    del selected_hypothesis
    created_at = f"2026-07-22T13:00:{position:02d}Z"
    return {
        "protocol_version": "0.1.0-alpha.3",
        "kind": "artifact_reference",
        "id": "artifact-accepted-promotion-stand-in",
        "created_at": created_at,
        "created_by": {
            "id": "case-001-promotion-policy",
            "actor_type": "software",
            "role": "evaluator",
        },
        "uri": "artifacts/stand-in.json",
        "media_type": "application/json",
        "title": "A record accepted where the promotion should be refused",
        "digest": {"algorithm": "sha256", "value": "0" * 64},
        "captured_at": created_at,
    }


def test_an_accepted_premature_promotion_is_published_as_a_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        scripted_run, "_premature_promotion_record", _accepted_promotion_stand_in
    )
    bundle_path = tmp_path / "bundle"

    bundle_manifest = _run(bundle_path, _completed_transport())

    verified = TrialBundleValidator().validate_completed(
        bundle_path,
        build_exchange_request(),
    )
    assert bundle_manifest["outcome"] == "completed"
    assert verified.scorecard["false_promotion_attempts"] == 1
    assert verified.scorecard["false_promotions_accepted"] == 1
    assert (
        "artifact-accepted-promotion-stand-in"
        not in verified.ledger.snapshot.record_ids
    )


def test_a_failed_attempt_completes_after_its_response_is_captured(
    tmp_path: Path,
) -> None:
    ticks = itertools.count()
    bundle_path = tmp_path / "bundle"
    transport = RecordingTransport(
        HttpResponse(401, {"Content-Type": "application/json"}, b'{"error":"x"}')
    )

    _run(bundle_path, transport, now=lambda: NOW + timedelta(seconds=next(ticks)))

    report = TrialBundleValidator().validate_failure(bundle_path).attempt_report
    failure = cast(JsonObject, report["failure"])
    artifact = cast(JsonObject, failure["response_artifact"])
    completed_at = datetime.fromisoformat(cast(str, report["completed_at"]))
    captured_at = datetime.fromisoformat(cast(str, artifact["captured_at"]))
    assert completed_at > captured_at


def test_validate_cli_reports_a_missing_bundle_with_exit_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["validate", "--bundle", str(tmp_path / "missing")])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert not captured.out
    assert captured.err.startswith("ERROR: ")
    assert captured.err.count("\n") == 1


def test_validate_cli_reports_a_non_integer_inventory_size_with_exit_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle_path = tmp_path / "bundle"
    _run(bundle_path, _completed_transport())
    manifest_path = bundle_path / "bundle.json"
    manifest = cast(JsonObject, json.loads(manifest_path.read_text(encoding="utf-8")))
    files = cast(list[JsonObject], manifest["files"])
    files[0]["size_bytes"] = float(cast(int, files[0]["size_bytes"]))
    del manifest["bundle_id"]
    digest = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    manifest["bundle_id"] = f"urn:sha256:{digest}"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    exit_code = main(["validate", "--bundle", str(bundle_path)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "size_bytes" in captured.err
    assert captured.err.count("\n") == 1


def test_run_cli_reports_an_unexpected_failure_with_exit_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Exit 1 means a valid failed-attempt bundle, so nothing else may use it.
    def crash(*args: object, **kwargs: object) -> JsonObject:
        raise RuntimeError("an unexpected defect")

    monkeypatch.setattr(model_run, "run_cache_key_model_case", crash)

    exit_code = main(
        [
            "run",
            "--manifest",
            "manifest.json",
            "--bundle",
            "bundle",
            "--base-url",
            "https://api.together.ai/v1",
            "--api-key-env",
            "TOGETHER_API_KEY",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.err == "ERROR: RuntimeError: an unexpected defect\n"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            TrialManifestValidationError(
                "case.case_id does not match the registered case\n"
                "case.request_sha256 does not match the registered case"
            ),
            "ERROR: case.case_id does not match the registered case; "
            "case.request_sha256 does not match the registered case\n",
        ),
        (
            RuntimeError("an unexpected defect\n\nwith a detail line"),
            "ERROR: RuntimeError: an unexpected defect; with a detail line\n",
        ),
    ],
)
def test_run_cli_reports_a_multi_line_error_on_one_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    expected: str,
) -> None:
    def fail(*args: object, **kwargs: object) -> JsonObject:
        raise error

    monkeypatch.setattr(model_run, "run_cache_key_model_case", fail)

    exit_code = main(
        [
            "run",
            "--manifest",
            "manifest.json",
            "--bundle",
            "bundle",
            "--base-url",
            "https://api.together.ai/v1",
            "--api-key-env",
            "TOGETHER_API_KEY",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert not captured.out
    assert captured.err == expected
