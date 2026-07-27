# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
import stat
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from experiments.cases.cache_key_comparison_study import (
    MAX_OUTPUT_TOKENS,
    PRIMARY_BEHAVIORAL_API_KEY_ENV,
    PRIMARY_BEHAVIORAL_BASE_URL,
    PRIMARY_BEHAVIORAL_PROVIDER,
    REPLICATE_COUNT,
    STUDY_EXECUTION_SLOTS,
    STUDY_TARGETS,
    STUDY_TARGETS_BY_VERSION,
    STUDY_VERSION,
    TEMPERATURE,
    TIMEOUT_SECONDS,
    TOP_P,
    V5_TARGET_ORDER,
    ComparisonStudyTarget,
    build_study_manifest,
    validate_study_manifests,
    write_study_manifests,
)
from experiments.comparison_manifests import ComparisonManifestValidationError

NOW = datetime(2026, 7, 24, 20, 0, tzinfo=UTC)


def test_current_study_matrix_has_viable_current_model_targets() -> None:
    family_counts = Counter(target.model_family for target in STUDY_TARGETS)
    provider_counts = Counter(target.provider for target in STUDY_TARGETS)

    assert STUDY_VERSION == "5"
    assert len(STUDY_TARGETS) == 3
    assert family_counts == {
        "GLM 5.2": 1,
        "MiniMax M3": 1,
        "Qwen 3.7 Plus": 1,
    }
    assert provider_counts == {PRIMARY_BEHAVIORAL_PROVIDER: 3}
    assert len({target.target_id for target in STUDY_TARGETS}) == 3
    assert len({target.model_id for target in STUDY_TARGETS}) == 3


def test_primary_behavioral_cohort_fixes_provider_as_provenance_only() -> None:
    assert {target.base_url for target in STUDY_TARGETS} == {
        PRIMARY_BEHAVIORAL_BASE_URL
    }
    assert {target.api_key_env for target in STUDY_TARGETS} == {
        PRIMARY_BEHAVIORAL_API_KEY_ENV
    }
    for target in STUDY_TARGETS:
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
    assert tuple(target.target_id for target in STUDY_TARGETS) == V5_TARGET_ORDER
    assert (
        tuple(
            (target_id, replicate_index)
            for replicate_index in range(1, REPLICATE_COUNT + 1)
            for target_id in V5_TARGET_ORDER
        )
        == STUDY_EXECUTION_SLOTS
    )


def test_model_provenance_does_not_misclassify_hosted_qwen_as_open_weight() -> None:
    qwen_targets = [
        target for target in STUDY_TARGETS if target.model_family == "Qwen 3.7 Plus"
    ]
    open_weight_targets = [
        target
        for target in STUDY_TARGETS
        if target.checkpoint_status == "public_weights"
    ]

    assert all(target.checkpoint_status == "hosted_only" for target in qwen_targets)
    assert all(target.checkpoint_url is None for target in qwen_targets)
    assert all(target.license_name is None for target in qwen_targets)
    assert {target.model_family for target in open_weight_targets} == {
        "GLM 5.2",
        "MiniMax M3",
    }


def test_every_target_uses_one_shared_registered_generation_profile() -> None:
    for target in STUDY_TARGETS:
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
    for target in STUDY_TARGETS:
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
