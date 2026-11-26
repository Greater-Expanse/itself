# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Validated, implementation-bound manifests for controlled model trials."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from jsonschema.exceptions import ValidationError

from experiments._contract_values import integer_value as _integer
from experiments._contract_values import number_value as _number
from experiments._contract_values import object_value as _object
from experiments._contract_values import (
    optional_integer_value as _optional_integer,
)
from experiments._contract_values import optional_number_value as _optional_number
from experiments._contract_values import string_value as _string
from experiments._schema_validation import contract_validator
from experiments._study_contracts import (
    STREAM_USAGE_OPTION,
    V1_CONTRACT,
    V2_CONTRACT,
    StudyContract,
    UnsupportedStudyContractError,
    declared_contract,
    pinned_shipped_adapter,
    running_shipped_adapter_issue,
    stream_usage_request,
    stream_usage_requested,
    v2_execution_issues,
)
from experiments.diagnostician import DiagnosisRequest, DiagnosticianIdentity
from experiments.model_adapters import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    PROTOCOL_ID,
    OpenAIChatEndpoint,
    ShippedAdapterIdentity,
    build_chat_completion_payload,
    model_assertion_schema,
    running_shipped_adapter,
)
from itself import JsonObject, JsonValue, StrPath
from itself._json import format_json_path as _format_path
from itself._json import strict_json_loads

TRIAL_MANIFEST_VERSION: Final = V1_CONTRACT.document_version
JSON_CANONICALIZATION: Final = "gxp-json-v1"

_MANIFEST_SCHEMA: Final = "trial-manifest"
_SUPPORTED_REQUEST_OPTIONS: Final = frozenset({"seed", "temperature"})
# A v2 manifest also registers whether a stream reports its token usage.
_V2_REQUEST_OPTIONS: Final = _SUPPORTED_REQUEST_OPTIONS | {STREAM_USAGE_OPTION}
_PAYLOAD_BASE_URL: Final = "https://payload-rendering.invalid/v1"
_PAYLOAD_API_KEY_ENV: Final = "PAYLOAD_RENDERING_UNUSED"


class _SchemaValidator(Protocol):
    """Typed surface used from the partially typed jsonschema dependency."""

    def iter_errors(self, instance: JsonValue) -> Iterator[ValidationError]: ...


class TrialManifestValidationError(ValueError):
    """Raised when a trial manifest is invalid or not bound to an execution."""


class TrialManifestFormatError(ValueError):
    """Raised when a trial manifest file is not one strict JSON object."""

    def __init__(self, path: Path, detail: str) -> None:
        self.path: Path = path
        self.detail: str = detail
        super().__init__(f"{path}: {detail}")


def canonical_json_bytes(value: JsonValue) -> bytes:
    """Return deterministic UTF-8 JSON bytes under ``gxp-json-v1``."""

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def json_sha256(value: JsonValue) -> str:
    """Return a SHA-256 digest of the canonical JSON representation."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def text_sha256(value: str) -> str:
    """Return a SHA-256 digest of one UTF-8 string."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_trial_manifest(path: StrPath) -> JsonObject:
    """Load one strict JSON manifest without accepting duplicate object keys."""

    source_path = Path(path)
    try:
        source = source_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise TrialManifestFormatError(
            source_path,
            f"manifest is not valid UTF-8: {error}",
        ) from error
    try:
        value = strict_json_loads(source)
    except (json.JSONDecodeError, ValueError) as error:
        raise TrialManifestFormatError(
            source_path,
            f"invalid JSON: {error}",
        ) from error
    if not isinstance(value, dict):
        raise TrialManifestFormatError(
            source_path,
            "manifest must contain one JSON object",
        )
    return value


def trial_manifest_contract(manifest: JsonValue) -> StudyContract:
    """Return the study contract a trial manifest declares, refusing others."""

    try:
        return declared_contract(manifest, "manifest_version")
    except UnsupportedStudyContractError as error:
        raise TrialManifestValidationError(str(error)) from None


class TrialManifestValidator:
    """Validate manifest shape and bind it to one exact adapter invocation.

    The schema is the one that the manifest's ``manifest_version`` declares.
    """

    def errors(self, manifest: JsonValue) -> list[str]:
        """Return stable, human-readable schema errors."""

        try:
            contract = declared_contract(manifest, "manifest_version")
        except UnsupportedStudyContractError as error:
            return [str(error)]
        validator: _SchemaValidator = contract_validator(
            contract.schema_path(_MANIFEST_SCHEMA)
        )
        issues = sorted(
            validator.iter_errors(manifest),
            key=lambda error: tuple(str(item) for item in error.absolute_path),
        )
        return [
            f"{_format_path(tuple(issue.absolute_path))}: {issue.message}"
            for issue in issues
        ]

    def validate(self, manifest: JsonValue) -> None:
        """Validate manifest shape or raise ``TrialManifestValidationError``."""

        issues = self.errors(manifest)
        if issues:
            raise TrialManifestValidationError("\n".join(issues))

    def validate_against(
        self,
        manifest: JsonObject,
        endpoint: OpenAIChatEndpoint,
        request: DiagnosisRequest,
    ) -> None:
        """Require a manifest to match one exact endpoint and rendered request."""

        self.validate(manifest)
        contract = trial_manifest_contract(manifest)
        case = _object(manifest["case"], "case")
        diagnostician = _object(manifest["diagnostician"], "diagnostician")
        generation = _object(manifest["generation"], "generation")
        execution = _object(manifest["execution"], "execution")

        supported_options = (
            _V2_REQUEST_OPTIONS
            if contract is V2_CONTRACT
            else _SUPPORTED_REQUEST_OPTIONS
        )
        unsupported_options = sorted(
            set(endpoint.extra_body).difference(supported_options)
        )
        issues: list[str] = []
        if unsupported_options:
            issues.append(
                "endpoint extra_body contains unregistered options: "
                + ", ".join(unsupported_options)
            )
        if endpoint.extra_headers:
            issues.append(
                "endpoint extra_headers must be empty for manifest "
                f"v{contract.document_version}"
            )
        if contract is V1_CONTRACT and endpoint.deadline_seconds is not None:
            issues.append("endpoint deadline_seconds must be None for manifest v0.2.0")

        expected_pairs: tuple[tuple[str, JsonValue, JsonValue], ...] = (
            ("case.case_id", case["case_id"], request.case_id),
            (
                "case.diagnosis_contract_version",
                case["diagnosis_contract_version"],
                request.contract_version,
            ),
            (
                "case.request_sha256",
                case["request_sha256"],
                json_sha256(request.to_json_object()),
            ),
            (
                "case.assertion_schema_sha256",
                case["assertion_schema_sha256"],
                json_sha256(model_assertion_schema()),
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
                "generation.max_output_tokens",
                generation["max_output_tokens"],
                endpoint.max_output_tokens,
            ),
            (
                "generation.prompt_payload_sha256",
                generation["prompt_payload_sha256"],
                json_sha256(build_chat_completion_payload(endpoint, request)),
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

        configured_seed = _optional_integer(endpoint.extra_body.get("seed"), "seed")
        manifest_seed = _optional_integer(generation["seed"], "seed")
        if manifest_seed != configured_seed:
            issues.append("generation.seed does not match the bound execution")

        if contract is V2_CONTRACT:
            if generation["stream"] != endpoint.stream:
                issues.append("generation.stream does not match the bound execution")
            issues.extend(v2_execution_issues(generation, execution, endpoint))

        if issues:
            raise TrialManifestValidationError("\n".join(issues))

    def validate_recorded(
        self,
        manifest: JsonObject,
        request: DiagnosisRequest,
        diagnostician: DiagnosticianIdentity,
    ) -> None:
        """Require a manifest to match a recorded request and diagnostician.

        A bundle keeps neither the base URL nor the credential, so this binds
        everything they do not determine: the case, the diagnostician that the
        diagnosis names, and the prompt payload that the manifest's own model
        and generation settings render for the request.
        """

        self.validate(manifest)
        case = _object(manifest["case"], "case")
        recorded = _object(manifest["diagnostician"], "diagnostician")
        generation = _object(manifest["generation"], "generation")
        expected_pairs: tuple[tuple[str, JsonValue, JsonValue], ...] = (
            ("case.case_id", case["case_id"], request.case_id),
            (
                "case.diagnosis_contract_version",
                case["diagnosis_contract_version"],
                request.contract_version,
            ),
            (
                "case.request_sha256",
                case["request_sha256"],
                json_sha256(request.to_json_object()),
            ),
            (
                "case.assertion_schema_sha256",
                case["assertion_schema_sha256"],
                json_sha256(model_assertion_schema()),
            ),
            ("diagnostician.actor_id", recorded["actor_id"], diagnostician.actor_id),
            ("diagnostician.protocol", recorded["protocol"], PROTOCOL_ID),
            (
                "diagnostician.adapter_id",
                recorded["adapter_id"],
                diagnostician.adapter_id,
            ),
            (
                "diagnostician.adapter_version",
                recorded["adapter_version"],
                diagnostician.adapter_version,
            ),
            (
                "generation.prompt_payload_sha256",
                generation["prompt_payload_sha256"],
                json_sha256(
                    build_chat_completion_payload(_payload_endpoint(manifest), request)
                ),
            ),
        )
        issues = [
            f"{field} does not match the recorded diagnosis"
            for field, observed, expected in expected_pairs
            if observed != expected
        ]
        if issues:
            raise TrialManifestValidationError("\n".join(issues))


def _payload_endpoint(manifest: JsonObject) -> OpenAIChatEndpoint:
    # The rendered payload depends on neither the base URL nor the credential,
    # so these placeholders reproduce the payload of the registered endpoint.
    return endpoint_from_trial_manifest(
        manifest,
        base_url=_PAYLOAD_BASE_URL,
        api_key_env=_PAYLOAD_API_KEY_ENV,
    )


def validate_trial_manifest_against(
    manifest: JsonObject,
    endpoint: OpenAIChatEndpoint,
    request: DiagnosisRequest,
) -> None:
    """Validate and bind one manifest with a fresh validator."""

    TrialManifestValidator().validate_against(manifest, endpoint, request)


def endpoint_from_trial_manifest(
    manifest: JsonObject,
    *,
    base_url: str,
    api_key_env: str,
) -> OpenAIChatEndpoint:
    """Construct the only endpoint configuration authorized by a manifest."""

    TrialManifestValidator().validate(manifest)
    contract = trial_manifest_contract(manifest)
    diagnostician = _object(manifest["diagnostician"], "diagnostician")
    generation = _object(manifest["generation"], "generation")
    execution = _object(manifest["execution"], "execution")
    extra_body: JsonObject = {}
    temperature = generation["temperature"]
    seed = generation["seed"]
    if temperature is not None:
        extra_body["temperature"] = temperature
    if seed is not None:
        extra_body["seed"] = seed
    stream = False
    deadline_seconds: float | None = None
    if contract is V2_CONTRACT:
        stream = generation["stream"] is True
        if generation["stream_include_usage"] is True:
            extra_body[STREAM_USAGE_OPTION] = stream_usage_request()
        deadline_seconds = _optional_number(
            execution["deadline_seconds"],
            "deadline_seconds",
        )
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
        stream=stream,
        extra_body=extra_body,
        deadline_seconds=deadline_seconds,
    )


def require_running_shipped_adapter(manifest: JsonObject) -> None:
    """Refuse a v0.3.0 trial whose pinned shipped client is not the running one.

    A v0.2.0 manifest pins no shipped client, so it is refused here too.
    """

    TrialManifestValidator().validate(manifest)
    if trial_manifest_contract(manifest) is not V2_CONTRACT:
        raise TrialManifestValidationError(
            "only a v0.3.0 trial manifest pins the shipped client"
        )
    issue = running_shipped_adapter_issue(pinned_shipped_adapter(manifest))
    if issue is not None:
        raise TrialManifestValidationError(issue)


def _registration_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("registered_at must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_trial_manifest(
    endpoint: OpenAIChatEndpoint,
    request: DiagnosisRequest,
    *,
    trial_series_id: str,
    registered_at: datetime,
    study_phase: str,
    metrics: Sequence[str],
    limitations: Sequence[str],
    trial_count: int = 1,
    shipped_adapter: ShippedAdapterIdentity | None = None,
) -> JsonObject:
    """Build and execution-bind one credential-free v0.3.0 trial manifest.

    The manifest pins ``shipped_adapter``, or the running shipped client when
    it is omitted, and records the endpoint's stream, whether it asks for
    token usage on a stream, and its request deadline.
    """

    pinned = running_shipped_adapter() if shipped_adapter is None else shipped_adapter
    metric_values: list[JsonValue] = [metric for metric in metrics]
    limitation_values: list[JsonValue] = [limitation for limitation in limitations]
    manifest: JsonObject = {
        "manifest_version": V2_CONTRACT.document_version,
        "trial_series_id": trial_series_id,
        "study_phase": study_phase,
        "registered_at": _registration_time(registered_at),
        "case": {
            "case_id": request.case_id,
            "case_version": "1",
            "diagnosis_contract_version": request.contract_version,
            "request_sha256": json_sha256(request.to_json_object()),
            "assertion_schema_sha256": json_sha256(model_assertion_schema()),
        },
        "condition": "evidence_enforced",
        "diagnostician": {
            "actor_id": endpoint.actor_id,
            "protocol": PROTOCOL_ID,
            "adapter_id": ADAPTER_ID,
            "adapter_version": ADAPTER_VERSION,
            "shipped_adapter": pinned.to_json_object(),
            "base_url_sha256": text_sha256(endpoint.base_url),
            "model": endpoint.model,
        },
        "generation": {
            "structured_output_mode": "json_schema_strict",
            "temperature": _optional_number(
                endpoint.extra_body.get("temperature"),
                "temperature",
            ),
            "seed": _optional_integer(endpoint.extra_body.get("seed"), "seed"),
            "max_output_tokens": endpoint.max_output_tokens,
            "stream": endpoint.stream,
            "stream_include_usage": stream_usage_requested(endpoint.extra_body) is True,
            "prompt_payload_sha256": json_sha256(
                build_chat_completion_payload(endpoint, request)
            ),
        },
        "execution": {
            "trial_count": trial_count,
            "max_attempts_per_trial": 1,
            "timeout_seconds": endpoint.timeout_seconds,
            "deadline_seconds": endpoint.deadline_seconds,
            "automatic_retry": False,
            "json_repair": False,
            "fallback": False,
        },
        "artifact_policy": {
            "raw_provider_response": "private",
            "published_representation": "metadata_and_digest",
            "credential": "environment_only",
            "private_reasoning": "not_requested",
        },
        "metrics": metric_values,
        "limitations": limitation_values,
    }
    TrialManifestValidator().validate_against(manifest, endpoint, request)
    return manifest
