# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Run a deterministic SDK lifecycle and adversarial mutation campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Final, Protocol, cast

from examples.inference_to_evidence.run import run_example
from itself import (
    BundleValidator,
    EvidenceBundleValidationError,
    EvidenceBundleValidator,
    IntegrityCode,
    JsonObject,
    JsonValue,
    ProtocolValidator,
    ReasoningReceiptValidationError,
    ReasoningReceiptValidator,
    VerifiedEvidenceBundle,
    __version__,
    evidence_bundle_id,
    export_schemas,
)
from itself._filesystem import publish_path_no_replace
from itself._json import strict_json_loads

from .reporting import (
    EvaluationCheck,
    EvaluationEnvironment,
    EvaluationOutcome,
    EvaluationReport,
    EvaluationStatus,
    build_evaluation_report,
    write_evaluation_report,
)

ROOT: Final = Path(__file__).resolve().parents[1]
INVALID_RECORD_ROOT: Final = ROOT / "conformance" / "invalid"
INVALID_BUNDLE_ROOT: Final = ROOT / "conformance" / "bundles" / "invalid"
EXPECTED_RECORD_KINDS: Final = frozenset(
    {
        "artifact_reference",
        "claim",
        "decision",
        "evidence",
        "hypothesis",
        "prediction",
        "status_transition",
        "test",
        "verdict",
    }
)
EXPECTED_INTEGRITY_CODES: Final[dict[str, IntegrityCode]] = {
    "dangling-reference.json": IntegrityCode.UNRESOLVED_REFERENCE,
    "duplicate-id.json": IntegrityCode.DUPLICATE_ID,
    "invalid-transition.json": IntegrityCode.INVALID_TRANSITION,
    "state-drift.json": IntegrityCode.STATE_MISMATCH,
    "test-plan-kind-mismatch.json": IntegrityCode.REFERENCE_KIND_MISMATCH,
    "transition-subject-mismatch.json": IntegrityCode.TRANSITION_SUBJECT_MISMATCH,
    "unrelated-evidence.json": IntegrityCode.EVIDENCE_RELATION_MISSING,
    "verdict-after-transition.json": IntegrityCode.REFERENCE_NOT_AVAILABLE,
    "wrong-reference-kind.json": IntegrityCode.REFERENCE_KIND_MISMATCH,
}
LIMITATIONS: Final = (
    "The lifecycle uses a deterministic reviewed inference fixture, not a hosted model.",
    "The mutation campaign covers declared fixtures and does not prove resistance to every possible fault.",
    "Structural, cryptographic, and replay integrity do not establish real-world factual truth.",
)


class MonotonicClock(Protocol):
    """Callable monotonic nanosecond clock used for check observations."""

    def __call__(self) -> int: ...


class EvaluationSkip(RuntimeError):
    """Signal that a capability cannot be exercised in the current environment."""


@dataclass(frozen=True, slots=True)
class CheckObservation:
    """Sanitized result returned by one successful evaluation check."""

    summary: str
    details: Mapping[str, JsonValue]


@dataclass(slots=True)
class ExerciseContext:
    """Mutable artifacts shared by dependent checks within one run."""

    staging: Path
    workspace: Path
    bundle: VerifiedEvidenceBundle | None = None


CheckFunction = Callable[[], CheckObservation]


def _load_json(path: Path) -> JsonValue:
    return strict_json_loads(path.read_text(encoding="utf-8"))


def _load_json_object(path: Path) -> JsonObject:
    value = _load_json(path)
    if not isinstance(value, dict):
        raise TypeError(f"{path.name} must contain a JSON object")
    return value


def _load_record_bundle(path: Path) -> list[JsonObject]:
    value = _load_json(path)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError(f"{path.name} must contain a JSON object array")
    return cast(list[JsonObject], value)


def _pretty_json(value: JsonValue) -> bytes:
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


def _file_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _run_check(
    *,
    check_id: str,
    category: str,
    expected: str,
    function: CheckFunction,
    monotonic_ns: MonotonicClock,
) -> EvaluationCheck:
    started = monotonic_ns()
    try:
        observation = function()
    except EvaluationSkip as error:
        status = EvaluationStatus.SKIPPED
        summary = str(error)
        details: Mapping[str, JsonValue] = {
            "reason": str(error),
        }
    except Exception as error:
        status = EvaluationStatus.FAILED
        summary = f"{type(error).__name__}: {error}"
        details = {
            "error_type": type(error).__name__,
            "error": str(error),
        }
    else:
        status = EvaluationStatus.PASSED
        summary = observation.summary
        details = observation.details
    elapsed_ms = max(0, (monotonic_ns() - started) // 1_000_000)
    return EvaluationCheck(
        check_id=check_id,
        category=category,
        status=status,
        duration_ms=elapsed_ms,
        expected=expected,
        observed=summary,
        details=details,
    )


def _schema_export_check(context: ExerciseContext) -> CheckObservation:
    snapshot = export_schemas(context.workspace / "schema-export")
    if len(snapshot.schemas) != 6:
        raise AssertionError("canonical export did not contain both schema lines")
    schemas: list[JsonValue] = [
        {
            "name": schema.name,
            "schema_id": schema.schema_id,
            "sha256": schema.sha256,
        }
        for schema in snapshot.schemas
    ]
    return CheckObservation(
        summary="Exported six content-identified schemas across two immutable lines.",
        details={"schemas": schemas},
    )


def _lifecycle_check(context: ExerciseContext) -> CheckObservation:
    bundle = run_example(
        context.staging / "artifacts" / "lifecycle-bundle",
        private_artifact_root=context.workspace / "private-primary",
    )
    kinds = frozenset(cast(str, record["kind"]) for record in bundle.ledger.records)
    if kinds != EXPECTED_RECORD_KINDS:
        raise AssertionError(
            f"lifecycle record kinds differ: {sorted(kinds ^ EXPECTED_RECORD_KINDS)}"
        )
    states: JsonObject = {
        subject: status.value
        for subject, status in bundle.ledger.snapshot.current_states.items()
    }
    if set(states.values()) != {"supported"}:
        raise AssertionError("the fixture lifecycle did not end in supported states")
    record_kinds: list[JsonValue] = [kind for kind in sorted(kinds)]
    context.bundle = bundle
    return CheckObservation(
        summary=(
            "Completed assertion, test, evidence, verdict, promotion, and decision."
        ),
        details={
            "artifact_path": "artifacts/lifecycle-bundle",
            "bundle_id": bundle.manifest["bundle_id"],
            "ledger_record_count": len(bundle.ledger),
            "record_kinds": record_kinds,
            "current_states": states,
        },
    )


def _require_bundle(context: ExerciseContext) -> VerifiedEvidenceBundle:
    if context.bundle is None:
        raise EvaluationSkip("The lifecycle bundle was unavailable.")
    return context.bundle


def _receipt_check(context: ExerciseContext) -> CheckObservation:
    bundle = _require_bundle(context)
    ReasoningReceiptValidator().validate_against_ledger(
        bundle.receipt,
        bundle.ledger,
    )
    return CheckObservation(
        summary="Recomputed the reasoning receipt exactly from its ledger.",
        details={"receipt_id": bundle.receipt["receipt_id"]},
    )


def _bundle_check(context: ExerciseContext) -> CheckObservation:
    bundle = _require_bundle(context)
    verified = EvidenceBundleValidator().validate(bundle.path)
    if verified.manifest != bundle.manifest:
        raise AssertionError("independent bundle validation changed the manifest")
    return CheckObservation(
        summary="Independently revalidated every inventoried bundle byte.",
        details={
            "bundle_id": verified.manifest["bundle_id"],
            "file_count": len(cast(list[JsonValue], verified.manifest["files"])),
        },
    )


def _reproduction_check(context: ExerciseContext) -> CheckObservation:
    bundle = _require_bundle(context)
    reproduced = run_example(
        context.workspace / "reproduced-bundle",
        private_artifact_root=context.workspace / "private-reproduction",
    )
    first_files = _file_tree(bundle.path)
    reproduced_files = _file_tree(reproduced.path)
    if first_files != reproduced_files:
        raise AssertionError("deterministic lifecycle bundles differ")
    if bundle.manifest["bundle_id"] != reproduced.manifest["bundle_id"]:
        raise AssertionError("deterministic lifecycle bundle identifiers differ")
    return CheckObservation(
        summary="Reproduced byte-identical lifecycle bundle contents.",
        details={
            "bundle_id": bundle.manifest["bundle_id"],
            "file_count": len(first_files),
        },
    )


def _invalid_record_check(path: Path) -> CheckObservation:
    issues = ProtocolValidator().errors(_load_json(path))
    if not issues:
        raise AssertionError(f"{path.name} was unexpectedly accepted")
    return CheckObservation(
        summary=f"Rejected invalid record fixture {path.name}.",
        details={
            "fixture": path.name,
            "issue_count": len(issues),
            "issues": list(issues),
        },
    )


def _invalid_bundle_check(
    path: Path,
    expected_code: IntegrityCode,
) -> CheckObservation:
    issues = BundleValidator().errors(_load_record_bundle(path))
    if len(issues) != 1 or issues[0].code is not expected_code:
        observed = [issue.code.value for issue in issues]
        raise AssertionError(
            f"{path.name} produced {observed}, expected {expected_code.value}"
        )
    return CheckObservation(
        summary=f"Rejected {path.name} with {expected_code.value}.",
        details={
            "fixture": path.name,
            "integrity_code": expected_code.value,
            "record_id": issues[0].record_id,
            "reference": issues[0].reference,
        },
    )


def _mutation_root(
    context: ExerciseContext,
    name: str,
) -> Path:
    bundle = _require_bundle(context)
    destination = context.workspace / name
    shutil.copytree(bundle.path, destination)
    return destination


def _artifact_tamper_check(context: ExerciseContext) -> CheckObservation:
    root = _mutation_root(context, "artifact-tamper")
    artifact = root / "artifacts" / "model-assertion.json"
    artifact.write_bytes(artifact.read_bytes() + b"\n")
    try:
        EvidenceBundleValidator().validate(root)
    except EvidenceBundleValidationError as error:
        if not any(
            detail in str(error) for detail in ("size mismatch", "digest mismatch")
        ):
            raise AssertionError(f"unexpected artifact rejection: {error}") from error
    else:
        raise AssertionError("tampered artifact bytes were accepted")
    return CheckObservation(
        summary="Rejected artifact bytes that no longer matched their inventory.",
        details={
            "mutation": "artifact-byte-append",
            "rejection_stage": "inventory_size_or_digest",
        },
    )


def _receipt_binding_check(context: ExerciseContext) -> CheckObservation:
    root = _mutation_root(context, "receipt-binding-tamper")
    receipt_path = root / "reasoning-receipt.json"
    receipt = _load_json_object(receipt_path)
    limitations = receipt["limitations"]
    if not isinstance(limitations, list):
        raise TypeError("receipt limitations must be an array")
    limitations.append("Schema-valid but not derived from the source ledger.")
    receipt_content = _pretty_json(receipt)
    receipt_path.write_bytes(receipt_content)

    manifest_path = root / "bundle.json"
    manifest = _load_json_object(manifest_path)
    entries = manifest["files"]
    if not isinstance(entries, list) or not all(
        isinstance(item, dict) for item in entries
    ):
        raise TypeError("bundle files must be an object array")
    entry_values = cast(list[JsonObject], entries)
    receipt_entry = next(
        entry for entry in entry_values if entry.get("path") == "reasoning-receipt.json"
    )
    receipt_entry["size_bytes"] = len(receipt_content)
    receipt_entry["digest"] = {
        "algorithm": "sha256",
        "value": hashlib.sha256(receipt_content).hexdigest(),
    }
    identity = {
        key: deepcopy(value) for key, value in manifest.items() if key != "bundle_id"
    }
    manifest["bundle_id"] = evidence_bundle_id(identity)
    manifest_path.write_bytes(_pretty_json(manifest))

    try:
        EvidenceBundleValidator().validate(root)
    except ReasoningReceiptValidationError as error:
        if "does not match" not in str(error):
            raise AssertionError(f"unexpected receipt rejection: {error}") from error
    else:
        raise AssertionError("schema-valid receipt drift was accepted")
    return CheckObservation(
        summary="Rejected a re-digested receipt that diverged from its ledger.",
        details={
            "mutation": "receipt-limitation-append-and-rebind",
            "rejection_stage": "receipt_ledger_recomputation",
        },
    )


def _uninventoryed_file_check(context: ExerciseContext) -> CheckObservation:
    root = _mutation_root(context, "uninventoryed-file")
    (root / "unexpected.txt").write_text("not inventoried\n", encoding="utf-8")
    try:
        EvidenceBundleValidator().validate(root)
    except EvidenceBundleValidationError as error:
        if "inventory mismatch" not in str(error):
            raise AssertionError(f"unexpected inventory rejection: {error}") from error
    else:
        raise AssertionError("uninventoryed bundle file was accepted")
    return CheckObservation(
        summary="Rejected a bundle containing an uninventoryed file.",
        details={
            "mutation": "uninventoryed-file",
            "rejection_stage": "closed_inventory",
        },
    )


def _symlink_check(context: ExerciseContext) -> CheckObservation:
    root = _mutation_root(context, "symbolic-link")
    artifact = root / "artifacts" / "model-assertion.json"
    external = context.workspace / "external-model-assertion.json"
    external.write_bytes(artifact.read_bytes())
    artifact.unlink()
    try:
        artifact.symlink_to(external)
    except OSError as error:
        raise EvaluationSkip(
            "Symbolic links are unavailable on this platform."
        ) from error
    try:
        EvidenceBundleValidator().validate(root)
    except EvidenceBundleValidationError:
        pass
    else:
        raise AssertionError("symbolic-link bundle artifact was accepted")
    return CheckObservation(
        summary="Rejected a bundle whose artifact path became a symbolic link.",
        details={
            "mutation": "artifact-symbolic-link",
            "rejection_stage": "filesystem_boundary",
        },
    )


def _checks(
    context: ExerciseContext,
    monotonic_ns: MonotonicClock,
) -> tuple[EvaluationCheck, ...]:
    checks = [
        _run_check(
            check_id="schemas.export",
            category="schema portability",
            expected="Canonical schemas export with stable identities.",
            function=lambda: _schema_export_check(context),
            monotonic_ns=monotonic_ns,
        ),
        _run_check(
            check_id="lifecycle.complete",
            category="end-to-end lifecycle",
            expected="Every protocol record kind participates in a verified workflow.",
            function=lambda: _lifecycle_check(context),
            monotonic_ns=monotonic_ns,
        ),
        _run_check(
            check_id="lifecycle.receipt-recomputes",
            category="end-to-end lifecycle",
            expected="The reasoning receipt recomputes exactly from the ledger.",
            function=lambda: _receipt_check(context),
            monotonic_ns=monotonic_ns,
        ),
        _run_check(
            check_id="lifecycle.bundle-revalidates",
            category="end-to-end lifecycle",
            expected="Independent validation accepts every inventoried bundle byte.",
            function=lambda: _bundle_check(context),
            monotonic_ns=monotonic_ns,
        ),
        _run_check(
            check_id="lifecycle.deterministic-reproduction",
            category="determinism",
            expected="The same fixture produces byte-identical bundle contents.",
            function=lambda: _reproduction_check(context),
            monotonic_ns=monotonic_ns,
        ),
    ]

    for path in sorted(INVALID_RECORD_ROOT.glob("*.json")):
        checks.append(
            _run_check(
                check_id=f"mutation.record.{path.stem}",
                category="record mutation",
                expected="The invalid record is rejected by the protocol schema.",
                function=partial(_invalid_record_check, path),
                monotonic_ns=monotonic_ns,
            )
        )
    for filename, expected_code in sorted(EXPECTED_INTEGRITY_CODES.items()):
        path = INVALID_BUNDLE_ROOT / filename
        checks.append(
            _run_check(
                check_id=f"mutation.ledger.{path.stem}",
                category="ledger mutation",
                expected=f"The ledger is rejected with {expected_code.value}.",
                function=partial(_invalid_bundle_check, path, expected_code),
                monotonic_ns=monotonic_ns,
            )
        )

    checks.extend(
        (
            _run_check(
                check_id="mutation.bundle.artifact-digest",
                category="evidence-bundle mutation",
                expected="Changed artifact bytes are rejected by inventory identity.",
                function=lambda: _artifact_tamper_check(context),
                monotonic_ns=monotonic_ns,
            ),
            _run_check(
                check_id="mutation.bundle.receipt-binding",
                category="evidence-bundle mutation",
                expected="Schema-valid receipt drift is rejected by ledger recomputation.",
                function=lambda: _receipt_binding_check(context),
                monotonic_ns=monotonic_ns,
            ),
            _run_check(
                check_id="mutation.bundle.closed-inventory",
                category="evidence-bundle mutation",
                expected="Files absent from the manifest inventory are rejected.",
                function=lambda: _uninventoryed_file_check(context),
                monotonic_ns=monotonic_ns,
            ),
            _run_check(
                check_id="mutation.bundle.symbolic-link",
                category="evidence-bundle mutation",
                expected="Symbolic-link artifact paths are rejected.",
                function=lambda: _symlink_check(context),
                monotonic_ns=monotonic_ns,
            ),
        )
    )
    return tuple(checks)


def run_sdk_exercise(
    output: Path,
    *,
    source_revision: str | None = None,
    generated_at: datetime | None = None,
    environment: EvaluationEnvironment | None = None,
    monotonic_ns: MonotonicClock = time.monotonic_ns,
) -> EvaluationReport:
    """Run, report, and atomically publish one deterministic SDK exercise."""

    target = Path(output)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            dir=target.parent,
            prefix=f".{target.name}.staging-",
        )
    )
    staging.chmod(stat.S_IRWXU)
    published = False
    try:
        with tempfile.TemporaryDirectory(prefix="itself-sdk-exercise-") as workspace:
            context = ExerciseContext(
                staging=staging,
                workspace=Path(workspace),
            )
            checks = _checks(context, monotonic_ns)
        report = build_evaluation_report(
            suite_id="sdk-exercise",
            generated_at=generated_at or datetime.now(UTC),
            sdk_version=__version__,
            source_revision=source_revision,
            environment=environment or EvaluationEnvironment.current(),
            checks=checks,
            limitations=LIMITATIONS,
        )
        write_evaluation_report(staging, report)
        publish_path_no_replace(staging, target)
        published = True
        return report
    finally:
        if not published:
            shutil.rmtree(staging, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--source-revision",
        default=os.environ.get("GITHUB_SHA"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the SDK exercise and return failure only for broken guarantees."""

    args = _parser().parse_args(argv)
    output = cast(Path, args.output)
    source_revision = cast(str | None, args.source_revision)
    try:
        report = run_sdk_exercise(
            output,
            source_revision=source_revision,
        )
    except (OSError, ValueError) as error:
        print(f"FAIL {output}: {error}")
        return 2

    print(
        f"{report.outcome.value.upper()} {output}: "
        f"{len(report.checks)} checks, {report.run_id}"
    )
    return 0 if report.outcome is EvaluationOutcome.PASSED else 1


if __name__ == "__main__":
    raise SystemExit(main())
