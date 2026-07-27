# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Validated, implementation-bound manifests for controlled model trials."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
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
from experiments.diagnostician import DiagnosisRequest
from experiments.model_adapters import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    PROTOCOL_ID,
    OpenAIChatEndpoint,
    build_chat_completion_payload,
    model_assertion_schema,
)
from itself import JsonObject, JsonValue, StrPath
from itself._json import format_json_path as _format_path
from itself._json import strict_json_loads

TRIAL_MANIFEST_VERSION: Final = "0.2.0"
JSON_CANONICALIZATION: Final = "gxp-json-v1"

_CONTRACT_ROOT: Final = Path(__file__).resolve().parent / "contracts" / "v1"
_MANIFEST_SCHEMA_PATH: Final = _CONTRACT_ROOT / "trial-manifest.schema.json"
_SUPPORTED_REQUEST_OPTIONS: Final = frozenset({"seed", "temperature"})


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


class TrialManifestValidator:
    """Validate manifest shape and bind it to one exact adapter invocation."""

    def __init__(self) -> None:
        schema_value = cast(
            JsonValue,
            json.loads(_MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8")),
        )
        if not isinstance(schema_value, dict):
            raise TypeError("trial manifest schema must contain a JSON object")
        Draft202012Validator.check_schema(schema_value)
        self._validator = cast(
            _SchemaValidator,
            Draft202012Validator(
                schema_value,
                format_checker=FormatChecker(),
            ),
        )

    def errors(self, manifest: JsonValue) -> list[str]:
        """Return stable, human-readable schema errors."""

        issues = sorted(
            self._validator.iter_errors(manifest),
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
        case = _object(manifest["case"], "case")
        diagnostician = _object(manifest["diagnostician"], "diagnostician")
        generation = _object(manifest["generation"], "generation")
        execution = _object(manifest["execution"], "execution")

        unsupported_options = sorted(
            set(endpoint.extra_body).difference(_SUPPORTED_REQUEST_OPTIONS)
        )
        issues: list[str] = []
        if unsupported_options:
            issues.append(
                "endpoint extra_body contains unregistered options: "
                + ", ".join(unsupported_options)
            )
        if endpoint.extra_headers:
            issues.append("endpoint extra_headers must be empty for manifest v0.2.0")

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

        if issues:
            raise TrialManifestValidationError("\n".join(issues))


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
        extra_body=extra_body,
    )
