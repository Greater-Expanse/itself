# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Observe SDK scaling across synthetic ledger sizes without pass thresholds."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol, TypeVar, cast

from itself import (
    Actor,
    ActorRole,
    ActorType,
    ClaimType,
    EvidenceBundleBuilder,
    EvidenceBundleFile,
    EvidenceBundleFileRole,
    EvidenceBundleValidator,
    JsonlLedgerStore,
    JsonObject,
    JsonValue,
    Ledger,
    ReasoningReceiptValidator,
    RecordHeader,
    Scope,
    __version__,
    build_reasoning_receipt,
    claim_record,
)
from itself._filesystem import publish_path_no_replace

from .reporting import (
    EvaluationCheck,
    EvaluationEnvironment,
    EvaluationOutcome,
    EvaluationReport,
    EvaluationStatus,
    build_evaluation_report,
    write_evaluation_report,
)

DEFAULT_SIZES: Final = (10, 100, 1_000)
WORKLOAD_CREATED_AT: Final = datetime(2026, 7, 24, 16, tzinfo=UTC)
LIMITATIONS: Final = (
    "The workload contains independent unverified claims, not a deeply connected evidence graph.",
    "One run on one machine is a capacity observation, not a portable performance benchmark.",
    "No latency threshold or regression gate is inferred from these observations.",
)

T = TypeVar("T")


class MonotonicClock(Protocol):
    """Callable monotonic nanosecond clock used for capacity measurements."""

    def __call__(self) -> int: ...


def _timed(function: Callable[[], T], monotonic_ns: MonotonicClock) -> tuple[T, int]:
    started = monotonic_ns()
    value = function()
    return value, max(0, monotonic_ns() - started)


def _milliseconds(elapsed_ns: int) -> float:
    return round(elapsed_ns / 1_000_000, 3)


def _records(size: int) -> tuple[JsonObject, ...]:
    actor = Actor(
        actor_id="capacity-generator",
        actor_type=ActorType.SOFTWARE,
        role=ActorRole.OPERATOR,
        implementation_ref="evaluations.capacity_curve@0.1.0",
    )
    scope = Scope(
        description="Synthetic independent-claim capacity observation",
        dimensions={
            "suite": "capacity-curve",
            "record_count": size,
        },
    )
    return tuple(
        claim_record(
            RecordHeader(
                record_id=f"capacity-claim-{index:08d}",
                created_at=WORKLOAD_CREATED_AT,
                created_by=actor,
            ),
            proposition=f"Synthetic claim {index} remains unverified.",
            claim_type=ClaimType.FACTUAL,
            scope=scope,
        )
        for index in range(size)
    )


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


def _sample(
    *,
    size: int,
    staging: Path,
    workspace: Path,
    monotonic_ns: MonotonicClock,
) -> EvaluationCheck:
    check_started = monotonic_ns()
    try:
        records, construction_ns = _timed(
            lambda: _records(size),
            monotonic_ns,
        )
        ledger, ledger_validation_ns = _timed(
            lambda: Ledger(records),
            monotonic_ns,
        )

        batch_store = JsonlLedgerStore(workspace / f"batch-{size}.jsonl")
        snapshot, batch_write_ns = _timed(
            lambda: batch_store.extend(records),
            monotonic_ns,
        )
        loaded, batch_load_ns = _timed(
            batch_store.load,
            monotonic_ns,
        )
        if snapshot.record_count != size or len(loaded) != size:
            raise AssertionError("batch store record count changed")

        append_store = JsonlLedgerStore(workspace / f"append-{size}.jsonl")
        append_store.extend(records[:-1])
        append_snapshot, marginal_append_ns = _timed(
            lambda: append_store.append(records[-1]),
            monotonic_ns,
        )
        if append_snapshot.record_count != size:
            raise AssertionError("marginal append record count changed")

        receipt, receipt_build_ns = _timed(
            lambda: build_reasoning_receipt(ledger),
            monotonic_ns,
        )
        _, receipt_validation_ns = _timed(
            lambda: ReasoningReceiptValidator().validate_against_ledger(
                receipt,
                ledger,
            ),
            monotonic_ns,
        )
        receipt_content = _pretty_json(receipt)

        workload_content = _pretty_json(
            {
                "suite": "capacity-curve",
                "record_count": size,
                "record_shape": "independent_unverified_claim",
            }
        )
        bundle_path = staging / "artifacts" / f"records-{size}"
        bundle, bundle_build_ns = _timed(
            lambda: EvidenceBundleBuilder().build(
                bundle_path,
                ledger=ledger,
                title=f"Synthetic capacity observation: {size} records",
                created_at=WORKLOAD_CREATED_AT,
                files=(
                    EvidenceBundleFile(
                        path="inputs/workload.json",
                        content=workload_content,
                        media_type="application/json",
                        role=EvidenceBundleFileRole.INPUT,
                    ),
                ),
            ),
            monotonic_ns,
        )
        verified, bundle_validation_ns = _timed(
            lambda: EvidenceBundleValidator().validate(bundle.path),
            monotonic_ns,
        )
        if len(verified.ledger) != size:
            raise AssertionError("verified evidence bundle record count changed")

        metrics: JsonObject = {
            "record_count": size,
            "record_construction_ms": _milliseconds(construction_ns),
            "ledger_validation_ms": _milliseconds(ledger_validation_ns),
            "batch_store_write_ms": _milliseconds(batch_write_ns),
            "batch_store_load_ms": _milliseconds(batch_load_ns),
            "marginal_append_ms": _milliseconds(marginal_append_ns),
            "receipt_build_ms": _milliseconds(receipt_build_ns),
            "receipt_validation_ms": _milliseconds(receipt_validation_ns),
            "bundle_build_ms": _milliseconds(bundle_build_ns),
            "bundle_validation_ms": _milliseconds(bundle_validation_ns),
            "ledger_bytes": batch_store.path.stat().st_size,
            "receipt_bytes": len(receipt_content),
            "bundle_id": bundle.manifest["bundle_id"],
            "artifact_path": f"artifacts/records-{size}",
        }
        status = EvaluationStatus.OBSERVED
        observed = (
            f"Observed {size} records: "
            f"{metrics['ledger_validation_ms']} ms validation, "
            f"{metrics['bundle_build_ms']} ms bundle build."
        )
    except Exception as error:
        status = EvaluationStatus.FAILED
        observed = f"{type(error).__name__}: {error}"
        metrics = {
            "record_count": size,
            "error_type": type(error).__name__,
            "error": str(error),
        }
    duration_ms = max(0, (monotonic_ns() - check_started) // 1_000_000)
    return EvaluationCheck(
        check_id=f"capacity.records-{size}",
        category="capacity observation",
        status=status,
        duration_ms=duration_ms,
        expected=(
            "Observe construction, validation, storage, receipt, and bundle behavior "
            "without a performance threshold."
        ),
        observed=observed,
        details=metrics,
    )


def _validated_sizes(sizes: Sequence[int]) -> tuple[int, ...]:
    values = tuple(sizes)
    if not values:
        raise ValueError("capacity curve requires at least one record count")
    if any(isinstance(value, bool) or value <= 0 for value in values):
        raise ValueError("capacity record counts must be positive integers")
    if len(values) != len(set(values)):
        raise ValueError("capacity record counts must not contain duplicates")
    return tuple(sorted(values))


def run_capacity_curve(
    output: Path,
    *,
    sizes: Sequence[int] = DEFAULT_SIZES,
    source_revision: str | None = None,
    generated_at: datetime | None = None,
    environment: EvaluationEnvironment | None = None,
    monotonic_ns: MonotonicClock = time.monotonic_ns,
) -> EvaluationReport:
    """Observe capacity, report it, and atomically publish retained bundles."""

    sample_sizes = _validated_sizes(sizes)
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
        with tempfile.TemporaryDirectory(prefix="itself-capacity-curve-") as workspace:
            checks = tuple(
                _sample(
                    size=size,
                    staging=staging,
                    workspace=Path(workspace),
                    monotonic_ns=monotonic_ns,
                )
                for size in sample_sizes
            )
        report = build_evaluation_report(
            suite_id="capacity-curve",
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
        "--size",
        action="append",
        dest="sizes",
        type=int,
        help="Record count to observe; repeat for a curve.",
    )
    parser.add_argument(
        "--source-revision",
        default=os.environ.get("GITHUB_SHA"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the capacity curve and fail only when an operation is incorrect."""

    args = _parser().parse_args(argv)
    output = cast(Path, args.output)
    sizes = cast(list[int] | None, args.sizes)
    source_revision = cast(str | None, args.source_revision)
    try:
        report = run_capacity_curve(
            output,
            sizes=DEFAULT_SIZES if sizes is None else sizes,
            source_revision=source_revision,
        )
    except (OSError, ValueError) as error:
        print(f"FAIL {output}: {error}")
        return 2

    print(
        f"{report.outcome.value.upper()} {output}: "
        f"{len(report.checks)} samples, {report.run_id}"
    )
    return 1 if report.outcome is EvaluationOutcome.FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
