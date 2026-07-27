# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Atomic, private, digest-bound bundles for completed controlled trials."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import tempfile
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Final, Protocol, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from experiments._bundle_validation import (
    ValidatedInventory as _ValidatedInventory,
)
from experiments._bundle_validation import bundle_timestamp as _timestamp
from experiments._bundle_validation import file_entry as _shared_file_entry
from experiments._bundle_validation import (
    load_json_object as _shared_load_json_object,
)
from experiments._bundle_validation import (
    normalized_bundle_path as _shared_normalized_bundle_path,
)
from experiments._bundle_validation import pretty_json_bytes as _pretty_json_bytes
from experiments._bundle_validation import sha256_bytes as _sha256_bytes
from experiments._bundle_validation import (
    validate_inventory as _shared_validate_inventory,
)
from experiments._contract_values import object_value as _object
from experiments._contract_values import string_value as _string
from experiments._contract_values import string_values as _strings
from experiments.diagnostician import (
    DiagnosisRequest,
    DiagnosisResult,
    DiagnosticianContractValidator,
)
from experiments.model_adapters import DirectoryArtifactSink, ModelAdapterError
from experiments.trial_manifests import (
    TrialManifestValidator,
    canonical_json_bytes,
    load_trial_manifest,
)
from itself import (
    JsonlLedgerStore,
    JsonObject,
    JsonValue,
    Ledger,
    StrPath,
    build_reasoning_receipt,
)
from itself._filesystem import (
    fsync_directory as _fsync_directory,
)
from itself._filesystem import (
    publish_path_no_replace,
)
from itself._filesystem import (
    write_private_file_exclusive as _write_private_bytes,
)
from itself._json import format_json_path as _format_path
from itself.receipts import (
    JsonReceiptStore,
    ReasoningReceiptValidator,
    canonical_ledger_bytes,
)

TRIAL_BUNDLE_VERSION: Final = "0.2.0"

_CONTRACT_ROOT: Final = Path(__file__).resolve().parent / "contracts" / "v1"
_BUNDLE_SCHEMA_PATH: Final = _CONTRACT_ROOT / "trial-bundle.schema.json"
_ATTEMPT_SCHEMA_PATH: Final = _CONTRACT_ROOT / "trial-attempt-report.schema.json"
_BUNDLE_MANIFEST_NAME: Final = "bundle.json"
_TRIAL_MANIFEST_NAME: Final = "trial-manifest.json"
_ATTEMPT_REPORT_NAME: Final = "attempt-report.json"
_DIAGNOSIS_RESULT_NAME: Final = "diagnosis-result.json"
_LEDGER_NAME: Final = "ledger.jsonl"
_SCORECARD_NAME: Final = "scorecard.json"
_RECEIPT_NAME: Final = "reasoning-receipt.json"
_SHAREABLE_FILES: Final = (
    _TRIAL_MANIFEST_NAME,
    _DIAGNOSIS_RESULT_NAME,
    _LEDGER_NAME,
    _SCORECARD_NAME,
    _RECEIPT_NAME,
)
_SHAREABLE_MEDIA_TYPES: Final = {
    _TRIAL_MANIFEST_NAME: "application/json",
    _DIAGNOSIS_RESULT_NAME: "application/json",
    _LEDGER_NAME: "application/x-ndjson",
    _SCORECARD_NAME: "application/json",
    _RECEIPT_NAME: "application/json",
}


class _SchemaValidator(Protocol):
    """Typed surface used from the partially typed jsonschema dependency."""

    def iter_errors(self, instance: JsonValue) -> Iterator[ValidationError]: ...


class TrialMetrics(Protocol):
    """Non-composite scorecard surface supplied by an experiment runner."""

    def to_json_object(self) -> JsonObject:
        """Return the runner's preregistered metrics."""

        ...


class CompletedTrial(Protocol):
    """Minimal completed-run surface accepted by the generic bundle builder."""

    @property
    def request(self) -> DiagnosisRequest: ...

    @property
    def diagnosis(self) -> DiagnosisResult: ...

    @property
    def ledger(self) -> Ledger: ...

    @property
    def metrics(self) -> TrialMetrics: ...


class TrialBundleValidationError(ValueError):
    """Raised when a trial bundle is structurally or cryptographically invalid."""


class TrialBundleStateError(RuntimeError):
    """Raised when a bundle builder is used outside its one-shot lifecycle."""


def _load_json_object(path: Path) -> JsonObject:
    return _shared_load_json_object(path, error_type=TrialBundleValidationError)


def parse_bundle_relative_path(value: str) -> PurePosixPath:
    """Parse a normalized relative bundle path or reject path smuggling."""

    return _shared_normalized_bundle_path(
        value,
        error_type=TrialBundleValidationError,
    )


def _file_entry(
    root: Path,
    relative_path: str,
    *,
    media_type: str,
    visibility: str,
) -> JsonObject:
    return _shared_file_entry(
        root,
        relative_path,
        media_type=media_type,
        visibility=visibility,
        error_type=TrialBundleValidationError,
    )


def _scorecard(result: CompletedTrial, receipt_recomputes: bool) -> JsonObject:
    value = result.metrics.to_json_object()
    usage = result.diagnosis.usage
    value.update(
        {
            "model_input_tokens": usage.model_input_tokens,
            "model_output_tokens": usage.model_output_tokens,
            "wall_time_ms": usage.wall_time_ms,
            "receipt_recomputes": receipt_recomputes,
        }
    )
    return value


@dataclass(frozen=True, slots=True)
class VerifiedTrialBundle:
    """Validated projections loaded from one complete trial bundle."""

    path: Path
    bundle_manifest: JsonObject
    trial_manifest: JsonObject
    diagnosis: DiagnosisResult
    ledger: Ledger
    scorecard: JsonObject
    receipt: JsonObject


@dataclass(frozen=True, slots=True)
class VerifiedFailedTrialBundle:
    """Validated records loaded from one failed model-adapter attempt."""

    path: Path
    bundle_manifest: JsonObject
    trial_manifest: JsonObject
    attempt_report: JsonObject


class TrialBundleValidator:
    """Validate file inventory, digests, and cross-document bundle bindings."""

    def __init__(self) -> None:
        schema_value = cast(
            JsonValue,
            json.loads(_BUNDLE_SCHEMA_PATH.read_text(encoding="utf-8")),
        )
        if not isinstance(schema_value, dict):
            raise TypeError("trial bundle schema must contain a JSON object")
        Draft202012Validator.check_schema(schema_value)
        self._validator = cast(
            _SchemaValidator,
            Draft202012Validator(
                schema_value,
                format_checker=FormatChecker(),
            ),
        )
        attempt_schema_value = cast(
            JsonValue,
            json.loads(_ATTEMPT_SCHEMA_PATH.read_text(encoding="utf-8")),
        )
        if not isinstance(attempt_schema_value, dict):
            raise TypeError("trial attempt report schema must contain a JSON object")
        Draft202012Validator.check_schema(attempt_schema_value)
        self._attempt_validator = cast(
            _SchemaValidator,
            Draft202012Validator(
                attempt_schema_value,
                format_checker=FormatChecker(),
            ),
        )

    def errors(self, bundle_manifest: JsonValue) -> list[str]:
        """Return stable, human-readable bundle-manifest schema errors."""

        issues = sorted(
            self._validator.iter_errors(bundle_manifest),
            key=lambda error: tuple(str(item) for item in error.absolute_path),
        )
        return [
            f"{_format_path(tuple(issue.absolute_path))}: {issue.message}"
            for issue in issues
        ]

    def validate_manifest(self, bundle_manifest: JsonValue) -> None:
        """Validate the bundle-manifest shape."""

        issues = self.errors(bundle_manifest)
        if issues:
            raise TrialBundleValidationError("\n".join(issues))

    def validate_attempt_report(self, attempt_report: JsonValue) -> None:
        """Validate one sanitized failed-attempt report."""

        issues = sorted(
            self._attempt_validator.iter_errors(attempt_report),
            key=lambda error: tuple(str(item) for item in error.absolute_path),
        )
        if issues:
            messages = [
                f"{_format_path(tuple(issue.absolute_path))}: {issue.message}"
                for issue in issues
            ]
            raise TrialBundleValidationError("\n".join(messages))

    def _validate_inventory(self, root: Path) -> _ValidatedInventory:
        return _shared_validate_inventory(
            root,
            manifest_name=_BUNDLE_MANIFEST_NAME,
            validate_manifest=self.validate_manifest,
            error_type=TrialBundleValidationError,
        )

    def validate_completed(
        self,
        path: StrPath,
        request: DiagnosisRequest,
    ) -> VerifiedTrialBundle:
        """Verify every byte and semantic cross-reference in a completed bundle."""

        root = Path(path)
        inventory = self._validate_inventory(root)
        bundle_manifest = inventory.bundle_manifest
        file_entries = inventory.file_entries
        entry_by_path = inventory.entry_by_path
        inventory_paths = set(inventory.paths)
        if bundle_manifest["outcome"] != "completed":
            raise TrialBundleValidationError("bundle outcome is not completed")

        for required in _SHAREABLE_FILES:
            if required not in inventory_paths:
                raise TrialBundleValidationError(
                    f"completed bundle is missing required file {required!r}"
                )
        for required, media_type in _SHAREABLE_MEDIA_TYPES.items():
            entry = entry_by_path[required]
            if entry["visibility"] != "shareable":
                raise TrialBundleValidationError(
                    f"completed bundle file {required!r} must be shareable"
                )
            if entry["media_type"] != media_type:
                raise TrialBundleValidationError(
                    f"completed bundle file {required!r} has the wrong media type"
                )

        trial_manifest = load_trial_manifest(root / _TRIAL_MANIFEST_NAME)
        TrialManifestValidator().validate(trial_manifest)
        if bundle_manifest["trial_series_id"] != trial_manifest["trial_series_id"]:
            raise TrialBundleValidationError(
                "bundle trial_series_id does not match its trial manifest"
            )
        if bundle_manifest["condition"] != trial_manifest["condition"]:
            raise TrialBundleValidationError(
                "bundle condition does not match its trial manifest"
            )
        artifact_policy = _object(trial_manifest["artifact_policy"], "artifact_policy")
        if artifact_policy["raw_provider_response"] != "private":
            raise TrialBundleValidationError(
                "completed private bundle requires private raw-response retention"
            )

        ledger = JsonlLedgerStore(root / _LEDGER_NAME).load()
        receipt = JsonReceiptStore(root / _RECEIPT_NAME).load()
        ReasoningReceiptValidator().validate_against_ledger(receipt, ledger)

        diagnosis_value = _load_json_object(root / _DIAGNOSIS_RESULT_NAME)
        diagnosis = DiagnosticianContractValidator().decode_result(
            request,
            diagnosis_value,
        )
        scorecard = _load_json_object(root / _SCORECARD_NAME)
        declared_metrics = set(_strings(trial_manifest["metrics"], "metrics"))
        if set(scorecard) != declared_metrics:
            raise TrialBundleValidationError(
                "scorecard fields do not exactly match preregistered metrics"
            )
        usage = diagnosis.usage
        expected_usage = {
            "model_input_tokens": usage.model_input_tokens,
            "model_output_tokens": usage.model_output_tokens,
            "wall_time_ms": usage.wall_time_ms,
        }
        for field, expected in expected_usage.items():
            if scorecard[field] != expected:
                raise TrialBundleValidationError(
                    f"scorecard {field} does not match diagnosis usage"
                )
        if scorecard["receipt_recomputes"] is not True:
            raise TrialBundleValidationError(
                "scorecard receipt_recomputes must be true"
            )

        artifact_uri = diagnosis.raw_output_artifact.uri
        if artifact_uri not in inventory_paths:
            raise TrialBundleValidationError(
                "diagnosis artifact URI is absent from the bundle inventory"
            )
        allowed_paths = set(_SHAREABLE_FILES) | {artifact_uri}
        if inventory_paths != allowed_paths:
            raise TrialBundleValidationError(
                "completed bundle inventory contains undeclared extra files"
            )
        artifact_entry = next(
            entry
            for entry in file_entries
            if _string(entry["path"], "path") == artifact_uri
        )
        if artifact_entry["visibility"] != "private":
            raise TrialBundleValidationError(
                "raw provider artifact must remain private"
            )
        if artifact_entry["media_type"] != diagnosis.raw_output_artifact.media_type:
            raise TrialBundleValidationError(
                "diagnosis artifact media type does not match bundle inventory"
            )
        artifact_digest = _object(artifact_entry["digest"], "digest")
        if artifact_digest["value"] != diagnosis.raw_output_artifact.sha256:
            raise TrialBundleValidationError(
                "diagnosis artifact digest does not match bundle inventory"
            )

        return VerifiedTrialBundle(
            path=root,
            bundle_manifest=deepcopy(bundle_manifest),
            trial_manifest=deepcopy(trial_manifest),
            diagnosis=diagnosis,
            ledger=ledger,
            scorecard=deepcopy(scorecard),
            receipt=deepcopy(receipt),
        )

    def validate_failure(self, path: StrPath) -> VerifiedFailedTrialBundle:
        """Verify one sanitized, digest-bound failed adapter attempt."""

        root = Path(path)
        inventory = self._validate_inventory(root)
        bundle_manifest = inventory.bundle_manifest
        entry_by_path = inventory.entry_by_path
        inventory_paths = set(inventory.paths)
        if bundle_manifest["outcome"] != "adapter_failed":
            raise TrialBundleValidationError("bundle outcome is not adapter_failed")

        required_paths = {_TRIAL_MANIFEST_NAME, _ATTEMPT_REPORT_NAME}
        if not required_paths.issubset(inventory_paths):
            raise TrialBundleValidationError(
                "failed bundle is missing its manifest or attempt report"
            )
        for required in required_paths:
            entry = entry_by_path[required]
            if entry["visibility"] != "shareable":
                raise TrialBundleValidationError(
                    f"failed bundle file {required!r} must be shareable"
                )
            if entry["media_type"] != "application/json":
                raise TrialBundleValidationError(
                    f"failed bundle file {required!r} has the wrong media type"
                )

        trial_manifest = load_trial_manifest(root / _TRIAL_MANIFEST_NAME)
        TrialManifestValidator().validate(trial_manifest)
        if bundle_manifest["trial_series_id"] != trial_manifest["trial_series_id"]:
            raise TrialBundleValidationError(
                "bundle trial_series_id does not match its trial manifest"
            )
        if bundle_manifest["condition"] != trial_manifest["condition"]:
            raise TrialBundleValidationError(
                "bundle condition does not match its trial manifest"
            )
        artifact_policy = _object(trial_manifest["artifact_policy"], "artifact_policy")
        if artifact_policy["raw_provider_response"] != "private":
            raise TrialBundleValidationError(
                "failed private bundle requires private raw-response retention"
            )

        attempt_report = _load_json_object(root / _ATTEMPT_REPORT_NAME)
        self.validate_attempt_report(attempt_report)
        if attempt_report["trial_series_id"] != trial_manifest["trial_series_id"]:
            raise TrialBundleValidationError(
                "attempt report trial_series_id does not match its trial manifest"
            )
        failure = _object(attempt_report["failure"], "failure")
        artifact_value = failure.get("response_artifact")
        allowed_paths = set(required_paths)
        if artifact_value is None:
            if bundle_manifest["contains_private_artifacts"] is not False:
                raise TrialBundleValidationError(
                    "failed bundle incorrectly declares private artifacts"
                )
        else:
            artifact = _object(artifact_value, "response_artifact")
            artifact_uri = _string(artifact["uri"], "response_artifact.uri")
            allowed_paths.add(artifact_uri)
            if bundle_manifest["contains_private_artifacts"] is not True:
                raise TrialBundleValidationError(
                    "failed bundle omits its private-artifact declaration"
                )
            if artifact_uri not in inventory_paths:
                raise TrialBundleValidationError(
                    "failed response artifact is absent from the bundle inventory"
                )
            entry = entry_by_path[artifact_uri]
            if entry["visibility"] != "private":
                raise TrialBundleValidationError(
                    "failed provider response artifact must remain private"
                )
            if entry["media_type"] != artifact["media_type"]:
                raise TrialBundleValidationError(
                    "failed response artifact media type does not match inventory"
                )
            digest = _object(artifact["digest"], "response_artifact.digest")
            entry_digest = _object(entry["digest"], "digest")
            if entry_digest["value"] != digest["value"]:
                raise TrialBundleValidationError(
                    "failed response artifact digest does not match inventory"
                )

        if inventory_paths != allowed_paths:
            raise TrialBundleValidationError(
                "failed bundle inventory contains undeclared extra files"
            )
        return VerifiedFailedTrialBundle(
            path=root,
            bundle_manifest=deepcopy(bundle_manifest),
            trial_manifest=deepcopy(trial_manifest),
            attempt_report=deepcopy(attempt_report),
        )

    def validate(
        self,
        path: StrPath,
        request: DiagnosisRequest,
    ) -> VerifiedTrialBundle | VerifiedFailedTrialBundle:
        """Validate a completed or failed bundle according to its declared outcome."""

        root = Path(path)
        bundle_manifest = _load_json_object(root / _BUNDLE_MANIFEST_NAME)
        self.validate_manifest(bundle_manifest)
        outcome = bundle_manifest["outcome"]
        if outcome == "completed":
            return self.validate_completed(root, request)
        if outcome == "adapter_failed":
            return self.validate_failure(root)
        raise TrialBundleValidationError(f"unsupported bundle outcome {outcome!r}")


class TrialBundleBuilder:
    """Build one immutable bundle in a private sibling directory, then rename."""

    def __init__(
        self,
        destination: StrPath,
        trial_manifest: JsonObject,
        *,
        now: datetime,
    ) -> None:
        self.destination: Path = Path(destination)
        if not self.destination.name:
            raise ValueError("bundle destination must name a new directory")
        TrialManifestValidator().validate(trial_manifest)
        artifact_policy = _object(trial_manifest["artifact_policy"], "artifact_policy")
        if artifact_policy["raw_provider_response"] != "private":
            raise TrialBundleValidationError(
                "bundle builder requires private raw-response retention"
            )
        self._trial_manifest: JsonObject = deepcopy(trial_manifest)
        self._created_at = _timestamp(now)
        self._staging: Path | None = None
        self._published = False

    def __enter__(self) -> TrialBundleBuilder:
        if self._staging is not None or self._published:
            raise TrialBundleStateError("bundle builder cannot be entered twice")
        if self.destination.exists():
            raise FileExistsError(self.destination)
        parent = self.destination.parent
        parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                dir=parent,
                prefix=f".{self.destination.name}.",
            )
        )
        staging.chmod(stat.S_IRWXU)
        self._staging = staging
        return self

    def __exit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> None:
        del exception_type, exception, traceback
        staging = self._staging
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
        self._staging = None

    def _active_root(self) -> Path:
        if self._staging is None or self._published:
            raise TrialBundleStateError("bundle builder is not active")
        return self._staging

    @property
    def artifact_sink(self) -> DirectoryArtifactSink:
        """Return a private sink whose references remain valid after publication."""

        root = self._active_root()
        return DirectoryArtifactSink(
            root / "artifacts",
            uri_prefix="artifacts",
        )

    def complete(self, result: CompletedTrial) -> JsonObject:
        """Write, verify, and atomically publish one completed trial bundle."""

        root = self._active_root()
        receipt = build_reasoning_receipt(result.ledger)
        ReasoningReceiptValidator().validate_against_ledger(receipt, result.ledger)
        scorecard = _scorecard(result, receipt_recomputes=True)
        declared_metrics = set(_strings(self._trial_manifest["metrics"], "metrics"))
        if set(scorecard) != declared_metrics:
            raise TrialBundleValidationError(
                "scorecard fields do not exactly match preregistered metrics"
            )

        artifact_uri = result.diagnosis.raw_output_artifact.uri
        artifact_relative = parse_bundle_relative_path(artifact_uri)
        if not artifact_relative.parts or artifact_relative.parts[0] != "artifacts":
            raise TrialBundleValidationError(
                "diagnosis artifact must use the bundle artifacts/ namespace"
            )
        artifact_path = root.joinpath(*artifact_relative.parts)
        if not artifact_path.is_file() or artifact_path.is_symlink():
            raise TrialBundleValidationError(
                "diagnosis artifact was not captured inside the bundle"
            )
        artifact_content = artifact_path.read_bytes()
        if (
            _sha256_bytes(artifact_content)
            != result.diagnosis.raw_output_artifact.sha256
        ):
            raise TrialBundleValidationError(
                "captured diagnosis artifact does not match its declared digest"
            )

        _write_private_bytes(
            root / _TRIAL_MANIFEST_NAME,
            _pretty_json_bytes(self._trial_manifest),
        )
        _write_private_bytes(
            root / _DIAGNOSIS_RESULT_NAME,
            _pretty_json_bytes(result.diagnosis.to_json_object()),
        )
        _write_private_bytes(root / _LEDGER_NAME, canonical_ledger_bytes(result.ledger))
        _write_private_bytes(root / _SCORECARD_NAME, _pretty_json_bytes(scorecard))
        _write_private_bytes(root / _RECEIPT_NAME, _pretty_json_bytes(receipt))

        files = [
            _file_entry(
                root,
                _TRIAL_MANIFEST_NAME,
                media_type="application/json",
                visibility="shareable",
            ),
            _file_entry(
                root,
                _DIAGNOSIS_RESULT_NAME,
                media_type="application/json",
                visibility="shareable",
            ),
            _file_entry(
                root,
                _LEDGER_NAME,
                media_type="application/x-ndjson",
                visibility="shareable",
            ),
            _file_entry(
                root,
                _SCORECARD_NAME,
                media_type="application/json",
                visibility="shareable",
            ),
            _file_entry(
                root,
                _RECEIPT_NAME,
                media_type="application/json",
                visibility="shareable",
            ),
            _file_entry(
                root,
                artifact_uri,
                media_type=result.diagnosis.raw_output_artifact.media_type,
                visibility="private",
            ),
        ]
        files_json: list[JsonValue] = [entry for entry in files]
        bundle_manifest_without_id: JsonObject = {
            "bundle_version": TRIAL_BUNDLE_VERSION,
            "trial_series_id": self._trial_manifest["trial_series_id"],
            "attempt_index": 1,
            "condition": self._trial_manifest["condition"],
            "outcome": "completed",
            "created_at": self._created_at,
            "contains_private_artifacts": True,
            "files": files_json,
        }
        bundle_id = (
            "urn:sha256:"
            + hashlib.sha256(
                canonical_json_bytes(bundle_manifest_without_id)
            ).hexdigest()
        )
        bundle_manifest: JsonObject = {
            "bundle_id": bundle_id,
            **bundle_manifest_without_id,
        }
        TrialBundleValidator().validate_manifest(bundle_manifest)
        _write_private_bytes(
            root / _BUNDLE_MANIFEST_NAME,
            _pretty_json_bytes(bundle_manifest),
        )

        TrialBundleValidator().validate_completed(root, result.request)
        _fsync_directory(root / "artifacts")
        _fsync_directory(root)
        publish_path_no_replace(root, self.destination)
        self._staging = None
        self._published = True
        _fsync_directory(self.destination.parent)
        return deepcopy(bundle_manifest)

    def fail(self, error: ModelAdapterError) -> JsonObject:
        """Sanitize, verify, and atomically publish one failed adapter attempt."""

        root = self._active_root()
        failure: JsonObject = {
            "kind": error.failure.value,
            "retryable": error.retryable,
            "status_code": error.status_code,
        }
        artifact_uri: str | None = None
        if error.artifact is not None:
            artifact = error.artifact
            artifact_uri = artifact.uri
            artifact_relative = parse_bundle_relative_path(artifact_uri)
            if not artifact_relative.parts or artifact_relative.parts[0] != "artifacts":
                raise TrialBundleValidationError(
                    "failed response artifact must use the artifacts/ namespace"
                )
            artifact_path = root.joinpath(*artifact_relative.parts)
            if not artifact_path.is_file() or artifact_path.is_symlink():
                raise TrialBundleValidationError(
                    "failed response artifact was not captured inside the bundle"
                )
            if _sha256_bytes(artifact_path.read_bytes()) != artifact.sha256:
                raise TrialBundleValidationError(
                    "failed response artifact does not match its declared digest"
                )
            failure["response_artifact"] = artifact.to_json_object()

        attempt_report: JsonObject = {
            "attempt_report_version": "0.2.0",
            "trial_series_id": self._trial_manifest["trial_series_id"],
            "attempt_index": 1,
            "outcome": "adapter_failed",
            "completed_at": self._created_at,
            "failure": failure,
        }
        validator = TrialBundleValidator()
        validator.validate_attempt_report(attempt_report)
        _write_private_bytes(
            root / _TRIAL_MANIFEST_NAME,
            _pretty_json_bytes(self._trial_manifest),
        )
        _write_private_bytes(
            root / _ATTEMPT_REPORT_NAME,
            _pretty_json_bytes(attempt_report),
        )

        files = [
            _file_entry(
                root,
                _TRIAL_MANIFEST_NAME,
                media_type="application/json",
                visibility="shareable",
            ),
            _file_entry(
                root,
                _ATTEMPT_REPORT_NAME,
                media_type="application/json",
                visibility="shareable",
            ),
        ]
        if artifact_uri is not None and error.artifact is not None:
            files.append(
                _file_entry(
                    root,
                    artifact_uri,
                    media_type=error.artifact.media_type,
                    visibility="private",
                )
            )
        files_json: list[JsonValue] = [entry for entry in files]
        bundle_without_id: JsonObject = {
            "bundle_version": TRIAL_BUNDLE_VERSION,
            "trial_series_id": self._trial_manifest["trial_series_id"],
            "attempt_index": 1,
            "condition": self._trial_manifest["condition"],
            "outcome": "adapter_failed",
            "created_at": self._created_at,
            "contains_private_artifacts": artifact_uri is not None,
            "files": files_json,
        }
        bundle_id = (
            "urn:sha256:"
            + hashlib.sha256(canonical_json_bytes(bundle_without_id)).hexdigest()
        )
        bundle_manifest: JsonObject = {
            "bundle_id": bundle_id,
            **bundle_without_id,
        }
        validator.validate_manifest(bundle_manifest)
        _write_private_bytes(
            root / _BUNDLE_MANIFEST_NAME,
            _pretty_json_bytes(bundle_manifest),
        )
        validator.validate_failure(root)
        if (root / "artifacts").exists():
            _fsync_directory(root / "artifacts")
        _fsync_directory(root)
        publish_path_no_replace(root, self.destination)
        self._staging = None
        self._published = True
        _fsync_directory(self.destination.parent)
        return deepcopy(bundle_manifest)
