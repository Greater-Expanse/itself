# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Atomic, private, digest-bound bundles for paired model comparisons."""

from __future__ import annotations

import hashlib
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Final, Protocol

from jsonschema.exceptions import ValidationError

from experiments._bundle_validation import (
    FILE_BYTE_LIMIT,
    LEDGER_BYTE_LIMIT,
    RECEIPT_BYTE_LIMIT,
    RecordingArtifactSink,
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
from experiments._contract_values import integer_value as _integer
from experiments._contract_values import object_value as _object
from experiments._contract_values import string_value as _string
from experiments._contract_values import string_values as _strings
from experiments._schema_validation import contract_validator
from experiments._study_contracts import (
    V1_CONTRACT,
    V2_CONTRACT,
    RunnerFailure,
    StudyContract,
    UnsupportedStudyContractError,
    declared_contract,
    pinned_shipped_adapter,
)
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
    comparison_manifest_contract,
)
from experiments.diagnostician import (
    DiagnosisRequest,
    DiagnosisResult,
    DiagnosticianContractValidator,
    RawOutputArtifact,
)
from experiments.model_adapters import (
    HttpTransport,
    ModelAdapterError,
    ShippedAdapterIdentity,
    StatusObservingTransport,
    running_shipped_adapter,
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

COMPARISON_BUNDLE_VERSION: Final = V1_CONTRACT.document_version
COMPARISON_ATTEMPT_REPORT_VERSION: Final = V1_CONTRACT.document_version

_BUNDLE_SCHEMA: Final = "comparison-bundle"
_ATTEMPT_SCHEMA: Final = "comparison-attempt-report"
_BUNDLE_MANIFEST_NAME: Final = "bundle.json"
_COMPARISON_MANIFEST_NAME: Final = "comparison-manifest.json"
_ATTEMPT_REPORT_NAME: Final = "attempt-report.json"
_NARRATIVE_RESULT_PATH: Final = f"attempts/{NARRATIVE_ATTEMPT_ID}/result.json"
_STRUCTURED_RESULT_PATH: Final = f"attempts/{STRUCTURED_ATTEMPT_ID}/result.json"
_NARRATIVE_ARTIFACT_PREFIX: Final = f"attempts/{NARRATIVE_ATTEMPT_ID}/private"
_STRUCTURED_ARTIFACT_PREFIX: Final = f"attempts/{STRUCTURED_ATTEMPT_ID}/private"
_BRANCH_NAMES: Final = tuple(condition.value for condition in ComparisonCondition)
# A v2 bundle also records a failure of the runner after a request.
_FAILED_OUTCOMES: Final = {
    V1_CONTRACT: ("adapter_failed",),
    V2_CONTRACT: ("adapter_failed", "runner_failed"),
}


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


def _document_contract(document: JsonValue, version_field: str) -> StudyContract:
    try:
        return declared_contract(document, version_field)
    except UnsupportedStudyContractError as error:
        raise ComparisonBundleValidationError(str(error)) from None


def _schema_issues(
    document: JsonValue,
    *,
    version_field: str,
    schema: str,
) -> list[str]:
    try:
        contract = declared_contract(document, version_field)
    except UnsupportedStudyContractError as error:
        return [str(error)]
    validator: _SchemaValidator = contract_validator(contract.schema_path(schema))
    issues = sorted(
        validator.iter_errors(document),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    return [
        f"{_format_path(tuple(issue.absolute_path))}: {issue.message}"
        for issue in issues
    ]


def _require_pinned_shipped_adapter(
    recorded: JsonValue,
    manifest: JsonObject,
    *,
    label: str,
) -> None:
    identity = ShippedAdapterIdentity.from_json_object(
        recorded,
        f"{label}.shipped_adapter",
    )
    if identity != pinned_shipped_adapter(manifest):
        raise ComparisonBundleValidationError(
            f"{label} shipped_adapter does not match the client its comparison "
            "manifest pins"
        )


def _artifact_sha256(path: Path) -> str:
    return _sha256_bytes(
        read_regular_file(
            path,
            max_bytes=FILE_BYTE_LIMIT,
            error_type=ComparisonBundleValidationError,
        )
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


def _artifact_prefix(attempt_id: str) -> str:
    prefix = {
        NARRATIVE_ATTEMPT_ID: _NARRATIVE_ARTIFACT_PREFIX,
        STRUCTURED_ATTEMPT_ID: _STRUCTURED_ARTIFACT_PREFIX,
    }.get(attempt_id)
    if prefix is None:
        raise ValueError(f"unsupported comparison attempt {attempt_id!r}")
    return prefix


def _require_narrative_for(
    attempt_id: str,
    narrative_result: NarrativeResult | None,
) -> None:
    if (attempt_id == STRUCTURED_ATTEMPT_ID) is (narrative_result is None):
        raise ComparisonBundleValidationError(
            "structured failure requires the completed narrative result"
        )


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
    """Validate inventory, bindings, projections, scorecards, and receipts.

    Each document is validated by the schema its own version declares, and a
    bundle's manifest and attempt report must declare the bundle's version.
    """

    def validate_manifest(self, bundle_manifest: JsonValue) -> None:
        """Validate comparison bundle metadata against its schema."""

        issues = _schema_issues(
            bundle_manifest,
            version_field="bundle_version",
            schema=_BUNDLE_SCHEMA,
        )
        if issues:
            raise ComparisonBundleValidationError("\n".join(issues))

    def validate_attempt_report(self, attempt_report: JsonValue) -> None:
        """Validate one sanitized failed-attempt report."""

        issues = _schema_issues(
            attempt_report,
            version_field="attempt_report_version",
            schema=_ATTEMPT_SCHEMA,
        )
        if issues:
            raise ComparisonBundleValidationError("\n".join(issues))

    def validate_attempt_report_against_manifest(
        self,
        attempt_report: JsonObject,
        manifest: JsonObject,
    ) -> None:
        """Validate a failure report and bind its profile to its manifest.

        A v2 report must also name the shipped client the manifest pins.
        """

        self.validate_attempt_report(attempt_report)
        contract = comparison_manifest_contract(manifest)
        if _document_contract(attempt_report, "attempt_report_version") is not contract:
            raise ComparisonBundleValidationError(
                "attempt report version does not match the comparison manifest"
            )
        attempt_id = _string(attempt_report["attempt_id"], "attempt_id")
        expected_profile_id = _attempt_profile_id(manifest, attempt_id)
        if attempt_report["profile_id"] != expected_profile_id:
            raise ComparisonBundleValidationError(
                "attempt report profile does not match the comparison manifest"
            )
        if contract is V2_CONTRACT:
            _require_pinned_shipped_adapter(
                attempt_report["shipped_adapter"],
                manifest,
                label="attempt report",
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
        manifest = _load_json_object(root / _COMPARISON_MANIFEST_NAME)
        ComparisonManifestValidator().validate(manifest)
        bundle = inventory.bundle_manifest
        contract = comparison_manifest_contract(manifest)
        if contract is not _document_contract(bundle, "bundle_version"):
            raise ComparisonBundleValidationError(
                "bundle_version does not match its comparison manifest's version"
            )
        if contract is V2_CONTRACT:
            _require_pinned_shipped_adapter(
                bundle["shipped_adapter"],
                manifest,
                label="bundle",
            )
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
        if _artifact_sha256(root / _normalized_relative_path(uri)) != artifact.sha256:
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

        ledger = JsonlLedgerStore(root / _normalized_relative_path(ledger_path)).load(
            max_bytes=LEDGER_BYTE_LIMIT
        )
        receipt = JsonReceiptStore(root / _normalized_relative_path(receipt_path)).load(
            max_bytes=RECEIPT_BYTE_LIMIT
        )
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
        """Verify a stopped comparison and its sanitized failure boundary.

        Under v1 the stop is an adapter failure; v2 also records a runner
        failure after a request was sent.
        """

        root = Path(path)
        inventory = self._validate_inventory(root)
        bundle = inventory.bundle_manifest
        failed_outcomes = _FAILED_OUTCOMES[_document_contract(bundle, "bundle_version")]
        if bundle["outcome"] not in failed_outcomes:
            raise ComparisonBundleValidationError(
                "comparison bundle outcome is not " + " or ".join(failed_outcomes)
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
        if attempt_report["outcome"] != bundle["outcome"]:
            raise ComparisonBundleValidationError(
                "attempt report outcome does not match its bundle"
            )

        outcomes = _object(bundle["attempt_outcomes"], "attempt_outcomes")
        failed_attempt = _string(attempt_report["attempt_id"], "attempt_id")
        if outcomes.get(failed_attempt) != bundle["outcome"]:
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
        require_bundle_root(root, error_type=ComparisonBundleValidationError)
        bundle_manifest = _load_json_object(root / _BUNDLE_MANIFEST_NAME)
        self.validate_manifest(bundle_manifest)
        if bundle_manifest["outcome"] == "completed":
            return self.validate_completed(root, request)
        return self.validate_failure(root, request)


class ComparisonBundleBuilder:
    """Build one immutable comparison bundle, verify it, then publish atomically.

    Once staging holds a captured response, an exception never deletes it:
    the staging directory is renamed to ``<destination>.quarantined`` and an
    ``AttemptQuarantinedError`` names that path.

    The bundle has the version of its manifest.  A v2 bundle records the
    shipped client that ran, takes a failed attempt's status from the
    transport that ``observed_transport`` returns for that attempt, and
    records a runner failure after a request with ``fail_runner``.
    Quarantine is then left for a replicate whose failure bundle cannot
    itself be published.
    """

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
        self._contract = comparison_manifest_contract(comparison_manifest)
        self._request = request
        self._replicate_index = replicate_index
        self._now = now
        self._created_at = _timestamp(now())
        self._staging: Path | None = None
        self._published = False
        self._sinks: dict[str, RecordingArtifactSink] = {}
        self._observers: dict[str, StatusObservingTransport] = {}
        self._written: list[Path] = []

    def __enter__(self) -> ComparisonBundleBuilder:
        if self._staging is not None or self._published:
            raise ComparisonBundleStateError(
                "comparison bundle builder cannot be entered twice"
            )
        require_unused_destination(self.destination)
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
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, traceback
        staging, self._staging = self._staging, None
        close_staging(staging, self.destination, exception)

    def _active_root(self) -> Path:
        if self._staging is None or self._published:
            raise ComparisonBundleStateError("comparison bundle builder is not active")
        return self._staging

    def _artifact_sink(self, prefix: str) -> RecordingArtifactSink:
        root = self._active_root()
        artifact_root = root / _normalized_relative_path(prefix)
        ensure_private_directory(root, artifact_root)
        sink = self._sinks.get(prefix)
        if sink is None:
            sink = RecordingArtifactSink(artifact_root, uri_prefix=prefix)
            self._sinks[prefix] = sink
        return sink

    @property
    def narrative_artifact_sink(self) -> RecordingArtifactSink:
        """Return the raw-response sink for the narrative attempt."""

        return self._artifact_sink(_NARRATIVE_ARTIFACT_PREFIX)

    @property
    def structured_artifact_sink(self) -> RecordingArtifactSink:
        """Return the raw-response sink for the structured attempt."""

        return self._artifact_sink(_STRUCTURED_ARTIFACT_PREFIX)

    def observed_transport(
        self,
        transport: HttpTransport,
        *,
        attempt_id: str,
    ) -> HttpTransport:
        """Return ``transport`` wrapped so a v2 bundle records one attempt.

        The attempt's request must be sent through the returned transport: a
        v2 failed attempt keeps the status it observed.
        """

        self._active_root()
        _artifact_prefix(attempt_id)
        if self._contract is not V2_CONTRACT:
            raise ComparisonBundleStateError(
                "only a v0.3.0 comparison bundle records an observed status"
            )
        if attempt_id in self._observers:
            raise ComparisonBundleStateError(
                f"attempt {attempt_id!r} already has an observed transport"
            )
        observer = StatusObservingTransport(transport)
        self._observers[attempt_id] = observer
        return observer

    @property
    def records_runner_failure(self) -> bool:
        """Whether ``fail_runner`` would now publish a runner-failed bundle.

        That needs an unpublished v2 bundle that has sent a request; before
        the first request there is no attempt to keep.
        """

        return (
            self._contract is V2_CONTRACT
            and self._staging is not None
            and not self._published
            and any(observer.request_sent for observer in self._observers.values())
        )

    def _write(self, path: Path, content: bytes) -> None:
        # The path is noted first, so a write that fails part-way is removed
        # with the rest when a runner failure replaces what was written.
        self._written.append(path)
        _write_private_bytes(self._active_root(), path, content)

    def _discard_written(self) -> None:
        for path in reversed(self._written):
            path.unlink(missing_ok=True)
        self._written.clear()

    def _observed_status(self, attempt_id: str) -> int | None:
        observer = self._observers.get(attempt_id)
        if observer is None:
            raise ComparisonBundleStateError(
                "a v0.3.0 comparison bundle records the observed status, so "
                f"attempt {attempt_id!r} must send through observed_transport"
            )
        return observer.status_code

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
        if _artifact_sha256(path) != artifact.sha256:
            raise ComparisonBundleValidationError(
                "captured provider artifact does not match its result digest"
            )

    def _write_branch(self, branch: ComparisonBranchResult) -> list[JsonObject]:
        root = self._active_root()
        ledger_path = _branch_path(branch.condition, "ledger.jsonl")
        scorecard_path = _branch_path(branch.condition, "scorecard.json")
        receipt_path = _branch_path(branch.condition, "reasoning-receipt.json")
        # A bundle records only receipts that recompute, so a branch whose
        # receipt does not is a failure that keeps its captured responses.
        if not (
            branch.scorecard.receipt_recomputes
            and receipt_recomputes(branch.receipt, branch.ledger)
        ):
            raise ComparisonBundleValidationError(
                f"{branch.condition.value} receipt does not recompute from its ledger"
            )
        scorecard = branch.scorecard.to_json_object()
        declared_metrics = set(_strings(self._manifest["metrics"], "metrics"))
        if set(scorecard) != declared_metrics:
            raise ComparisonBundleValidationError(
                f"{branch.condition.value} scorecard differs from registered metrics"
            )
        self._write(
            root / _normalized_relative_path(ledger_path),
            canonical_ledger_bytes(branch.ledger),
        )
        self._write(
            root / _normalized_relative_path(scorecard_path),
            _pretty_json_bytes(scorecard),
        )
        self._write(
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
            "bundle_version": self._contract.document_version,
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
        if self._contract is V2_CONTRACT:
            without_id["shipped_adapter"] = running_shipped_adapter().to_json_object()
        bundle_id = (
            "urn:sha256:" + hashlib.sha256(canonical_json_bytes(without_id)).hexdigest()
        )
        return {"bundle_id": bundle_id, **without_id}

    def _publish(self, bundle_manifest: JsonObject) -> JsonObject:
        root = self._active_root()
        self._write(root / _BUNDLE_MANIFEST_NAME, _pretty_json_bytes(bundle_manifest))
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
        self._write(
            root / _COMPARISON_MANIFEST_NAME,
            _pretty_json_bytes(self._manifest),
        )
        self._write(
            root / _normalized_relative_path(_NARRATIVE_RESULT_PATH),
            _pretty_json_bytes(result.narrative_result.to_json_object()),
        )
        self._write(
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

    def _failure_status(
        self,
        error: ModelAdapterError,
        attempt_id: str,
    ) -> int | None:
        if self._contract is V1_CONTRACT:
            return recordable_status_code(error.status_code)
        observed = self._observed_status(attempt_id)
        if error.status_code is not None and error.status_code != observed:
            raise ComparisonBundleValidationError(
                "adapter failure status differs from the status the transport observed"
            )
        return observed

    def _publish_failure(
        self,
        *,
        outcome: str,
        attempt_id: str,
        narrative_result: NarrativeResult | None,
        failure: JsonObject,
        artifact: RawOutputArtifact | None,
    ) -> JsonObject:
        root = self._active_root()
        self._write(
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
            self._write(
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

        if artifact is not None:
            self._require_captured_artifact(
                artifact,
                expected_prefix=_artifact_prefix(attempt_id),
            )
            failure["response_artifact"] = artifact.to_json_object()
            files.append(
                _file_entry(
                    root,
                    artifact.uri,
                    media_type=artifact.media_type,
                    visibility="private",
                )
            )

        attempt_report: JsonObject = {
            "attempt_report_version": self._contract.document_version,
            "comparison_series_id": self._manifest["comparison_series_id"],
            "attempt_id": attempt_id,
            "profile_id": _attempt_profile_id(self._manifest, attempt_id),
            "outcome": outcome,
            "completed_at": _timestamp(self._now()),
            "failure": failure,
        }
        if self._contract is V2_CONTRACT:
            attempt_report["shipped_adapter"] = (
                running_shipped_adapter().to_json_object()
            )
        validator = ComparisonBundleValidator()
        validator.validate_attempt_report_against_manifest(
            attempt_report,
            self._manifest,
        )
        self._write(
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
                NARRATIVE_ATTEMPT_ID: outcome,
                STRUCTURED_ATTEMPT_ID: "not_run",
            }
        else:
            attempt_outcomes = {
                NARRATIVE_ATTEMPT_ID: "completed",
                STRUCTURED_ATTEMPT_ID: outcome,
            }
        null_bindings: JsonObject = {
            condition.value: None for condition in ComparisonCondition
        }
        bundle_manifest = self._bundle_manifest(
            outcome=outcome,
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

    def fail(
        self,
        error: ModelAdapterError,
        *,
        attempt_id: str,
        narrative_result: NarrativeResult | None = None,
    ) -> JsonObject:
        """Retain a sanitized attempt failure and stop the paired replicate.

        A v1 report clamps the adapter's status to 100-599; a v2 report keeps
        the status the attempt's transport observed, or null when no response
        arrived.
        """

        _artifact_prefix(attempt_id)
        _require_narrative_for(attempt_id, narrative_result)
        self._active_root()
        failure: JsonObject = {
            "kind": error.failure.value,
            "retryable": error.retryable,
            "status_code": self._failure_status(error, attempt_id),
        }
        return self._publish_failure(
            outcome="adapter_failed",
            attempt_id=attempt_id,
            narrative_result=narrative_result,
            failure=failure,
            artifact=error.artifact,
        )

    def _last_captured_artifact(self, attempt_id: str) -> RawOutputArtifact | None:
        sink = self._sinks.get(_artifact_prefix(attempt_id))
        if sink is None or not sink.captured:
            return None
        reference = sink.captured[-1]
        return RawOutputArtifact(
            uri=reference.uri,
            media_type=reference.media_type,
            sha256=reference.sha256,
            captured_at=reference.captured_at,
        )

    def fail_runner(
        self,
        failure: RunnerFailure,
        *,
        attempt_id: str,
        narrative_result: NarrativeResult | None = None,
    ) -> JsonObject:
        """Publish one v2 runner failure after a request, keeping its responses.

        ``attempt_id`` names the attempt whose result was not yet recorded:
        the attempt in progress, or the structured attempt when the failure
        followed both responses.  Files written by an earlier ``complete`` or
        ``fail`` are derived from the responses, so they are removed; a
        captured response never is.
        """

        _artifact_prefix(attempt_id)
        _require_narrative_for(attempt_id, narrative_result)
        if not self.records_runner_failure:
            raise ComparisonBundleStateError(
                "a runner failure is recorded only by an unpublished v0.3.0 "
                "comparison bundle that has sent a request"
            )
        status_code = (
            self._observers[attempt_id].status_code
            if attempt_id in self._observers
            else None
        )
        self._discard_written()
        failure_json: JsonObject = {
            "kind": failure.kind,
            "retryable": False,
            "status_code": status_code,
            "error": failure.error_json_object(),
        }
        return self._publish_failure(
            outcome="runner_failed",
            attempt_id=attempt_id,
            narrative_result=narrative_result,
            failure=failure_json,
            artifact=self._last_captured_artifact(attempt_id),
        )
