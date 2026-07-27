# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Atomic, private, digest-bound bundles for paired model comparisons."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
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
from experiments._contract_values import integer_value as _integer
from experiments._contract_values import object_value as _object
from experiments._contract_values import string_value as _string
from experiments._contract_values import string_values as _strings
from experiments.cases.cache_key_comparison import (
    NARRATIVE_ATTEMPT_ID,
    STRUCTURED_ATTEMPT_ID,
    ComparativeRunResult,
    ComparisonBranchResult,
    ComparisonCondition,
    project_comparative_results,
)
from experiments.comparison_manifests import (
    ComparisonManifestValidator,
    load_comparison_manifest,
)
from experiments.diagnostician import (
    DiagnosisRequest,
    DiagnosisResult,
    DiagnosticianContractValidator,
    RawOutputArtifact,
)
from experiments.model_adapters import (
    DirectoryArtifactSink,
    ModelAdapterError,
)
from experiments.narrative_diagnostician import (
    NarrativeContractValidator,
    NarrativeResult,
)
from experiments.trial_manifests import canonical_json_bytes
from itself import JsonlLedgerStore, JsonObject, JsonValue, Ledger, StrPath
from itself._filesystem import (
    ensure_private_directory,
    publish_path_no_replace,
    write_private_file_exclusive,
)
from itself._filesystem import (
    fsync_directory as _fsync_directory,
)
from itself._json import format_json_path as _format_path
from itself.receipts import (
    JsonReceiptStore,
    ReasoningReceiptValidator,
    canonical_ledger_bytes,
)

COMPARISON_BUNDLE_VERSION: Final = "0.2.0"
COMPARISON_ATTEMPT_REPORT_VERSION: Final = "0.2.0"

_CONTRACT_ROOT: Final = Path(__file__).resolve().parent / "contracts" / "v1"
_BUNDLE_SCHEMA_PATH: Final = _CONTRACT_ROOT / "comparison-bundle.schema.json"
_ATTEMPT_SCHEMA_PATH: Final = _CONTRACT_ROOT / "comparison-attempt-report.schema.json"
_BUNDLE_MANIFEST_NAME: Final = "bundle.json"
_COMPARISON_MANIFEST_NAME: Final = "comparison-manifest.json"
_ATTEMPT_REPORT_NAME: Final = "attempt-report.json"
_NARRATIVE_RESULT_PATH: Final = f"attempts/{NARRATIVE_ATTEMPT_ID}/result.json"
_STRUCTURED_RESULT_PATH: Final = f"attempts/{STRUCTURED_ATTEMPT_ID}/result.json"
_NARRATIVE_ARTIFACT_PREFIX: Final = f"attempts/{NARRATIVE_ATTEMPT_ID}/private"
_STRUCTURED_ARTIFACT_PREFIX: Final = f"attempts/{STRUCTURED_ATTEMPT_ID}/private"
_BRANCH_NAMES: Final = tuple(condition.value for condition in ComparisonCondition)


class _SchemaValidator(Protocol):
    """Typed surface used from the partially typed jsonschema dependency."""

    def iter_errors(self, instance: JsonValue) -> Iterator[ValidationError]: ...


class ComparisonBundleValidationError(ValueError):
    """Raised when a comparison bundle is invalid or internally inconsistent."""


class ComparisonBundleStateError(RuntimeError):
    """Raised when a comparison builder is used outside its one-shot lifecycle."""


def _load_json_object(path: Path) -> JsonObject:
    return _shared_load_json_object(
        path,
        error_type=ComparisonBundleValidationError,
    )


def _normalized_relative_path(value: str) -> Path:
    relative = _shared_normalized_bundle_path(
        value,
        error_type=ComparisonBundleValidationError,
    )
    return Path(*relative.parts)


def _write_private_bytes(root: Path, path: Path, content: bytes) -> None:
    ensure_private_directory(root, path.parent)
    write_private_file_exclusive(path, content)


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
        error_type=ComparisonBundleValidationError,
    )


def _branch_path(condition: ComparisonCondition, filename: str) -> str:
    return f"branches/{condition.value}/{filename}"


def _attempt_profile_id(manifest: JsonObject, attempt_id: str) -> str:
    profile_name = {
        NARRATIVE_ATTEMPT_ID: "narrative",
        STRUCTURED_ATTEMPT_ID: "structured",
    }.get(attempt_id)
    if profile_name is None:
        raise ValueError(f"unsupported comparison attempt {attempt_id!r}")
    profiles = _object(manifest["profiles"], "profiles")
    profile = _object(profiles[profile_name], f"profiles.{profile_name}")
    return _string(profile["profile_id"], f"profiles.{profile_name}.profile_id")


def _replicate_count(manifest: JsonObject) -> int:
    design = _object(manifest["design"], "design")
    return _integer(design["replicate_count"], "replicate_count")


def _validate_replicate_index(
    manifest: JsonObject,
    replicate_index: int,
) -> None:
    if (
        isinstance(replicate_index, bool)
        or replicate_index < 1
        or replicate_index > _replicate_count(manifest)
    ):
        raise ComparisonBundleValidationError(
            "replicate_index falls outside the registered comparison series"
        )


@dataclass(frozen=True, slots=True)
class VerifiedComparisonBranch:
    """One independently replayed branch from a completed comparison."""

    condition: ComparisonCondition
    ledger: Ledger
    scorecard: JsonObject
    receipt: JsonObject


@dataclass(frozen=True, slots=True)
class VerifiedComparisonBundle:
    """Validated projections loaded from one completed comparison bundle."""

    path: Path
    bundle_manifest: JsonObject
    comparison_manifest: JsonObject
    request: DiagnosisRequest
    narrative_result: NarrativeResult
    structured_result: DiagnosisResult
    branches: Mapping[ComparisonCondition, VerifiedComparisonBranch]


@dataclass(frozen=True, slots=True)
class VerifiedFailedComparisonBundle:
    """Validated records from one failed paired-comparison attempt."""

    path: Path
    bundle_manifest: JsonObject
    comparison_manifest: JsonObject
    attempt_report: JsonObject
    narrative_result: NarrativeResult | None


class ComparisonBundleValidator:
    """Validate inventory, bindings, projections, scorecards, and receipts."""

    def __init__(self) -> None:
        bundle_schema = cast(
            JsonValue,
            json.loads(_BUNDLE_SCHEMA_PATH.read_text(encoding="utf-8")),
        )
        attempt_schema = cast(
            JsonValue,
            json.loads(_ATTEMPT_SCHEMA_PATH.read_text(encoding="utf-8")),
        )
        if not isinstance(bundle_schema, dict) or not isinstance(attempt_schema, dict):
            raise TypeError("comparison bundle contracts must contain JSON objects")
        Draft202012Validator.check_schema(bundle_schema)
        Draft202012Validator.check_schema(attempt_schema)
        self._bundle_validator = cast(
            _SchemaValidator,
            Draft202012Validator(
                bundle_schema,
                format_checker=FormatChecker(),
            ),
        )
        self._attempt_validator = cast(
            _SchemaValidator,
            Draft202012Validator(
                attempt_schema,
                format_checker=FormatChecker(),
            ),
        )

    @staticmethod
    def _errors(
        validator: _SchemaValidator,
        value: JsonValue,
    ) -> list[str]:
        issues = sorted(
            validator.iter_errors(value),
            key=lambda error: tuple(str(item) for item in error.absolute_path),
        )
        return [
            f"{_format_path(tuple(issue.absolute_path))}: {issue.message}"
            for issue in issues
        ]

    def validate_manifest(self, bundle_manifest: JsonValue) -> None:
        """Validate comparison bundle metadata against its schema."""

        issues = self._errors(self._bundle_validator, bundle_manifest)
        if issues:
            raise ComparisonBundleValidationError("\n".join(issues))

    def validate_attempt_report(self, attempt_report: JsonValue) -> None:
        """Validate one sanitized failed-attempt report."""

        issues = self._errors(self._attempt_validator, attempt_report)
        if issues:
            raise ComparisonBundleValidationError("\n".join(issues))

    def validate_attempt_report_against_manifest(
        self,
        attempt_report: JsonObject,
        manifest: JsonObject,
    ) -> None:
        """Validate a failure report and bind its profile to its manifest."""

        self.validate_attempt_report(attempt_report)
        attempt_id = _string(attempt_report["attempt_id"], "attempt_id")
        expected_profile_id = _attempt_profile_id(manifest, attempt_id)
        if attempt_report["profile_id"] != expected_profile_id:
            raise ComparisonBundleValidationError(
                "attempt report profile does not match the comparison manifest"
            )

    def _validate_inventory(self, root: Path) -> _ValidatedInventory:
        return _shared_validate_inventory(
            root,
            manifest_name=_BUNDLE_MANIFEST_NAME,
            validate_manifest=self.validate_manifest,
            error_type=ComparisonBundleValidationError,
        )

    @staticmethod
    def _require_entry(
        inventory: _ValidatedInventory,
        path: str,
        *,
        media_type: str,
        visibility: str,
    ) -> None:
        entry = inventory.entry_by_path.get(path)
        if entry is None:
            raise ComparisonBundleValidationError(
                f"comparison bundle is missing required file {path!r}"
            )
        if entry["media_type"] != media_type:
            raise ComparisonBundleValidationError(
                f"comparison bundle file {path!r} has the wrong media type"
            )
        if entry["visibility"] != visibility:
            raise ComparisonBundleValidationError(
                f"comparison bundle file {path!r} has the wrong visibility"
            )

    def _comparison_manifest(
        self,
        root: Path,
        inventory: _ValidatedInventory,
    ) -> JsonObject:
        self._require_entry(
            inventory,
            _COMPARISON_MANIFEST_NAME,
            media_type="application/json",
            visibility="shareable",
        )
        manifest = load_comparison_manifest(root / _COMPARISON_MANIFEST_NAME)
        ComparisonManifestValidator().validate(manifest)
        bundle = inventory.bundle_manifest
        if bundle["comparison_series_id"] != manifest["comparison_series_id"]:
            raise ComparisonBundleValidationError(
                "bundle comparison_series_id does not match its manifest"
            )
        _validate_replicate_index(
            manifest,
            _integer(bundle["replicate_index"], "replicate_index"),
        )
        artifact_policy = _object(manifest["artifact_policy"], "artifact_policy")
        if artifact_policy["raw_provider_response"] != "private":
            raise ComparisonBundleValidationError(
                "comparison bundles require private raw-response retention"
            )
        return manifest

    def _validate_artifact(
        self,
        root: Path,
        inventory: _ValidatedInventory,
        artifact: RawOutputArtifact,
        *,
        expected_prefix: str,
    ) -> None:
        uri = artifact.uri
        if not uri.startswith(expected_prefix + "/"):
            raise ComparisonBundleValidationError(
                "provider artifact URI uses the wrong attempt namespace"
            )
        self._require_entry(
            inventory,
            uri,
            media_type=artifact.media_type,
            visibility="private",
        )
        entry = inventory.entry_by_path[uri]
        digest = _object(entry["digest"], "digest")
        if digest["value"] != artifact.sha256:
            raise ComparisonBundleValidationError(
                "provider artifact digest does not match the result binding"
            )
        if _sha256_bytes((root / _normalized_relative_path(uri)).read_bytes()) != (
            artifact.sha256
        ):
            raise ComparisonBundleValidationError(
                "provider artifact bytes do not match the result binding"
            )

    def _validate_branch(
        self,
        root: Path,
        inventory: _ValidatedInventory,
        projected: ComparisonBranchResult,
        declared_metrics: frozenset[str],
    ) -> VerifiedComparisonBranch:
        condition = projected.condition
        ledger_path = _branch_path(condition, "ledger.jsonl")
        scorecard_path = _branch_path(condition, "scorecard.json")
        receipt_path = _branch_path(condition, "reasoning-receipt.json")
        self._require_entry(
            inventory,
            ledger_path,
            media_type="application/x-ndjson",
            visibility="shareable",
        )
        self._require_entry(
            inventory,
            scorecard_path,
            media_type="application/json",
            visibility="shareable",
        )
        self._require_entry(
            inventory,
            receipt_path,
            media_type="application/json",
            visibility="shareable",
        )

        ledger = JsonlLedgerStore(root / _normalized_relative_path(ledger_path)).load()
        receipt = JsonReceiptStore(
            root / _normalized_relative_path(receipt_path)
        ).load()
        ReasoningReceiptValidator().validate_against_ledger(receipt, ledger)
        scorecard = _load_json_object(root / _normalized_relative_path(scorecard_path))
        if frozenset(scorecard) != declared_metrics:
            raise ComparisonBundleValidationError(
                f"{condition.value} scorecard fields differ from registered metrics"
            )
        if ledger.records != projected.ledger.records:
            raise ComparisonBundleValidationError(
                f"{condition.value} ledger does not match deterministic projection"
            )
        if receipt != projected.receipt:
            raise ComparisonBundleValidationError(
                f"{condition.value} receipt does not match deterministic projection"
            )
        if scorecard != projected.scorecard.to_json_object():
            raise ComparisonBundleValidationError(
                f"{condition.value} scorecard does not match deterministic projection"
            )
        return VerifiedComparisonBranch(
            condition=condition,
            ledger=ledger,
            scorecard=deepcopy(scorecard),
            receipt=deepcopy(receipt),
        )

    def validate_completed(
        self,
        path: StrPath,
        request: DiagnosisRequest,
    ) -> VerifiedComparisonBundle:
        """Verify every byte and deterministic branch in a completed bundle."""

        root = Path(path)
        inventory = self._validate_inventory(root)
        bundle = inventory.bundle_manifest
        if bundle["outcome"] != "completed":
            raise ComparisonBundleValidationError(
                "comparison bundle outcome is not completed"
            )
        manifest = self._comparison_manifest(root, inventory)
        for result_path in (_NARRATIVE_RESULT_PATH, _STRUCTURED_RESULT_PATH):
            self._require_entry(
                inventory,
                result_path,
                media_type="application/json",
                visibility="shareable",
            )

        narrative_result = NarrativeContractValidator().decode_result(
            request,
            _load_json_object(root / _normalized_relative_path(_NARRATIVE_RESULT_PATH)),
        )
        structured_result = DiagnosticianContractValidator().decode_result(
            request,
            _load_json_object(
                root / _normalized_relative_path(_STRUCTURED_RESULT_PATH)
            ),
        )
        self._validate_artifact(
            root,
            inventory,
            narrative_result.raw_output_artifact,
            expected_prefix=_NARRATIVE_ARTIFACT_PREFIX,
        )
        self._validate_artifact(
            root,
            inventory,
            structured_result.raw_output_artifact,
            expected_prefix=_STRUCTURED_ARTIFACT_PREFIX,
        )

        projected = project_comparative_results(
            request,
            narrative_result,
            structured_result,
        )
        declared_metrics = frozenset(_strings(manifest["metrics"], "metrics"))
        projected_branches = (
            projected.narrative,
            projected.structured_without_testing,
            projected.evidence_enforced,
        )
        branches = {
            branch.condition: self._validate_branch(
                root,
                inventory,
                branch,
                declared_metrics,
            )
            for branch in projected_branches
        }

        bindings = _object(bundle["branch_bindings"], "branch_bindings")
        expected_bindings: JsonObject = {
            ComparisonCondition.NARRATIVE.value: {
                "source_attempt_id": NARRATIVE_ATTEMPT_ID,
                "result_path": _NARRATIVE_RESULT_PATH,
                "raw_artifact_sha256": (narrative_result.raw_output_artifact.sha256),
            },
            ComparisonCondition.STRUCTURED_WITHOUT_TESTING.value: {
                "source_attempt_id": STRUCTURED_ATTEMPT_ID,
                "result_path": _STRUCTURED_RESULT_PATH,
                "raw_artifact_sha256": (structured_result.raw_output_artifact.sha256),
            },
            ComparisonCondition.EVIDENCE_ENFORCED.value: {
                "source_attempt_id": STRUCTURED_ATTEMPT_ID,
                "result_path": _STRUCTURED_RESULT_PATH,
                "raw_artifact_sha256": (structured_result.raw_output_artifact.sha256),
            },
        }
        if bindings != expected_bindings:
            raise ComparisonBundleValidationError(
                "comparison branch bindings do not match their source attempts"
            )

        expected_paths = {
            _COMPARISON_MANIFEST_NAME,
            _NARRATIVE_RESULT_PATH,
            _STRUCTURED_RESULT_PATH,
            narrative_result.raw_output_artifact.uri,
            structured_result.raw_output_artifact.uri,
            *(
                _branch_path(ComparisonCondition(branch), filename)
                for branch in _BRANCH_NAMES
                for filename in (
                    "ledger.jsonl",
                    "scorecard.json",
                    "reasoning-receipt.json",
                )
            ),
        }
        if set(inventory.paths) != expected_paths:
            raise ComparisonBundleValidationError(
                "completed comparison inventory contains undeclared files"
            )
        return VerifiedComparisonBundle(
            path=root,
            bundle_manifest=deepcopy(bundle),
            comparison_manifest=deepcopy(manifest),
            request=request,
            narrative_result=narrative_result,
            structured_result=structured_result,
            branches=MappingProxyType(dict(branches)),
        )

    def validate_failure(
        self,
        path: StrPath,
        request: DiagnosisRequest,
    ) -> VerifiedFailedComparisonBundle:
        """Verify a stopped comparison and its sanitized failure boundary."""

        root = Path(path)
        inventory = self._validate_inventory(root)
        bundle = inventory.bundle_manifest
        if bundle["outcome"] != "adapter_failed":
            raise ComparisonBundleValidationError(
                "comparison bundle outcome is not adapter_failed"
            )
        manifest = self._comparison_manifest(root, inventory)
        self._require_entry(
            inventory,
            _ATTEMPT_REPORT_NAME,
            media_type="application/json",
            visibility="shareable",
        )
        attempt_report = _load_json_object(root / _ATTEMPT_REPORT_NAME)
        self.validate_attempt_report_against_manifest(attempt_report, manifest)
        if attempt_report["comparison_series_id"] != manifest["comparison_series_id"]:
            raise ComparisonBundleValidationError(
                "attempt report series does not match the comparison manifest"
            )

        outcomes = _object(bundle["attempt_outcomes"], "attempt_outcomes")
        failed_attempt = _string(attempt_report["attempt_id"], "attempt_id")
        if outcomes.get(failed_attempt) != "adapter_failed":
            raise ComparisonBundleValidationError(
                "attempt report does not identify the declared failed attempt"
            )
        allowed_paths = {_COMPARISON_MANIFEST_NAME, _ATTEMPT_REPORT_NAME}
        narrative_result: NarrativeResult | None = None
        if failed_attempt == STRUCTURED_ATTEMPT_ID:
            self._require_entry(
                inventory,
                _NARRATIVE_RESULT_PATH,
                media_type="application/json",
                visibility="shareable",
            )
            narrative_result = NarrativeContractValidator().decode_result(
                request,
                _load_json_object(
                    root / _normalized_relative_path(_NARRATIVE_RESULT_PATH)
                ),
            )
            self._validate_artifact(
                root,
                inventory,
                narrative_result.raw_output_artifact,
                expected_prefix=_NARRATIVE_ARTIFACT_PREFIX,
            )
            allowed_paths.update(
                {
                    _NARRATIVE_RESULT_PATH,
                    narrative_result.raw_output_artifact.uri,
                }
            )

        failure = _object(attempt_report["failure"], "failure")
        artifact_value = failure.get("response_artifact")
        if artifact_value is not None:
            artifact = _object(artifact_value, "response_artifact")
            digest = _object(artifact["digest"], "response_artifact.digest")
            raw_artifact = RawOutputArtifact(
                uri=_string(artifact["uri"], "response_artifact.uri"),
                media_type=_string(
                    artifact["media_type"],
                    "response_artifact.media_type",
                ),
                sha256=_string(digest["value"], "response_artifact.digest.value"),
                captured_at=_string(
                    artifact["captured_at"],
                    "response_artifact.captured_at",
                ),
            )
            expected_prefix = (
                _NARRATIVE_ARTIFACT_PREFIX
                if failed_attempt == NARRATIVE_ATTEMPT_ID
                else _STRUCTURED_ARTIFACT_PREFIX
            )
            self._validate_artifact(
                root,
                inventory,
                raw_artifact,
                expected_prefix=expected_prefix,
            )
            allowed_paths.add(raw_artifact.uri)

        if bundle["contains_private_artifacts"] is not (len(allowed_paths) > 2):
            raise ComparisonBundleValidationError(
                "private-artifact declaration does not match failed inventory"
            )
        if set(inventory.paths) != allowed_paths:
            raise ComparisonBundleValidationError(
                "failed comparison inventory contains undeclared files"
            )
        return VerifiedFailedComparisonBundle(
            path=root,
            bundle_manifest=deepcopy(bundle),
            comparison_manifest=deepcopy(manifest),
            attempt_report=deepcopy(attempt_report),
            narrative_result=narrative_result,
        )

    def validate(
        self,
        path: StrPath,
        request: DiagnosisRequest,
    ) -> VerifiedComparisonBundle | VerifiedFailedComparisonBundle:
        """Validate a completed or stopped comparison bundle."""

        root = Path(path)
        bundle_manifest = _load_json_object(root / _BUNDLE_MANIFEST_NAME)
        self.validate_manifest(bundle_manifest)
        if bundle_manifest["outcome"] == "completed":
            return self.validate_completed(root, request)
        return self.validate_failure(root, request)


class ComparisonBundleBuilder:
    """Build one immutable comparison bundle, verify it, then publish atomically."""

    def __init__(
        self,
        destination: StrPath,
        comparison_manifest: JsonObject,
        request: DiagnosisRequest,
        *,
        replicate_index: int,
        now: Callable[[], datetime],
    ) -> None:
        self.destination = Path(destination)
        if not self.destination.name:
            raise ValueError("comparison bundle destination must name a directory")
        ComparisonManifestValidator().validate(comparison_manifest)
        _validate_replicate_index(comparison_manifest, replicate_index)
        artifact_policy = _object(
            comparison_manifest["artifact_policy"],
            "artifact_policy",
        )
        if artifact_policy["raw_provider_response"] != "private":
            raise ComparisonBundleValidationError(
                "comparison builder requires private raw-response retention"
            )
        self._manifest = deepcopy(comparison_manifest)
        self._request = request
        self._replicate_index = replicate_index
        self._now = now
        self._created_at = _timestamp(now())
        self._staging: Path | None = None
        self._published = False

    def __enter__(self) -> ComparisonBundleBuilder:
        if self._staging is not None or self._published:
            raise ComparisonBundleStateError(
                "comparison bundle builder cannot be entered twice"
            )
        if self.destination.exists() or self.destination.is_symlink():
            raise FileExistsError(self.destination)
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                dir=self.destination.parent,
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
            raise ComparisonBundleStateError("comparison bundle builder is not active")
        return self._staging

    def _artifact_sink(self, prefix: str) -> DirectoryArtifactSink:
        root = self._active_root()
        artifact_root = root / _normalized_relative_path(prefix)
        ensure_private_directory(root, artifact_root)
        return DirectoryArtifactSink(artifact_root, uri_prefix=prefix)

    @property
    def narrative_artifact_sink(self) -> DirectoryArtifactSink:
        """Return the raw-response sink for the narrative attempt."""

        return self._artifact_sink(_NARRATIVE_ARTIFACT_PREFIX)

    @property
    def structured_artifact_sink(self) -> DirectoryArtifactSink:
        """Return the raw-response sink for the structured attempt."""

        return self._artifact_sink(_STRUCTURED_ARTIFACT_PREFIX)

    def _require_captured_artifact(
        self,
        artifact: RawOutputArtifact,
        *,
        expected_prefix: str,
    ) -> None:
        if not artifact.uri.startswith(expected_prefix + "/"):
            raise ComparisonBundleValidationError(
                "captured provider artifact uses the wrong attempt namespace"
            )
        path = self._active_root() / _normalized_relative_path(artifact.uri)
        if not path.is_file() or path.is_symlink():
            raise ComparisonBundleValidationError(
                "provider artifact was not captured inside the comparison bundle"
            )
        if _sha256_bytes(path.read_bytes()) != artifact.sha256:
            raise ComparisonBundleValidationError(
                "captured provider artifact does not match its result digest"
            )

    def _write_branch(self, branch: ComparisonBranchResult) -> list[JsonObject]:
        root = self._active_root()
        ledger_path = _branch_path(branch.condition, "ledger.jsonl")
        scorecard_path = _branch_path(branch.condition, "scorecard.json")
        receipt_path = _branch_path(branch.condition, "reasoning-receipt.json")
        ReasoningReceiptValidator().validate_against_ledger(
            branch.receipt,
            branch.ledger,
        )
        scorecard = branch.scorecard.to_json_object()
        declared_metrics = set(_strings(self._manifest["metrics"], "metrics"))
        if set(scorecard) != declared_metrics:
            raise ComparisonBundleValidationError(
                f"{branch.condition.value} scorecard differs from registered metrics"
            )
        _write_private_bytes(
            root,
            root / _normalized_relative_path(ledger_path),
            canonical_ledger_bytes(branch.ledger),
        )
        _write_private_bytes(
            root,
            root / _normalized_relative_path(scorecard_path),
            _pretty_json_bytes(scorecard),
        )
        _write_private_bytes(
            root,
            root / _normalized_relative_path(receipt_path),
            _pretty_json_bytes(branch.receipt),
        )
        return [
            _file_entry(
                root,
                ledger_path,
                media_type="application/x-ndjson",
                visibility="shareable",
            ),
            _file_entry(
                root,
                scorecard_path,
                media_type="application/json",
                visibility="shareable",
            ),
            _file_entry(
                root,
                receipt_path,
                media_type="application/json",
                visibility="shareable",
            ),
        ]

    def _bundle_manifest(
        self,
        *,
        outcome: str,
        attempt_outcomes: JsonObject,
        completed_branch_count: int,
        contains_private_artifacts: bool,
        branch_bindings: JsonObject,
        files: Sequence[JsonObject],
    ) -> JsonObject:
        files_json: list[JsonValue] = [entry for entry in files]
        without_id: JsonObject = {
            "bundle_version": COMPARISON_BUNDLE_VERSION,
            "comparison_series_id": self._manifest["comparison_series_id"],
            "replicate_index": self._replicate_index,
            "outcome": outcome,
            "created_at": self._created_at,
            "attempt_outcomes": attempt_outcomes,
            "completed_branch_count": completed_branch_count,
            "contains_private_artifacts": contains_private_artifacts,
            "branch_bindings": branch_bindings,
            "files": files_json,
        }
        bundle_id = (
            "urn:sha256:" + hashlib.sha256(canonical_json_bytes(without_id)).hexdigest()
        )
        return {"bundle_id": bundle_id, **without_id}

    def _publish(self, bundle_manifest: JsonObject) -> JsonObject:
        root = self._active_root()
        _write_private_bytes(
            root,
            root / _BUNDLE_MANIFEST_NAME,
            _pretty_json_bytes(bundle_manifest),
        )
        ComparisonBundleValidator().validate(root, self._request)
        directories = sorted(
            (path for path in root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in directories:
            _fsync_directory(directory)
        _fsync_directory(root)
        publish_path_no_replace(root, self.destination)
        self._staging = None
        self._published = True
        _fsync_directory(self.destination.parent)
        return deepcopy(bundle_manifest)

    def complete(self, result: ComparativeRunResult) -> JsonObject:
        """Write, verify, and publish one completed paired comparison."""

        if result.request.to_json_object() != self._request.to_json_object():
            raise ComparisonBundleValidationError(
                "comparison result request differs from the registered request"
            )
        NarrativeContractValidator().validate_result(
            self._request,
            result.narrative_result,
        )
        DiagnosticianContractValidator().validate_result(
            self._request,
            result.structured_result,
        )
        self._require_captured_artifact(
            result.narrative_result.raw_output_artifact,
            expected_prefix=_NARRATIVE_ARTIFACT_PREFIX,
        )
        self._require_captured_artifact(
            result.structured_result.raw_output_artifact,
            expected_prefix=_STRUCTURED_ARTIFACT_PREFIX,
        )
        root = self._active_root()
        _write_private_bytes(
            root,
            root / _COMPARISON_MANIFEST_NAME,
            _pretty_json_bytes(self._manifest),
        )
        _write_private_bytes(
            root,
            root / _normalized_relative_path(_NARRATIVE_RESULT_PATH),
            _pretty_json_bytes(result.narrative_result.to_json_object()),
        )
        _write_private_bytes(
            root,
            root / _normalized_relative_path(_STRUCTURED_RESULT_PATH),
            _pretty_json_bytes(result.structured_result.to_json_object()),
        )

        files = [
            _file_entry(
                root,
                _COMPARISON_MANIFEST_NAME,
                media_type="application/json",
                visibility="shareable",
            ),
            _file_entry(
                root,
                _NARRATIVE_RESULT_PATH,
                media_type="application/json",
                visibility="shareable",
            ),
            _file_entry(
                root,
                _STRUCTURED_RESULT_PATH,
                media_type="application/json",
                visibility="shareable",
            ),
            _file_entry(
                root,
                result.narrative_result.raw_output_artifact.uri,
                media_type=result.narrative_result.raw_output_artifact.media_type,
                visibility="private",
            ),
            _file_entry(
                root,
                result.structured_result.raw_output_artifact.uri,
                media_type=result.structured_result.raw_output_artifact.media_type,
                visibility="private",
            ),
        ]
        for branch in (
            result.narrative,
            result.structured_without_testing,
            result.evidence_enforced,
        ):
            files.extend(self._write_branch(branch))

        branch_bindings: JsonObject = {
            ComparisonCondition.NARRATIVE.value: {
                "source_attempt_id": NARRATIVE_ATTEMPT_ID,
                "result_path": _NARRATIVE_RESULT_PATH,
                "raw_artifact_sha256": (
                    result.narrative_result.raw_output_artifact.sha256
                ),
            },
            ComparisonCondition.STRUCTURED_WITHOUT_TESTING.value: {
                "source_attempt_id": STRUCTURED_ATTEMPT_ID,
                "result_path": _STRUCTURED_RESULT_PATH,
                "raw_artifact_sha256": (
                    result.structured_result.raw_output_artifact.sha256
                ),
            },
            ComparisonCondition.EVIDENCE_ENFORCED.value: {
                "source_attempt_id": STRUCTURED_ATTEMPT_ID,
                "result_path": _STRUCTURED_RESULT_PATH,
                "raw_artifact_sha256": (
                    result.structured_result.raw_output_artifact.sha256
                ),
            },
        }
        bundle_manifest = self._bundle_manifest(
            outcome="completed",
            attempt_outcomes={
                NARRATIVE_ATTEMPT_ID: "completed",
                STRUCTURED_ATTEMPT_ID: "completed",
            },
            completed_branch_count=3,
            contains_private_artifacts=True,
            branch_bindings=branch_bindings,
            files=files,
        )
        ComparisonBundleValidator().validate_manifest(bundle_manifest)
        return self._publish(bundle_manifest)

    def fail(
        self,
        error: ModelAdapterError,
        *,
        attempt_id: str,
        narrative_result: NarrativeResult | None = None,
    ) -> JsonObject:
        """Retain a sanitized attempt failure and stop the paired replicate."""

        if attempt_id not in {NARRATIVE_ATTEMPT_ID, STRUCTURED_ATTEMPT_ID}:
            raise ValueError(f"unsupported comparison attempt {attempt_id!r}")
        if (attempt_id == STRUCTURED_ATTEMPT_ID) is (narrative_result is None):
            raise ComparisonBundleValidationError(
                "structured failure requires the completed narrative result"
            )
        root = self._active_root()
        _write_private_bytes(
            root,
            root / _COMPARISON_MANIFEST_NAME,
            _pretty_json_bytes(self._manifest),
        )
        files = [
            _file_entry(
                root,
                _COMPARISON_MANIFEST_NAME,
                media_type="application/json",
                visibility="shareable",
            )
        ]
        if narrative_result is not None:
            NarrativeContractValidator().validate_result(
                self._request,
                narrative_result,
            )
            self._require_captured_artifact(
                narrative_result.raw_output_artifact,
                expected_prefix=_NARRATIVE_ARTIFACT_PREFIX,
            )
            _write_private_bytes(
                root,
                root / _normalized_relative_path(_NARRATIVE_RESULT_PATH),
                _pretty_json_bytes(narrative_result.to_json_object()),
            )
            files.extend(
                (
                    _file_entry(
                        root,
                        _NARRATIVE_RESULT_PATH,
                        media_type="application/json",
                        visibility="shareable",
                    ),
                    _file_entry(
                        root,
                        narrative_result.raw_output_artifact.uri,
                        media_type=narrative_result.raw_output_artifact.media_type,
                        visibility="private",
                    ),
                )
            )

        failure: JsonObject = {
            "kind": error.failure.value,
            "retryable": error.retryable,
            "status_code": error.status_code,
        }
        if error.artifact is not None:
            expected_prefix = (
                _NARRATIVE_ARTIFACT_PREFIX
                if attempt_id == NARRATIVE_ATTEMPT_ID
                else _STRUCTURED_ARTIFACT_PREFIX
            )
            self._require_captured_artifact(
                error.artifact,
                expected_prefix=expected_prefix,
            )
            failure["response_artifact"] = error.artifact.to_json_object()
            files.append(
                _file_entry(
                    root,
                    error.artifact.uri,
                    media_type=error.artifact.media_type,
                    visibility="private",
                )
            )

        attempt_report: JsonObject = {
            "attempt_report_version": COMPARISON_ATTEMPT_REPORT_VERSION,
            "comparison_series_id": self._manifest["comparison_series_id"],
            "attempt_id": attempt_id,
            "profile_id": _attempt_profile_id(self._manifest, attempt_id),
            "outcome": "adapter_failed",
            "completed_at": _timestamp(self._now()),
            "failure": failure,
        }
        validator = ComparisonBundleValidator()
        validator.validate_attempt_report_against_manifest(
            attempt_report,
            self._manifest,
        )
        _write_private_bytes(
            root,
            root / _ATTEMPT_REPORT_NAME,
            _pretty_json_bytes(attempt_report),
        )
        files.append(
            _file_entry(
                root,
                _ATTEMPT_REPORT_NAME,
                media_type="application/json",
                visibility="shareable",
            )
        )

        attempt_outcomes: JsonObject
        if attempt_id == NARRATIVE_ATTEMPT_ID:
            attempt_outcomes = {
                NARRATIVE_ATTEMPT_ID: "adapter_failed",
                STRUCTURED_ATTEMPT_ID: "not_run",
            }
        else:
            attempt_outcomes = {
                NARRATIVE_ATTEMPT_ID: "completed",
                STRUCTURED_ATTEMPT_ID: "adapter_failed",
            }
        null_bindings: JsonObject = {
            condition.value: None for condition in ComparisonCondition
        }
        bundle_manifest = self._bundle_manifest(
            outcome="adapter_failed",
            attempt_outcomes=attempt_outcomes,
            completed_branch_count=0,
            contains_private_artifacts=any(
                entry["visibility"] == "private" for entry in files
            ),
            branch_bindings=null_bindings,
            files=files,
        )
        validator.validate_manifest(bundle_manifest)
        return self._publish(bundle_manifest)
