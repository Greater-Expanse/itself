# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Atomic, private, digest-bound bundles for completed controlled trials."""

from __future__ import annotations

import hashlib
import stat
import tempfile
from collections.abc import Callable, Iterator, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Final, Protocol

from jsonschema.exceptions import ValidationError

from experiments._bundle_validation import (
    FILE_BYTE_LIMIT,
    LEDGER_BYTE_LIMIT,
    RECEIPT_BYTE_LIMIT,
    close_staging,
    read_regular_file,
    receipt_recomputes,
    recordable_status_code,
    require_bundle_root,
    require_unused_destination,
)
from experiments._bundle_validation import (
    AttemptQuarantinedError as AttemptQuarantinedError,
)
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
from experiments._schema_validation import contract_validator
from experiments.cases.cache_key_scripted_run import project_scripted_case
from experiments.diagnostician import (
    DiagnosisRequest,
    DiagnosisResult,
    DiagnosticianContractValidator,
)
from experiments.model_adapters import DirectoryArtifactSink, ModelAdapterError
from experiments.trial_manifests import (
    TrialManifestValidationError,
    TrialManifestValidator,
    canonical_json_bytes,
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
# Scorecard fields that the builder adds to the runner's own metrics.
_BUILDER_METRICS: Final = (
    "model_input_tokens",
    "model_output_tokens",
    "wall_time_ms",
    "receipt_recomputes",
)


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


class TrialProjection(Protocol):
    """Deterministic replay of one recorded diagnosis through its case."""

    def __call__(
        self,
        request: DiagnosisRequest,
        diagnosis: DiagnosisResult,
        /,
    ) -> CompletedTrial: ...


class TrialBundleValidationError(ValueError):
    """Raised when a trial bundle is structurally or cryptographically invalid."""


class TrialBundleStateError(RuntimeError):
    """Raised when a bundle builder is used outside its one-shot lifecycle."""


def _load_json_object(path: Path) -> JsonObject:
    return _shared_load_json_object(path, error_type=TrialBundleValidationError)


def _read_artifact(path: Path) -> bytes:
    return read_regular_file(
        path,
        max_bytes=FILE_BYTE_LIMIT,
        error_type=TrialBundleValidationError,
    )


def _require_scorecard_metrics(
    trial_manifest: JsonObject,
    metric_fields: tuple[str, ...],
) -> None:
    """Require the manifest to declare exactly the fields the scorecard holds."""

    produced = set(metric_fields)
    if len(produced) != len(metric_fields) or produced & set(_BUILDER_METRICS):
        raise TrialBundleValidationError(
            "runner metric fields must be unique and must not repeat the "
            f"builder's fields {list(_BUILDER_METRICS)}"
        )
    produced.update(_BUILDER_METRICS)
    declared = set(_strings(trial_manifest["metrics"], "metrics"))
    if declared != produced:
        raise TrialBundleValidationError(
            "trial manifest metrics do not match the scorecard fields; "
            f"undeclared={sorted(produced - declared)}, "
            f"unmeasured={sorted(declared - produced)}"
        )


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
    """Validate file inventory, digests, and cross-document bundle bindings.

    A completed bundle is replayed: ``projection`` runs the recorded diagnosis
    through its case again, and the stored ledger and scorecard must equal
    what that replay produces.  The default replays case 001.
    """

    def __init__(self, *, projection: TrialProjection = project_scripted_case) -> None:
        self._validator: _SchemaValidator = contract_validator(_BUNDLE_SCHEMA_PATH)
        self._attempt_validator: _SchemaValidator = contract_validator(
            _ATTEMPT_SCHEMA_PATH
        )
        self._projection = projection

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

    @staticmethod
    def _trial_manifest(root: Path, bundle_manifest: JsonObject) -> JsonObject:
        trial_manifest = _load_json_object(root / _TRIAL_MANIFEST_NAME)
        TrialManifestValidator().validate(trial_manifest)
        if bundle_manifest["trial_series_id"] != trial_manifest["trial_series_id"]:
            raise TrialBundleValidationError(
                "bundle trial_series_id does not match its trial manifest"
            )
        if bundle_manifest["condition"] != trial_manifest["condition"]:
            raise TrialBundleValidationError(
                "bundle condition does not match its trial manifest"
            )
        return trial_manifest

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

        trial_manifest = self._trial_manifest(root, bundle_manifest)
        artifact_policy = _object(trial_manifest["artifact_policy"], "artifact_policy")
        if artifact_policy["raw_provider_response"] != "private":
            raise TrialBundleValidationError(
                "completed private bundle requires private raw-response retention"
            )

        ledger = JsonlLedgerStore(root / _LEDGER_NAME).load(max_bytes=LEDGER_BYTE_LIMIT)
        receipt = JsonReceiptStore(root / _RECEIPT_NAME).load(
            max_bytes=RECEIPT_BYTE_LIMIT
        )
        ReasoningReceiptValidator().validate_against_ledger(receipt, ledger)

        diagnosis_value = _load_json_object(root / _DIAGNOSIS_RESULT_NAME)
        diagnosis = DiagnosticianContractValidator().decode_result(
            request,
            diagnosis_value,
        )
        try:
            TrialManifestValidator().validate_recorded(
                trial_manifest,
                request,
                diagnosis.diagnostician,
            )
        except TrialManifestValidationError as error:
            raise TrialBundleValidationError(f"trial manifest: {error}") from error

        projected = self._projection(request, diagnosis)
        if ledger.records != projected.ledger.records:
            raise TrialBundleValidationError(
                "ledger does not match the deterministic projection of the diagnosis"
            )
        scorecard = _load_json_object(root / _SCORECARD_NAME)
        declared_metrics = set(_strings(trial_manifest["metrics"], "metrics"))
        if set(scorecard) != declared_metrics:
            raise TrialBundleValidationError(
                "scorecard fields do not exactly match preregistered metrics"
            )
        # The receipt was validated against this ledger above, so a scorecard
        # from a correct run records receipt_recomputes as true.
        if scorecard != _scorecard(projected, receipt_recomputes=True):
            raise TrialBundleValidationError(
                "scorecard does not match the deterministic projection of the diagnosis"
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

        trial_manifest = self._trial_manifest(root, bundle_manifest)
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
        require_bundle_root(root, error_type=TrialBundleValidationError)
        bundle_manifest = _load_json_object(root / _BUNDLE_MANIFEST_NAME)
        self.validate_manifest(bundle_manifest)
        outcome = bundle_manifest["outcome"]
        if outcome == "completed":
            return self.validate_completed(root, request)
        if outcome == "adapter_failed":
            return self.validate_failure(root)
        raise TrialBundleValidationError(f"unsupported bundle outcome {outcome!r}")


class TrialBundleBuilder:
    """Build one immutable bundle in a private sibling directory, then rename.

    ``metric_fields`` names the fields of the runner's metrics.  With the
    fields the builder adds, they must be exactly the manifest's registered
    metrics, which is checked before any request.  Once staging holds a
    captured response, an exception never deletes it: the staging directory
    is renamed to ``<destination>.quarantined`` and an
    ``AttemptQuarantinedError`` names that path.
    """

    def __init__(
        self,
        destination: StrPath,
        trial_manifest: JsonObject,
        *,
        now: Callable[[], datetime],
        metric_fields: Sequence[str],
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
        self._metric_fields = tuple(metric_fields)
        _require_scorecard_metrics(trial_manifest, self._metric_fields)
        self._trial_manifest: JsonObject = deepcopy(trial_manifest)
        self._now = now
        self._created_at = _timestamp(now())
        self._staging: Path | None = None
        self._published = False

    def __enter__(self) -> TrialBundleBuilder:
        if self._staging is not None or self._published:
            raise TrialBundleStateError("bundle builder cannot be entered twice")
        require_unused_destination(self.destination)
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
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, traceback
        staging, self._staging = self._staging, None
        close_staging(staging, self.destination, exception)

    def _active_root(self) -> Path:
        if self._staging is None or self._published:
            raise TrialBundleStateError("bundle builder is not active")
        return self._staging

    def _captured_artifact(self, uri: str, sha256: str, *, label: str) -> None:
        artifact_relative = parse_bundle_relative_path(uri)
        if not artifact_relative.parts or artifact_relative.parts[0] != "artifacts":
            raise TrialBundleValidationError(
                f"{label} artifact must use the bundle artifacts/ namespace"
            )
        artifact_path = self._active_root().joinpath(*artifact_relative.parts)
        if _sha256_bytes(_read_artifact(artifact_path)) != sha256:
            raise TrialBundleValidationError(
                f"captured {label} artifact does not match its declared digest"
            )

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
        if set(result.metrics.to_json_object()) != set(self._metric_fields):
            raise TrialBundleValidationError(
                "runner metrics differ from the metric fields given to the builder"
            )
        receipt = build_reasoning_receipt(result.ledger)
        # A v1 completed bundle records only a receipt that recomputes, so a
        # receipt that does not is a failure that keeps its captured response.
        if not receipt_recomputes(receipt, result.ledger):
            raise TrialBundleValidationError(
                "reasoning receipt does not recompute from the trial ledger"
            )
        scorecard = _scorecard(result, receipt_recomputes=True)
        declared_metrics = set(_strings(self._trial_manifest["metrics"], "metrics"))
        if set(scorecard) != declared_metrics:
            raise TrialBundleValidationError(
                "scorecard fields do not exactly match preregistered metrics"
            )

        artifact_uri = result.diagnosis.raw_output_artifact.uri
        self._captured_artifact(
            artifact_uri,
            result.diagnosis.raw_output_artifact.sha256,
            label="diagnosis",
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
            "status_code": recordable_status_code(error.status_code),
        }
        artifact_uri: str | None = None
        if error.artifact is not None:
            artifact = error.artifact
            artifact_uri = artifact.uri
            self._captured_artifact(
                artifact_uri,
                artifact.sha256,
                label="failed response",
            )
            failure["response_artifact"] = artifact.to_json_object()

        attempt_report: JsonObject = {
            "attempt_report_version": "0.2.0",
            "trial_series_id": self._trial_manifest["trial_series_id"],
            "attempt_index": 1,
            "outcome": "adapter_failed",
            "completed_at": _timestamp(self._now()),
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
