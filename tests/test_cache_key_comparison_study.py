# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import stat
import subprocess
import sys
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import pytest

import experiments.cases.cache_key_comparison_study as study_module
import itself.inference as shipped_inference
from experiments.cases.cache_key_comparison_study import (
    FIREWORKS_ENDPOINT,
    MAX_OUTPUT_TOKENS,
    PRIMARY_ENDPOINT_BY_VERSION,
    REPLICATE_COUNT,
    STUDY_EXECUTION_SLOTS,
    STUDY_EXECUTION_SLOTS_BY_VERSION,
    STUDY_TARGETS,
    STUDY_TARGETS_BY_VERSION,
    STUDY_VERSION,
    TEMPERATURE,
    TIMEOUT_SECONDS,
    TOGETHER_ENDPOINT,
    TOP_P,
    V5_TARGET_ORDER,
    V6_TARGET_ORDER,
    ComparisonStudyTarget,
    ProviderEndpoint,
    build_study_manifest,
    main,
    validate_study_manifests,
    write_study_manifests,
)
from experiments.comparison_manifests import ComparisonManifestValidationError
from experiments.model_adapters import (
    ShippedAdapterIdentity,
    build_structured_output_payload,
    diagnosis_output_profile,
)
from experiments.narrative_diagnostician import narrative_output_profile
from experiments.trial_manifests import json_sha256
from itself import JsonObject

ROOT = Path(__file__).resolve().parents[1]
COMMITTED_MANIFESTS = ROOT / "experiments" / "001-cache-key-omission" / "manifests"
NOW = datetime(2026, 7, 24, 20, 0, tzinfo=UTC)
V5_TARGETS = STUDY_TARGETS_BY_VERSION["5"]
V6_TARGETS = STUDY_TARGETS_BY_VERSION["6"]


def test_current_study_matrix_has_viable_current_model_targets() -> None:
    family_counts = Counter(target.model_family for target in V5_TARGETS)
    provider_counts = Counter(target.provider for target in V5_TARGETS)

    assert len(V5_TARGETS) == 3
    assert family_counts == {
        "GLM 5.2": 1,
        "MiniMax M3": 1,
        "Qwen 3.7 Plus": 1,
    }
    assert provider_counts == {FIREWORKS_ENDPOINT.provider: 3}
    assert len({target.target_id for target in V5_TARGETS}) == 3
    assert len({target.model_id for target in V5_TARGETS}) == 3


def test_primary_behavioral_cohort_fixes_provider_as_provenance_only() -> None:
    assert {target.base_url for target in V5_TARGETS} == {FIREWORKS_ENDPOINT.base_url}
    assert {target.api_key_env for target in V5_TARGETS} == {
        FIREWORKS_ENDPOINT.api_key_env
    }
    for target in V5_TARGETS:
        manifest = build_study_manifest(target, registered_at=NOW)
        metrics = manifest["metrics"]
        limitations = manifest["limitations"]

        assert isinstance(metrics, list)
        assert all(
            "provider" not in metric for metric in metrics if isinstance(metric, str)
        )
        assert isinstance(limitations, list)
        assert any(
            "not a study factor or behavioral metric" in limitation
            for limitation in limitations
            if isinstance(limitation, str)
        )


def test_primary_execution_order_is_registered_round_robin() -> None:
    assert tuple(target.target_id for target in V5_TARGETS) == V5_TARGET_ORDER
    assert (
        tuple(
            (target_id, replicate_index)
            for replicate_index in range(1, REPLICATE_COUNT + 1)
            for target_id in V5_TARGET_ORDER
        )
        == STUDY_EXECUTION_SLOTS_BY_VERSION["5"]
    )


def test_model_provenance_does_not_misclassify_hosted_qwen_as_open_weight() -> None:
    qwen_targets = [
        target for target in V5_TARGETS if target.model_family == "Qwen 3.7 Plus"
    ]
    open_weight_targets = [
        target for target in V5_TARGETS if target.checkpoint_status == "public_weights"
    ]

    assert all(target.checkpoint_status == "hosted_only" for target in qwen_targets)
    assert all(target.checkpoint_url is None for target in qwen_targets)
    assert all(target.license_name is None for target in qwen_targets)
    assert {target.model_family for target in open_weight_targets} == {
        "GLM 5.2",
        "MiniMax M3",
    }


def test_every_target_uses_one_shared_registered_generation_profile() -> None:
    for target in V5_TARGETS:
        endpoint = target.endpoint()

        assert endpoint.timeout_seconds == TIMEOUT_SECONDS
        assert endpoint.max_output_tokens == MAX_OUTPUT_TOKENS
        assert endpoint.stream is True
        assert target.expected_observation_values == ("A", "B")
        assert endpoint.extra_body == {
            "temperature": TEMPERATURE,
            "top_p": TOP_P,
        }
        assert "seed" not in endpoint.extra_body
        assert "reasoning_effort" not in endpoint.extra_body


def test_study_manifests_bind_targets_without_credentials_or_endpoint_urls() -> None:
    for target in V5_TARGETS:
        manifest = build_study_manifest(target, registered_at=NOW)
        serialized = json.dumps(manifest, sort_keys=True)
        design = manifest["design"]
        generation = manifest["generation"]

        assert isinstance(design, dict)
        assert isinstance(generation, dict)
        assert design["replicate_count"] == REPLICATE_COUNT
        assert generation["temperature"] == TEMPERATURE
        assert generation["top_p"] == TOP_P
        assert generation["seed"] is None
        assert generation["reasoning_effort"] is None
        assert generation["stream"] is True
        profiles = manifest["profiles"]
        assert isinstance(profiles, dict)
        structured = profiles["structured"]
        assert isinstance(structured, dict)
        assert structured["expected_observation_values"] == ["A", "B"]
        assert structured["profile_id"] == "diagnosis-assertion-v0.3.0"
        assert target.api_key_env not in serialized
        assert target.base_url not in serialized
        assert "Authorization" not in serialized


def test_manifest_set_is_atomic_exact_and_recomputable(tmp_path: Path) -> None:
    directory = tmp_path / "manifests"

    paths = write_study_manifests(directory, registered_at=NOW)
    manifests = validate_study_manifests(directory)

    assert len(paths) == len(manifests) == 3
    assert {path.name for path in paths} == {
        target.manifest_filename for target in STUDY_TARGETS
    }
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o644 for path in paths)
    assert all(
        manifest["registered_at"] == "2026-07-24T20:00:00Z" for manifest in manifests
    )


def test_manifest_writer_never_overwrites_existing_directory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "manifests"
    directory.mkdir()
    marker = directory / "owner-data.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(FileExistsError):
        write_study_manifests(directory, registered_at=NOW)

    assert marker.read_text(encoding="utf-8") == "preserve"


def test_manifest_validator_rejects_incomplete_target_set(tmp_path: Path) -> None:
    directory = tmp_path / "manifests"
    paths = write_study_manifests(directory, registered_at=NOW)
    paths[0].unlink()

    with pytest.raises(
        ComparisonManifestValidationError,
        match="exact target set",
    ):
        validate_study_manifests(directory)


def test_v1_manifest_set_remains_recomputable(tmp_path: Path) -> None:
    directory = tmp_path / "manifests-v1"

    paths = write_study_manifests(
        directory,
        registered_at=NOW,
        study_version="1",
    )
    manifests = validate_study_manifests(directory, study_version="1")

    assert len(paths) == len(manifests) == 6
    for manifest in manifests:
        generation = manifest["generation"]
        assert isinstance(generation, dict)
        assert "stream" not in generation
    assert {target.model_family for target in STUDY_TARGETS_BY_VERSION["1"]} == {
        "GLM 5.2",
        "MiniMax M3",
        "Qwen 3.7 Max",
    }


def test_v2_manifest_set_remains_recomputable_without_outcome_enum(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "manifests-v2"

    paths = write_study_manifests(
        directory,
        registered_at=NOW,
        study_version="2",
    )
    manifests = validate_study_manifests(directory, study_version="2")

    assert len(paths) == len(manifests) == 5
    for manifest in manifests:
        profiles = manifest["profiles"]
        assert isinstance(profiles, dict)
        structured = profiles["structured"]
        assert isinstance(structured, dict)
        assert "expected_observation_values" not in structured
        assert structured["profile_id"] == "diagnosis-assertion-v0.2.0"


def test_v3_manifest_set_remains_recomputable_after_aborted_run(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "manifests-v3"

    paths = write_study_manifests(
        directory,
        registered_at=NOW,
        study_version="3",
    )
    manifests = validate_study_manifests(directory, study_version="3")

    assert len(paths) == len(manifests) == 4
    for manifest in manifests:
        profiles = manifest["profiles"]
        assert isinstance(profiles, dict)
        structured = profiles["structured"]
        assert isinstance(structured, dict)
        assert structured["expected_observation_values"] == ["A", "B"]
        assert structured["profile_id"] == "diagnosis-assertion-v0.3.0"
        limitations = manifest["limitations"]
        assert isinstance(limitations, list)
        assert all(
            "V3 stopped" not in limitation
            for limitation in limitations
            if isinstance(limitation, str)
        )


def test_v4_manifest_set_remains_recomputable_after_mixed_provider_run(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "manifests-v4"

    paths = write_study_manifests(
        directory,
        registered_at=NOW,
        study_version="4",
    )
    manifests = validate_study_manifests(directory, study_version="4")

    assert len(paths) == len(manifests) == 4
    assert {target.provider for target in STUDY_TARGETS_BY_VERSION["4"]} == {
        "Together AI",
        "Fireworks AI",
    }


def test_v5_manifest_set_remains_recomputable(tmp_path: Path) -> None:
    directory = tmp_path / "manifests-v5"

    paths = write_study_manifests(directory, registered_at=NOW, study_version="5")
    manifests = validate_study_manifests(directory, study_version="5")

    assert [path.name for path in paths] == [
        f"{target_id}.json" for target_id in V5_TARGET_ORDER
    ]
    assert [manifest["manifest_version"] for manifest in manifests] == ["0.2.0"] * 3


def test_manifest_validator_rejects_non_file_entries(tmp_path: Path) -> None:
    directory = tmp_path / "manifests"
    write_study_manifests(directory, registered_at=NOW)
    (directory / "unexpected").mkdir()

    with pytest.raises(
        ComparisonManifestValidationError,
        match="only regular files",
    ):
        validate_study_manifests(directory)


@pytest.mark.parametrize(
    ("checkpoint_status", "checkpoint_url", "license_name"),
    [
        ("public_weights", None, None),
        ("hosted_only", "https://weights.example/model", "MIT"),
    ],
)
def test_target_provenance_classification_is_internally_consistent(
    checkpoint_status: Literal["public_weights", "hosted_only"],
    checkpoint_url: str | None,
    license_name: str | None,
) -> None:
    with pytest.raises(ValueError):
        ComparisonStudyTarget(
            study_version="2",
            target_id="invalid-target",
            provider="Example",
            model_family="Example",
            model_id="example/model",
            base_url="https://inference.example/v1",
            api_key_env="EXAMPLE_API_KEY",
            checkpoint_status=checkpoint_status,
            checkpoint_url=checkpoint_url,
            license_name=license_name,
            stream=True,
        )


def test_manifest_registration_requires_timezone_aware_timestamp(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        write_study_manifests(
            tmp_path / "manifests",
            registered_at=datetime(2026, 7, 24, 20, 0),
        )

    assert not (tmp_path / "manifests").exists()
    assert not tuple(tmp_path.glob(".manifests.*"))


@pytest.mark.parametrize("study_version", tuple(STUDY_TARGETS_BY_VERSION))
def test_committed_manifests_pass_check(study_version: str) -> None:
    directory = COMMITTED_MANIFESTS / f"v{study_version}"

    manifests = validate_study_manifests(directory, study_version=study_version)

    assert len(manifests) == len(STUDY_TARGETS_BY_VERSION[study_version])
    assert sorted(path.name for path in directory.iterdir()) == sorted(
        target.manifest_filename for target in STUDY_TARGETS_BY_VERSION[study_version]
    )


def test_v6_is_the_version_to_register_and_runs_its_targets_in_order() -> None:
    assert STUDY_VERSION == "6"
    assert STUDY_TARGETS == V6_TARGETS
    assert V6_TARGET_ORDER == (
        "together-glm-5p2",
        "together-minimax-m3p1-flash",
        "together-qwen3p7-plus",
    )
    assert tuple(target.target_id for target in V6_TARGETS) == V6_TARGET_ORDER
    assert [
        (
            target.model_family,
            target.model_id,
            target.checkpoint_status,
            target.checkpoint_url,
            target.license_name,
        )
        for target in V6_TARGETS
    ] == [
        (
            "GLM 5.2",
            "zai-org/GLM-5.2",
            "public_weights",
            "https://huggingface.co/zai-org/GLM-5.2",
            "MIT",
        ),
        (
            "MiniMax M3.1 Flash",
            "MiniMaxAI/MiniMax-M3.1-Flash",
            "public_weights",
            "https://huggingface.co/MiniMaxAI/MiniMax-M3.1-Flash",
            "MiniMax Community License",
        ),
        ("Qwen 3.7 Plus", "Qwen/Qwen3.7-Plus", "hosted_only", None, None),
    ]


def test_each_primary_study_uses_its_one_provider_endpoint() -> None:
    assert (
        ProviderEndpoint(
            "Fireworks AI",
            "https://api.fireworks.ai/inference/v1",
            "FIREWORKS_API_KEY",
        )
        == FIREWORKS_ENDPOINT
    )
    assert (
        ProviderEndpoint(
            "Together AI",
            "https://api.together.ai/v1",
            "TOGETHER_API_KEY",
        )
        == TOGETHER_ENDPOINT
    )
    assert dict(PRIMARY_ENDPOINT_BY_VERSION) == {
        "5": FIREWORKS_ENDPOINT,
        "6": TOGETHER_ENDPOINT,
    }
    for study_version, endpoint in PRIMARY_ENDPOINT_BY_VERSION.items():
        assert {
            ProviderEndpoint(target.provider, target.base_url, target.api_key_env)
            for target in STUDY_TARGETS_BY_VERSION[study_version]
        } == {endpoint}


@pytest.mark.parametrize("field", ["provider", "base_url", "api_key_env"])
def test_a_primary_study_off_its_endpoint_is_refused(field: str) -> None:
    target = V6_TARGETS[1]
    moved = {
        "provider": replace(target, provider=FIREWORKS_ENDPOINT.provider),
        "base_url": replace(target, base_url=FIREWORKS_ENDPOINT.base_url),
        "api_key_env": replace(target, api_key_env=FIREWORKS_ENDPOINT.api_key_env),
    }[field]
    targets = (V6_TARGETS[0], moved, V6_TARGETS[2])

    with pytest.raises(RuntimeError, match="one controlled provider endpoint"):
        study_module._check_primary_study(
            targets,
            order=V6_TARGET_ORDER,
            endpoint=TOGETHER_ENDPOINT,
        )


def test_a_primary_study_out_of_its_registered_order_is_refused() -> None:
    with pytest.raises(RuntimeError, match="registered execution order"):
        study_module._check_primary_study(
            tuple(reversed(V6_TARGETS)),
            order=V6_TARGET_ORDER,
            endpoint=TOGETHER_ENDPOINT,
        )


@pytest.mark.parametrize("study_version", ["5", "6"])
def test_the_module_rule_checks_every_primary_study(
    study_version: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    study_module._check_primary_studies()
    targets = STUDY_TARGETS_BY_VERSION[study_version]
    moved = replace(targets[0], base_url="https://inference.example/v1")
    monkeypatch.setattr(
        study_module,
        "STUDY_TARGETS_BY_VERSION",
        {**STUDY_TARGETS_BY_VERSION, study_version: (moved, *targets[1:])},
    )

    with pytest.raises(RuntimeError, match="one controlled provider endpoint"):
        study_module._check_primary_studies()


def test_v6_executes_round_robin_by_replicate_index() -> None:
    expected = tuple(
        (target_id, replicate_index)
        for replicate_index in (1, 2, 3, 4, 5)
        for target_id in V6_TARGET_ORDER
    )

    assert STUDY_EXECUTION_SLOTS_BY_VERSION["6"] == expected
    assert expected == STUDY_EXECUTION_SLOTS
    assert len(expected) == 15
    assert expected[:4] == (
        ("together-glm-5p2", 1),
        ("together-minimax-m3p1-flash", 1),
        ("together-qwen3p7-plus", 1),
        ("together-glm-5p2", 2),
    )


def test_v6_adds_stream_usage_and_a_deadline_to_the_v5_generation_profile() -> None:
    for target in V6_TARGETS:
        endpoint = target.endpoint()

        assert endpoint.timeout_seconds == TIMEOUT_SECONDS == 300.0
        assert endpoint.max_output_tokens == MAX_OUTPUT_TOKENS == 32_768
        assert endpoint.stream is True
        assert endpoint.deadline_seconds == 600.0
        assert target.expected_observation_values == ("A", "B")
        assert endpoint.extra_body == {
            "temperature": TEMPERATURE,
            "top_p": TOP_P,
            "stream_options": {"include_usage": True},
        }


def test_v6_manifests_are_built_under_study_contract_v2() -> None:
    pin = {
        "adapter_id": "openai-compatible-chat-completions",
        "adapter_version": "0.4.0",
    }
    for target in V6_TARGETS:
        manifest = build_study_manifest(target, registered_at=NOW)
        diagnostician = cast(JsonObject, manifest["diagnostician"])
        generation = cast(JsonObject, manifest["generation"])
        execution = cast(JsonObject, manifest["execution"])
        serialized = json.dumps(manifest, sort_keys=True)

        assert manifest["manifest_version"] == "0.3.0"
        assert manifest["comparison_series_id"] == (
            f"case-001-{target.target_id}-comparison-v6"
        )
        assert diagnostician["shipped_adapter"] == pin
        assert diagnostician["model"] == target.model_id
        assert generation == {
            "structured_output_mode": "json_schema_strict",
            "temperature": 1.0,
            "top_p": 0.95,
            "seed": None,
            "reasoning_effort": None,
            "max_output_tokens": 32_768,
            "stream": True,
            "stream_include_usage": True,
        }
        assert execution == {
            "max_attempts_per_model_attempt": 1,
            "timeout_seconds": 300.0,
            "automatic_retry": False,
            "json_repair": False,
            "fallback": False,
            "failure_mode": "stop_after_failed_attempt",
            "deadline_seconds": 600.0,
        }
        assert target.api_key_env not in serialized
        assert target.base_url not in serialized


def test_v6_limitations_record_what_differs_from_v5() -> None:
    limitations = cast(
        list[str],
        build_study_manifest(V6_TARGETS[0], registered_at=NOW)["limitations"],
    )

    assert limitations[-10:] == [
        (
            "Expected observations are constrained to the preregistered revision "
            "codes A and B; this is a harness treatment."
        ),
        (
            "All primary behavioral targets use Together AI through one base "
            "endpoint. Provider is controlled infrastructure retained only as "
            "provenance, not a study factor or behavioral metric."
        ),
        (
            "Cross-provider portability and conformance belong to a separate "
            "engineering study and are not pooled into this behavioral baseline."
        ),
        (
            "Execution is round-robin by replicate index in the registered target "
            "order: together-glm-5p2, together-minimax-m3p1-flash, "
            "together-qwen3p7-plus."
        ),
        (
            "V6 differs from v5 in provider, in one model, and in study contract, "
            "so v5 and v6 results are not pooled or compared."
        ),
        (
            "MiniMax M3.1 Flash takes the MiniMax cell because Together AI did not "
            "serve MiniMax M3 at registration; it is a different model from the "
            "one v5 measured."
        ),
        (
            "Together AI GLM 5.2 was excluded from v3 and v4 after v2 produced four "
            "provider rate-limit failures; v6 includes it, and a rate-limit failure "
            "stays in the record as a failed replicate, not retried or replaced."
        ),
        (
            "Each stream asks the provider to report token usage; a response that "
            "still reports none stops as a response_usage failure."
        ),
        (
            "Each request has a 600-second deadline; a response still arriving "
            "when it passes stops as a transport failure."
        ),
        (
            "V6 is a fresh run; no model output or scorecard from an earlier version "
            "is reused."
        ),
    ]
    hosted = cast(
        list[str],
        build_study_manifest(V6_TARGETS[2], registered_at=NOW)["limitations"],
    )
    assert (
        "No official public Qwen 3.7 Plus checkpoint was verified at registration."
    ) in hosted


def _prompt_payloads(target: ComparisonStudyTarget) -> dict[str, JsonObject]:
    request = study_module._request().to_json_object()
    endpoint = target.endpoint()
    return {
        "narrative": build_structured_output_payload(
            endpoint,
            request,
            narrative_output_profile(),
        ),
        "structured": build_structured_output_payload(
            endpoint,
            request,
            diagnosis_output_profile(target.expected_observation_values),
        ),
    }


def test_v6_prompt_payloads_differ_from_v5_only_by_model_and_stream_usage() -> None:
    for v5_target, v6_target in zip(V5_TARGETS, V6_TARGETS, strict=True):
        v5_payloads = _prompt_payloads(v5_target)
        v6_payloads = _prompt_payloads(v6_target)
        profiles = {
            version: cast(
                JsonObject,
                build_study_manifest(target, registered_at=NOW)["profiles"],
            )
            for version, target in (("5", v5_target), ("6", v6_target))
        }
        for name in ("narrative", "structured"):
            v5_payload = v5_payloads[name]
            v6_payload = v6_payloads[name]
            changed = {
                key
                for key in v5_payload.keys() | v6_payload.keys()
                if v5_payload.get(key) != v6_payload.get(key)
            }

            assert changed == {"model", "stream_options"}
            assert v6_payload["model"] == v6_target.model_id
            assert v6_payload["stream_options"] == {"include_usage": True}
            assert "stream_options" not in v5_payload
            for version, payload in (("5", v5_payload), ("6", v6_payload)):
                profile = cast(JsonObject, profiles[version][name])
                assert profile["prompt_payload_sha256"] == json_sha256(payload)


_PIN_PROBE = """
import json
import sys
from datetime import UTC, datetime

import itself.inference as shipped

shipped.ADAPTER_VERSION = "9.9.9"
from experiments.cases import cache_key_comparison_study as study

directory = sys.argv[1]
study.write_study_manifests(
    directory, registered_at=datetime(2026, 7, 24, tzinfo=UTC), study_version="6"
)
manifests = study.validate_study_manifests(directory, study_version="6")
pins = {json.dumps(m["diagnostician"]["shipped_adapter"]) for m in manifests}
sys.stdout.write(json.dumps(sorted(pins)))
"""


def test_v6_pins_the_shipped_client_as_a_literal(tmp_path: Path) -> None:
    # A process whose shipped client reports another version from the start
    # still builds and checks the v6 registration with the pinned version.
    completed = subprocess.run(
        [sys.executable, "-c", _PIN_PROBE, str(tmp_path / "v6")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(completed.stdout) == [
        json.dumps(
            ShippedAdapterIdentity(
                adapter_id="openai-compatible-chat-completions",
                adapter_version="0.4.0",
            ).to_json_object()
        )
    ]


def test_v6_checks_while_the_running_client_reports_another_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "v6"
    write_study_manifests(directory, registered_at=NOW, study_version="6")
    monkeypatch.setattr(shipped_inference, "ADAPTER_VERSION", "9.9.9")

    assert len(validate_study_manifests(directory, study_version="6")) == 3


def test_v6_registers_and_checks_from_the_command_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = tmp_path / "v6"
    registered_at = "2026-11-26T12:00:00Z"

    assert (
        main(
            [
                "register",
                "--output-directory",
                str(directory),
                "--registered-at",
                registered_at,
                "--study-version",
                "6",
            ]
        )
        == 0
    )
    registered = json.loads(capsys.readouterr().out)
    assert main(["check", "--directory", str(directory), "--study-version", "6"]) == 0
    checked = json.loads(capsys.readouterr().out)

    assert registered == {
        "manifest_count": 3,
        "output_directory": str(directory),
        "study_version": "6",
    }
    assert checked == {
        "manifest_count": 3,
        "directory": str(directory),
        "study_version": "6",
    }
    assert sorted(path.name for path in directory.iterdir()) == sorted(
        f"{target_id}.json" for target_id in V6_TARGET_ORDER
    )
    with pytest.raises(ComparisonManifestValidationError, match="exact target set"):
        validate_study_manifests(directory, study_version="5")


def test_the_command_line_registers_and_checks_v6_by_default(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = tmp_path / "default"

    assert (
        main(
            [
                "register",
                "--output-directory",
                str(directory),
                "--registered-at",
                "2026-11-26T12:00:00Z",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["study_version"] == "6"
    assert main(["check", "--directory", str(directory)]) == 0
    assert json.loads(capsys.readouterr().out)["study_version"] == "6"
