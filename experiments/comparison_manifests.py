# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Validated manifests for paired two-attempt comparative model trials."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from experiments._contract_values import integer_value as _integer
from experiments._contract_values import number_value as _number
from experiments._contract_values import object_value as _object
from experiments._contract_values import (
    optional_integer_value as _optional_integer,
)
from experiments._contract_values import optional_number_value as _optional_number
from experiments._contract_values import string_value as _string
from experiments._contract_values import string_values as _strings
from experiments.cases.cache_key_comparison import (
    COMPARISON_METRICS,
    NARRATIVE_ATTEMPT_ID,
    STRUCTURED_ATTEMPT_ID,
)
from experiments.diagnostician import CONTRACT_VERSION, DiagnosisRequest
from experiments.model_adapters import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    PROTOCOL_ID,
    OpenAIChatEndpoint,
    StructuredOutputProfile,
    build_structured_output_payload,
    diagnosis_output_profile,
)
from experiments.narrative_diagnostician import (
    NARRATIVE_CONTRACT_VERSION,
    narrative_assertion_schema,
    narrative_output_profile,
)
from experiments.trial_manifests import json_sha256, text_sha256
from itself import JsonObject, JsonValue, StrPath
from itself._json import format_json_path as _format_path
from itself._json import strict_json_loads

COMPARISON_MANIFEST_VERSION: Final = "0.2.0"

_CONTRACT_ROOT: Final = Path(__file__).resolve().parent / "contracts" / "v1"
_MANIFEST_SCHEMA_PATH: Final = _CONTRACT_ROOT / "comparison-manifest.schema.json"
_SUPPORTED_REQUEST_OPTIONS: Final = frozenset(
    {"reasoning_effort", "seed", "temperature", "top_p"}
)
_REASONING_EFFORTS: Final = frozenset({"low", "medium", "high"})


class _SchemaValidator(Protocol):
    """Typed surface used from the partially typed jsonschema dependency."""

    def iter_errors(self, instance: JsonValue) -> Iterator[ValidationError]: ...


class ComparisonManifestValidationError(ValueError):
    """Raised when a comparison manifest is invalid or execution-unbound."""


class ComparisonManifestFormatError(ValueError):
    """Raised when a comparison manifest is not one strict JSON object."""

    def __init__(self, path: Path, detail: str) -> None:
        self.path: Path = path
        self.detail: str = detail
        super().__init__(f"{path}: {detail}")


def load_comparison_manifest(path: StrPath) -> JsonObject:
    """Load one strict manifest without duplicate keys or nonstandard numbers."""

    source_path = Path(path)
    try:
        source = source_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ComparisonManifestFormatError(
            source_path,
            f"manifest is not valid UTF-8: {error}",
        ) from error
    try:
        value = strict_json_loads(source)
    except (json.JSONDecodeError, ValueError) as error:
        raise ComparisonManifestFormatError(
            source_path,
            f"invalid JSON: {error}",
        ) from error
    if not isinstance(value, dict):
        raise ComparisonManifestFormatError(
            source_path,
            "manifest must contain one JSON object",
        )
    return value


def _optional_reasoning_effort(value: JsonValue, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _REASONING_EFFORTS:
        raise TypeError(f"{field} must be low, medium, high, or null")
    return value


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("registered_at must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _structured_expected_observation_values(
    profile: JsonObject,
) -> tuple[str, ...]:
    value = profile.get("expected_observation_values")
    if value is None:
        return ()
    return _strings(value, "profiles.structured.expected_observation_values")


def build_comparison_manifest(
    endpoint: OpenAIChatEndpoint,
    request: DiagnosisRequest,
    *,
    comparison_series_id: str,
    registered_at: datetime,
    replicate_count: int,
    study_phase: str,
    limitations: Sequence[str],
    expected_observation_values: Sequence[str] = (),
) -> JsonObject:
    """Build and execution-bind one credential-free comparison manifest."""

    narrative_output = narrative_output_profile()
    constrained_values = tuple(expected_observation_values)
    structured_output = diagnosis_output_profile(constrained_values)
    generation: JsonObject = {
        "structured_output_mode": "json_schema_strict",
        "temperature": _optional_number(
            endpoint.extra_body.get("temperature"),
            "temperature",
        ),
        "top_p": _optional_number(endpoint.extra_body.get("top_p"), "top_p"),
        "seed": _optional_integer(endpoint.extra_body.get("seed"), "seed"),
        "reasoning_effort": _optional_reasoning_effort(
            endpoint.extra_body.get("reasoning_effort"),
            "reasoning_effort",
        ),
        "max_output_tokens": endpoint.max_output_tokens,
    }
    if endpoint.stream:
        generation["stream"] = True
    structured_profile: JsonObject = {
        "attempt_id": STRUCTURED_ATTEMPT_ID,
        "profile_id": structured_output.profile_id,
        "assertion_schema_sha256": json_sha256(structured_output.schema_json()),
        "prompt_payload_sha256": json_sha256(
            build_structured_output_payload(
                endpoint,
                request.to_json_object(),
                structured_output,
            )
        ),
    }
    if constrained_values:
        observation_values: list[JsonValue] = [value for value in constrained_values]
        structured_profile["expected_observation_values"] = observation_values
    manifest: JsonObject = {
        "manifest_version": COMPARISON_MANIFEST_VERSION,
        "comparison_series_id": comparison_series_id,
        "study_phase": study_phase,
        "registered_at": _timestamp(registered_at),
        "case": {
            "case_id": request.case_id,
            "case_version": "1",
            "diagnosis_contract_version": CONTRACT_VERSION,
            "narrative_contract_version": NARRATIVE_CONTRACT_VERSION,
            "request_sha256": json_sha256(request.to_json_object()),
        },
        "design": {
            "replicate_count": replicate_count,
            "model_attempt_count": 2,
            "condition_branch_count": 3,
            "call_order": [
                NARRATIVE_ATTEMPT_ID,
                STRUCTURED_ATTEMPT_ID,
            ],
            "branches": {
                "narrative_diagnosis": {
                    "source_attempt_id": NARRATIVE_ATTEMPT_ID,
                    "executes_external_test": False,
                },
                "structured_without_testing": {
                    "source_attempt_id": STRUCTURED_ATTEMPT_ID,
                    "executes_external_test": False,
                },
                "evidence_enforced": {
                    "source_attempt_id": STRUCTURED_ATTEMPT_ID,
                    "executes_external_test": True,
                },
            },
        },
        "diagnostician": {
            "actor_id": endpoint.actor_id,
            "protocol": PROTOCOL_ID,
            "adapter_id": ADAPTER_ID,
            "adapter_version": ADAPTER_VERSION,
            "base_url_sha256": text_sha256(endpoint.base_url),
            "model": endpoint.model,
        },
        "profiles": {
            "narrative": {
                "attempt_id": NARRATIVE_ATTEMPT_ID,
                "profile_id": narrative_output.profile_id,
                "assertion_schema_sha256": json_sha256(narrative_assertion_schema()),
                "prompt_payload_sha256": json_sha256(
                    build_structured_output_payload(
                        endpoint,
                        request.to_json_object(),
                        narrative_output,
                    )
                ),
            },
            "structured": structured_profile,
        },
        "generation": generation,
        "execution": {
            "max_attempts_per_model_attempt": 1,
            "timeout_seconds": endpoint.timeout_seconds,
            "automatic_retry": False,
            "json_repair": False,
            "fallback": False,
            "failure_mode": "stop_after_failed_attempt",
        },
        "artifact_policy": {
            "raw_provider_response": "private",
            "published_representation": "metadata_and_digest",
            "credential": "environment_only",
            "private_reasoning": "not_requested",
        },
        "metrics": list(COMPARISON_METRICS),
        "limitations": list(limitations),
    }
    ComparisonManifestValidator().validate_against(manifest, endpoint, request)
    return manifest


class ComparisonManifestValidator:
    """Validate shape and bind a comparison to two exact model payloads."""

    def __init__(self) -> None:
        schema_value = cast(
            JsonValue,
            json.loads(_MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8")),
        )
        if not isinstance(schema_value, dict):
            raise TypeError("comparison manifest schema must contain an object")
        Draft202012Validator.check_schema(schema_value)
        self._validator = cast(
            _SchemaValidator,
            Draft202012Validator(
                schema_value,
                format_checker=FormatChecker(),
            ),
        )

    def errors(self, manifest: JsonValue) -> list[str]:
        """Return stable schema and exact-metric errors."""

        issues = sorted(
            self._validator.iter_errors(manifest),
            key=lambda error: tuple(str(item) for item in error.absolute_path),
        )
        messages = [
            f"{_format_path(tuple(issue.absolute_path))}: {issue.message}"
            for issue in issues
        ]
        if isinstance(manifest, dict):
            metrics = manifest.get("metrics")
            if (
                isinstance(metrics, list)
                and all(isinstance(item, str) for item in metrics)
                and tuple(cast(str, item) for item in metrics) != COMPARISON_METRICS
            ):
                messages.append(
                    "$.metrics: must exactly match the ordered comparison "
                    "scorecard fields"
                )
        return messages

    def validate(self, manifest: JsonValue) -> None:
        """Validate manifest shape and exact metric declaration."""

        issues = self.errors(manifest)
        if issues:
            raise ComparisonManifestValidationError("\n".join(issues))

    def validate_against(
        self,
        manifest: JsonObject,
        endpoint: OpenAIChatEndpoint,
        request: DiagnosisRequest,
    ) -> None:
        """Require one manifest to match both rendered model attempts exactly."""

        self.validate(manifest)
        case = _object(manifest["case"], "case")
        diagnostician = _object(manifest["diagnostician"], "diagnostician")
        profiles = _object(manifest["profiles"], "profiles")
        narrative_profile = _object(profiles["narrative"], "profiles.narrative")
        structured_profile = _object(profiles["structured"], "profiles.structured")
        generation = _object(manifest["generation"], "generation")
        execution = _object(manifest["execution"], "execution")

        issues: list[str] = []
        unsupported_options = sorted(
            set(endpoint.extra_body).difference(_SUPPORTED_REQUEST_OPTIONS)
        )
        if unsupported_options:
            issues.append(
                "endpoint extra_body contains unregistered options: "
                + ", ".join(unsupported_options)
            )
        if endpoint.extra_headers:
            issues.append("endpoint extra_headers must be empty for manifest v0.2.0")
        if "expected_observation_values" in narrative_profile:
            issues.append(
                "profiles.narrative cannot declare expected observation values"
            )

        narrative_output = narrative_output_profile()
        structured_output = diagnosis_output_profile(
            _structured_expected_observation_values(structured_profile)
        )
        expected_pairs: tuple[tuple[str, JsonValue, JsonValue], ...] = (
            ("case.case_id", case["case_id"], request.case_id),
            (
                "case.diagnosis_contract_version",
                case["diagnosis_contract_version"],
                CONTRACT_VERSION,
            ),
            (
                "case.narrative_contract_version",
                case["narrative_contract_version"],
                NARRATIVE_CONTRACT_VERSION,
            ),
            (
                "case.request_sha256",
                case["request_sha256"],
                json_sha256(request.to_json_object()),
            ),
            ("diagnostician.actor_id", diagnostician["actor_id"], endpoint.actor_id),
            ("diagnostician.protocol", diagnostician["protocol"], PROTOCOL_ID),
            ("diagnostician.adapter_id", diagnostician["adapter_id"], ADAPTER_ID),
            (
                "diagnostician.adapter_version",
                diagnostician["adapter_version"],
                ADAPTER_VERSION,
            ),
            (
                "diagnostician.base_url_sha256",
                diagnostician["base_url_sha256"],
                text_sha256(endpoint.base_url),
            ),
            ("diagnostician.model", diagnostician["model"], endpoint.model),
            (
                "profiles.narrative.attempt_id",
                narrative_profile["attempt_id"],
                NARRATIVE_ATTEMPT_ID,
            ),
            (
                "profiles.narrative.profile_id",
                narrative_profile["profile_id"],
                narrative_output.profile_id,
            ),
            (
                "profiles.narrative.assertion_schema_sha256",
                narrative_profile["assertion_schema_sha256"],
                json_sha256(narrative_assertion_schema()),
            ),
            (
                "profiles.narrative.prompt_payload_sha256",
                narrative_profile["prompt_payload_sha256"],
                json_sha256(
                    build_structured_output_payload(
                        endpoint,
                        request.to_json_object(),
                        narrative_output,
                    )
                ),
            ),
            (
                "profiles.structured.attempt_id",
                structured_profile["attempt_id"],
                STRUCTURED_ATTEMPT_ID,
            ),
            (
                "profiles.structured.profile_id",
                structured_profile["profile_id"],
                structured_output.profile_id,
            ),
            (
                "profiles.structured.assertion_schema_sha256",
                structured_profile["assertion_schema_sha256"],
                json_sha256(structured_output.schema_json()),
            ),
            (
                "profiles.structured.prompt_payload_sha256",
                structured_profile["prompt_payload_sha256"],
                json_sha256(
                    build_structured_output_payload(
                        endpoint,
                        request.to_json_object(),
                        structured_output,
                    )
                ),
            ),
            (
                "generation.max_output_tokens",
                generation["max_output_tokens"],
                endpoint.max_output_tokens,
            ),
            (
                "generation.stream",
                generation.get("stream", False),
                endpoint.stream,
            ),
            (
                "execution.timeout_seconds",
                execution["timeout_seconds"],
                endpoint.timeout_seconds,
            ),
        )
        for field, observed, expected in expected_pairs:
            if observed != expected:
                issues.append(f"{field} does not match the bound execution")

        configured_temperature = _optional_number(
            endpoint.extra_body.get("temperature"),
            "temperature",
        )
        manifest_temperature = _optional_number(
            generation["temperature"],
            "temperature",
        )
        if manifest_temperature != configured_temperature:
            issues.append("generation.temperature does not match the bound execution")

        configured_top_p = _optional_number(
            endpoint.extra_body.get("top_p"),
            "top_p",
        )
        manifest_top_p = _optional_number(generation.get("top_p"), "top_p")
        if manifest_top_p != configured_top_p:
            issues.append("generation.top_p does not match the bound execution")

        configured_seed = _optional_integer(endpoint.extra_body.get("seed"), "seed")
        manifest_seed = _optional_integer(generation["seed"], "seed")
        if manifest_seed != configured_seed:
            issues.append("generation.seed does not match the bound execution")

        configured_reasoning_effort = _optional_reasoning_effort(
            endpoint.extra_body.get("reasoning_effort"),
            "reasoning_effort",
        )
        manifest_reasoning_effort = _optional_reasoning_effort(
            generation.get("reasoning_effort"),
            "reasoning_effort",
        )
        if manifest_reasoning_effort != configured_reasoning_effort:
            issues.append(
                "generation.reasoning_effort does not match the bound execution"
            )

        if issues:
            raise ComparisonManifestValidationError("\n".join(issues))


def endpoint_from_comparison_manifest(
    manifest: JsonObject,
    *,
    base_url: str,
    api_key_env: str,
) -> OpenAIChatEndpoint:
    """Construct the only common endpoint authorized by a comparison manifest."""

    ComparisonManifestValidator().validate(manifest)
    diagnostician = _object(manifest["diagnostician"], "diagnostician")
    generation = _object(manifest["generation"], "generation")
    execution = _object(manifest["execution"], "execution")
    extra_body: JsonObject = {}
    temperature = generation["temperature"]
    top_p = generation.get("top_p")
    seed = generation["seed"]
    if temperature is not None:
        extra_body["temperature"] = temperature
    if top_p is not None:
        extra_body["top_p"] = top_p
    if seed is not None:
        extra_body["seed"] = seed
    reasoning_effort = generation.get("reasoning_effort")
    if reasoning_effort is not None:
        extra_body["reasoning_effort"] = reasoning_effort
    return OpenAIChatEndpoint(
        actor_id=_string(diagnostician["actor_id"], "actor_id"),
        base_url=base_url,
        model=_string(diagnostician["model"], "model"),
        api_key_env=api_key_env,
        timeout_seconds=_number(execution["timeout_seconds"], "timeout_seconds"),
        max_output_tokens=_integer(
            generation["max_output_tokens"],
            "max_output_tokens",
        ),
        stream=generation.get("stream", False) is True,
        extra_body=extra_body,
    )


def diagnosis_profile_from_comparison_manifest(
    manifest: JsonObject,
) -> StructuredOutputProfile:
    """Reconstruct the exact structured diagnosis profile bound by a manifest."""

    ComparisonManifestValidator().validate(manifest)
    profiles = _object(manifest["profiles"], "profiles")
    structured = _object(profiles["structured"], "profiles.structured")
    return diagnosis_output_profile(_structured_expected_observation_values(structured))
