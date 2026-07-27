# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Register and verify versioned current-model comparison matrices for case 001."""

from __future__ import annotations

import argparse
import json
import shutil
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, cast

from experiments.comparison_manifests import (
    ComparisonManifestValidationError,
    ComparisonManifestValidator,
    build_comparison_manifest,
    load_comparison_manifest,
)
from experiments.diagnostician import DiagnosisRequest
from experiments.model_adapters import OpenAIChatEndpoint
from experiments.trial_manifests import canonical_json_bytes
from itself import JsonObject, StrPath
from itself._filesystem import publish_path_no_replace, write_file_exclusive

from .cache_key_comparison import build_comparison_request
from .cache_key_omission import CaseEnvironment, Mechanism

STUDY_VERSION: Final = "5"
REPLICATE_COUNT: Final = 5
TEMPERATURE: Final = 1.0
TOP_P: Final = 0.95
MAX_OUTPUT_TOKENS: Final = 32_768
TIMEOUT_SECONDS: Final = 300.0
PRIMARY_BEHAVIORAL_PROVIDER: Final = "Fireworks AI"
PRIMARY_BEHAVIORAL_BASE_URL: Final = "https://api.fireworks.ai/inference/v1"
PRIMARY_BEHAVIORAL_API_KEY_ENV: Final = "FIREWORKS_API_KEY"
V5_TARGET_ORDER: Final = (
    "fireworks-glm-5p2",
    "fireworks-minimax-m3",
    "fireworks-qwen3p7-plus",
)

_GENERAL_LIMITATIONS: Final = (
    "This transparent closed-set case does not establish cross-task transfer.",
    (
        "Five repeated samples characterize behavior on one prompt, not a "
        "population of causal-diagnosis tasks."
    ),
    (
        "Strict JSON Schema output is part of the treatment and may alter or "
        "suppress provider-exposed reasoning."
    ),
    (
        "A provider model identifier does not bind an independently verifiable "
        "checkpoint revision, quantization, chat template, or serving configuration."
    ),
)


@dataclass(frozen=True, slots=True)
class ComparisonStudyTarget:
    """One exact provider/model cell and its public provenance classification."""

    study_version: Literal["1", "2", "3", "4", "5"]
    target_id: str
    provider: str
    model_family: str
    model_id: str
    base_url: str
    api_key_env: str
    checkpoint_status: Literal["public_weights", "hosted_only"]
    checkpoint_url: str | None
    license_name: str | None
    stream: bool
    expected_observation_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.stream) is not bool:
            raise ValueError("stream must be a boolean")
        if any(
            not value or value != value.strip()
            for value in self.expected_observation_values
        ) or len(self.expected_observation_values) != len(
            set(self.expected_observation_values)
        ):
            raise ValueError("expected observation values must be non-empty and unique")
        if self.checkpoint_status == "public_weights":
            if self.checkpoint_url is None or self.license_name is None:
                raise ValueError(
                    "public-weight targets require a checkpoint URL and license"
                )
        elif self.checkpoint_url is not None or self.license_name is not None:
            raise ValueError(
                "hosted-only targets must not claim checkpoint or license metadata"
            )

    @property
    def actor_id(self) -> str:
        """Return the stable model actor identifier bound into its manifest."""

        return f"{self.target_id}-case-001"

    @property
    def comparison_series_id(self) -> str:
        """Return the stable repeated-study series identifier."""

        return f"case-001-{self.target_id}-comparison-v{self.study_version}"

    @property
    def manifest_filename(self) -> str:
        """Return the deterministic manifest filename for this target."""

        return f"{self.target_id}.json"

    def endpoint(self) -> OpenAIChatEndpoint:
        """Construct the public endpoint configuration bound by the manifest."""

        return OpenAIChatEndpoint(
            actor_id=self.actor_id,
            base_url=self.base_url,
            model=self.model_id,
            api_key_env=self.api_key_env,
            timeout_seconds=TIMEOUT_SECONDS,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            stream=self.stream,
            extra_body={
                "temperature": TEMPERATURE,
                "top_p": TOP_P,
            },
        )


def _shared_targets(
    study_version: Literal["1", "2", "3", "4", "5"],
    *,
    stream: bool,
    expected_observation_values: tuple[str, ...] = (),
) -> tuple[ComparisonStudyTarget, ...]:
    glm_checkpoint = "https://huggingface.co/zai-org/GLM-5.2"
    minimax_checkpoint = "https://huggingface.co/MiniMaxAI/MiniMax-M3"
    return (
        ComparisonStudyTarget(
            study_version=study_version,
            target_id="together-glm-5p2",
            provider="Together AI",
            model_family="GLM 5.2",
            model_id="zai-org/GLM-5.2",
            base_url="https://api.together.ai/v1",
            api_key_env="TOGETHER_API_KEY",
            checkpoint_status="public_weights",
            checkpoint_url=glm_checkpoint,
            license_name="MIT",
            stream=stream,
            expected_observation_values=expected_observation_values,
        ),
        ComparisonStudyTarget(
            study_version=study_version,
            target_id="fireworks-glm-5p2",
            provider="Fireworks AI",
            model_family="GLM 5.2",
            model_id="accounts/fireworks/models/glm-5p2",
            base_url="https://api.fireworks.ai/inference/v1",
            api_key_env="FIREWORKS_API_KEY",
            checkpoint_status="public_weights",
            checkpoint_url=glm_checkpoint,
            license_name="MIT",
            stream=stream,
            expected_observation_values=expected_observation_values,
        ),
        ComparisonStudyTarget(
            study_version=study_version,
            target_id="together-minimax-m3",
            provider="Together AI",
            model_family="MiniMax M3",
            model_id="MiniMaxAI/MiniMax-M3",
            base_url="https://api.together.ai/v1",
            api_key_env="TOGETHER_API_KEY",
            checkpoint_status="public_weights",
            checkpoint_url=minimax_checkpoint,
            license_name="MiniMax Community License",
            stream=stream,
            expected_observation_values=expected_observation_values,
        ),
        ComparisonStudyTarget(
            study_version=study_version,
            target_id="fireworks-minimax-m3",
            provider="Fireworks AI",
            model_family="MiniMax M3",
            model_id="accounts/fireworks/models/minimax-m3",
            base_url="https://api.fireworks.ai/inference/v1",
            api_key_env="FIREWORKS_API_KEY",
            checkpoint_status="public_weights",
            checkpoint_url=minimax_checkpoint,
            license_name="MiniMax Community License",
            stream=stream,
            expected_observation_values=expected_observation_values,
        ),
    )


def _v1_targets() -> tuple[ComparisonStudyTarget, ...]:
    return (
        *_shared_targets("1", stream=False),
        ComparisonStudyTarget(
            study_version="1",
            target_id="together-qwen3p7-max",
            provider="Together AI",
            model_family="Qwen 3.7 Max",
            model_id="Qwen/Qwen3.7-Max",
            base_url="https://api.together.ai/v1",
            api_key_env="TOGETHER_API_KEY",
            checkpoint_status="hosted_only",
            checkpoint_url=None,
            license_name=None,
            stream=False,
        ),
        ComparisonStudyTarget(
            study_version="1",
            target_id="fireworks-qwen3p7-max",
            provider="Fireworks AI",
            model_family="Qwen 3.7 Max",
            model_id="accounts/fireworks/models/qwen3p7-max",
            base_url="https://api.fireworks.ai/inference/v1",
            api_key_env="FIREWORKS_API_KEY",
            checkpoint_status="hosted_only",
            checkpoint_url=None,
            license_name=None,
            stream=False,
        ),
    )


def _v2_targets() -> tuple[ComparisonStudyTarget, ...]:
    return (
        *_shared_targets("2", stream=True),
        ComparisonStudyTarget(
            study_version="2",
            target_id="fireworks-qwen3p7-plus",
            provider="Fireworks AI",
            model_family="Qwen 3.7 Plus",
            model_id="accounts/fireworks/models/qwen3p7-plus",
            base_url="https://api.fireworks.ai/inference/v1",
            api_key_env="FIREWORKS_API_KEY",
            checkpoint_status="hosted_only",
            checkpoint_url=None,
            license_name=None,
            stream=True,
        ),
    )


def _categorical_targets(
    study_version: Literal["3", "4", "5"],
) -> tuple[ComparisonStudyTarget, ...]:
    open_weight_targets = tuple(
        target
        for target in _shared_targets(
            study_version,
            stream=True,
            expected_observation_values=("A", "B"),
        )
        if target.target_id != "together-glm-5p2"
    )
    return (
        *open_weight_targets,
        ComparisonStudyTarget(
            study_version=study_version,
            target_id="fireworks-qwen3p7-plus",
            provider="Fireworks AI",
            model_family="Qwen 3.7 Plus",
            model_id="accounts/fireworks/models/qwen3p7-plus",
            base_url="https://api.fireworks.ai/inference/v1",
            api_key_env="FIREWORKS_API_KEY",
            checkpoint_status="hosted_only",
            checkpoint_url=None,
            license_name=None,
            stream=True,
            expected_observation_values=("A", "B"),
        ),
    )


def _v3_targets() -> tuple[ComparisonStudyTarget, ...]:
    return _categorical_targets("3")


def _v4_targets() -> tuple[ComparisonStudyTarget, ...]:
    return _categorical_targets("4")


def _v5_targets() -> tuple[ComparisonStudyTarget, ...]:
    targets_by_id = {target.target_id: target for target in _categorical_targets("5")}
    return tuple(targets_by_id[target_id] for target_id in V5_TARGET_ORDER)


STUDY_TARGETS_BY_VERSION: Final[Mapping[str, tuple[ComparisonStudyTarget, ...]]] = (
    MappingProxyType(
        {
            "1": _v1_targets(),
            "2": _v2_targets(),
            "3": _v3_targets(),
            "4": _v4_targets(),
            "5": _v5_targets(),
        }
    )
)
STUDY_TARGETS: Final = STUDY_TARGETS_BY_VERSION[STUDY_VERSION]
_TARGETS_BY_ID = {target.target_id: target for target in STUDY_TARGETS}
if len(_TARGETS_BY_ID) != len(STUDY_TARGETS):
    raise RuntimeError("comparison study target identifiers must be unique")
if (
    {target.provider for target in STUDY_TARGETS} != {PRIMARY_BEHAVIORAL_PROVIDER}
    or {target.base_url for target in STUDY_TARGETS} != {PRIMARY_BEHAVIORAL_BASE_URL}
    or {target.api_key_env for target in STUDY_TARGETS}
    != {PRIMARY_BEHAVIORAL_API_KEY_ENV}
):
    raise RuntimeError(
        "the primary behavioral study must use one controlled provider endpoint"
    )
TARGET_BY_ID: Final[Mapping[str, ComparisonStudyTarget]] = MappingProxyType(
    _TARGETS_BY_ID
)
STUDY_EXECUTION_SLOTS: Final = tuple(
    (target_id, replicate_index)
    for replicate_index in range(1, REPLICATE_COUNT + 1)
    for target_id in V5_TARGET_ORDER
)


def _study_targets(study_version: str) -> tuple[ComparisonStudyTarget, ...]:
    try:
        return STUDY_TARGETS_BY_VERSION[study_version]
    except KeyError:
        supported = ", ".join(sorted(STUDY_TARGETS_BY_VERSION))
        raise ValueError(
            f"unsupported study version {study_version!r}; expected one of {supported}"
        ) from None


def _request() -> DiagnosisRequest:
    context = CaseEnvironment(
        Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION
    ).diagnostic_context()
    return build_comparison_request(context)


def build_study_manifest(
    target: ComparisonStudyTarget,
    *,
    registered_at: datetime,
) -> JsonObject:
    """Build one exact manifest for a provider/model study cell."""

    limitations: list[str] = list(_GENERAL_LIMITATIONS)
    if target.checkpoint_status == "hosted_only":
        limitations.append(
            f"No official public {target.model_family} checkpoint was verified "
            "at registration."
        )
    if target.study_version in {"3", "4"}:
        limitations.extend(
            (
                (
                    "Expected observations are constrained to the preregistered "
                    "revision codes A and B; this is a harness treatment."
                ),
                (
                    "Together AI GLM 5.2 is excluded after v2 produced four "
                    "provider rate-limit failures and no completed paired replicate."
                ),
            )
        )
    if target.study_version == "4":
        limitations.append(
            "V3 stopped after ten of twenty registered replicates because failed-"
            "attempt receipts did not bind the structured output profile; no v3 "
            "bundle is reused in v4."
        )
    if target.study_version == "5":
        limitations.extend(
            (
                (
                    "Expected observations are constrained to the preregistered "
                    "revision codes A and B; this is a harness treatment."
                ),
                (
                    "All primary behavioral targets use Fireworks AI through one "
                    "base endpoint. Provider is controlled infrastructure retained "
                    "only as provenance, not a study factor or behavioral metric."
                ),
                (
                    "Cross-provider portability and conformance belong to a "
                    "separate engineering study and are not pooled into this "
                    "behavioral baseline."
                ),
                (
                    "Execution is round-robin by replicate index in the registered "
                    "target order: fireworks-glm-5p2, fireworks-minimax-m3, "
                    "fireworks-qwen3p7-plus."
                ),
                ("V5 is a fresh run; no model output or scorecard from v4 is reused."),
            )
        )
    return build_comparison_manifest(
        target.endpoint(),
        _request(),
        comparison_series_id=target.comparison_series_id,
        registered_at=registered_at,
        replicate_count=REPLICATE_COUNT,
        study_phase="preregistered_study",
        limitations=limitations,
        expected_observation_values=target.expected_observation_values,
    )


def _pretty_json_bytes(value: JsonObject) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def write_study_manifests(
    output_directory: StrPath,
    *,
    registered_at: datetime,
    study_version: str = STUDY_VERSION,
) -> tuple[Path, ...]:
    """Generate all manifests in a new directory and publish it atomically."""

    targets = _study_targets(study_version)
    destination = Path(output_directory)
    if not destination.name:
        raise ValueError("manifest destination must name a directory")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
        )
    )
    try:
        for target in targets:
            write_file_exclusive(
                staging / target.manifest_filename,
                _pretty_json_bytes(
                    build_study_manifest(target, registered_at=registered_at)
                ),
                mode=(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH),
            )
        staging.chmod(0o755)
        publish_path_no_replace(staging, destination)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return tuple(destination / target.manifest_filename for target in targets)


def validate_study_manifests(
    directory: StrPath,
    *,
    study_version: str = STUDY_VERSION,
) -> tuple[JsonObject, ...]:
    """Validate one exact versioned study manifest set against current code."""

    targets = _study_targets(study_version)
    root = Path(directory)
    if not root.is_dir() or root.is_symlink():
        raise ComparisonManifestValidationError(
            f"{root}: study manifest path is not a directory"
        )
    entries = tuple(root.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise ComparisonManifestValidationError(
            "study manifest directory may contain only regular files"
        )
    expected_names = {target.manifest_filename for target in targets}
    actual_names = {path.name for path in entries}
    if actual_names != expected_names:
        raise ComparisonManifestValidationError(
            "study manifest directory does not contain the exact target set"
        )

    request = _request()
    manifests: list[JsonObject] = []
    registered_at: str | None = None
    for target in targets:
        manifest = load_comparison_manifest(root / target.manifest_filename)
        ComparisonManifestValidator().validate_against(
            manifest,
            target.endpoint(),
            request,
        )
        observed_registered_at = cast(str, manifest["registered_at"])
        if registered_at is None:
            registered_at = observed_registered_at
        elif observed_registered_at != registered_at:
            raise ComparisonManifestValidationError(
                "study manifests do not share one registration timestamp"
            )
        expected = build_study_manifest(
            target,
            registered_at=_parse_timestamp(observed_registered_at),
        )
        if canonical_json_bytes(manifest) != canonical_json_bytes(expected):
            raise ComparisonManifestValidationError(
                f"{target.target_id}: manifest differs from the registered study"
            )
        manifests.append(manifest)
    return tuple(manifests)


def _parse_timestamp(value: str) -> datetime:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("registration timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    register = commands.add_parser(
        "register",
        help="materialize one exact versioned manifest set",
    )
    register.add_argument("--output-directory", required=True)
    register.add_argument("--registered-at", required=True)
    register.add_argument(
        "--study-version",
        choices=tuple(STUDY_TARGETS_BY_VERSION),
        default=STUDY_VERSION,
    )

    check = commands.add_parser(
        "check",
        help="recompute and validate an existing manifest set",
    )
    check.add_argument("--directory", required=True)
    check.add_argument(
        "--study-version",
        choices=tuple(STUDY_TARGETS_BY_VERSION),
        default=STUDY_VERSION,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Register or independently verify the current study matrix."""

    args = _parser().parse_args(argv)
    command = cast(str, args.command)
    if command == "register":
        paths = write_study_manifests(
            cast(str, args.output_directory),
            registered_at=_parse_timestamp(cast(str, args.registered_at)),
            study_version=cast(str, args.study_version),
        )
        result: JsonObject = {
            "manifest_count": len(paths),
            "output_directory": str(Path(cast(str, args.output_directory))),
            "study_version": cast(str, args.study_version),
        }
    else:
        manifests = validate_study_manifests(
            cast(str, args.directory),
            study_version=cast(str, args.study_version),
        )
        result = {
            "manifest_count": len(manifests),
            "directory": str(Path(cast(str, args.directory))),
            "study_version": cast(str, args.study_version),
        }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
