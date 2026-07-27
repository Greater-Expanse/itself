# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Run a repository-owned check against a model assertion and record the result."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, NoReturn, cast

from itself import (
    DEFAULT_EVIDENCE_BUNDLE_LIMITATIONS,
    Actor,
    ActorRole,
    ActorType,
    Authority,
    AuthorityType,
    ClaimStatus,
    ClaimType,
    DecisionDisposition,
    Digest,
    DigestAlgorithm,
    EvidenceBundleBuilder,
    EvidenceBundleFile,
    EvidenceBundleFileRole,
    EvidenceRelation,
    EvidenceRelationType,
    EvidenceType,
    JsonObject,
    JsonValue,
    Ledger,
    Oracle,
    RecordHeader,
    Scope,
    TestCost,
    TestDesign,
    TestStatus,
    VerdictOutcome,
    artifact_reference_record,
    canonical_bundle_json,
    claim_record,
    decision_record,
    evidence_record,
    protocol_test_record,
    status_transition_record,
    verdict_record,
)

ASSERTION_ARTIFACT_ID: Final = "artifact-model-assertion"
ASSERTION_ARTIFACT_PATH: Final = "artifacts/model-assertion.json"
CHECK_ARTIFACT_ID: Final = "artifact-ci-check-result"
CHECK_ARTIFACT_PATH: Final = "artifacts/check-result.json"
CLAIM_ID: Final = "claim-agent-change-check"
EVIDENCE_ID: Final = "evidence-ci-check"
VERDICT_ID: Final = "verdict-agent-change-check"
MAX_ASSERTION_BYTES: Final = 64 * 1024
MAX_COMMAND_ARGUMENTS: Final = 128
MAX_COMMAND_BYTES: Final = 32 * 1024


class AssertionFormatError(ValueError):
    """Raised when the model-assertion document violates the recipe contract."""


@dataclass(frozen=True, slots=True)
class ModelAssertion:
    """The deliberately small model-owned input to the CI assurance boundary."""

    assertion_id: str
    text: str
    expected_exit_code: int


@dataclass(frozen=True, slots=True)
class CheckObservation:
    """A sanitized observation from the repository-owned process check."""

    command: tuple[str, ...]
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    test_status: TestStatus
    observed_exit_code: int | None
    execution_failure: str | None


@dataclass(frozen=True, slots=True)
class AssuranceOutcome:
    """The protocol outcome and process status exposed to the CI wrapper."""

    relation: EvidenceRelationType
    verdict: VerdictOutcome
    claim_status: ClaimStatus
    disposition: DecisionDisposition
    gate_exit_code: int
    rationale: str


def _reject_nonfinite(value: str) -> NoReturn:
    raise AssertionFormatError(f"non-finite JSON number is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
    value: JsonObject = {}
    for key, item in pairs:
        if key in value:
            raise AssertionFormatError(f"duplicate JSON object key: {key!r}")
        value[key] = item
    return value


def _load_assertion(path: Path) -> tuple[ModelAssertion, bytes]:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise AssertionFormatError(f"cannot read {path}: {error}") from error
    if len(content) > MAX_ASSERTION_BYTES:
        raise AssertionFormatError(
            f"{path} exceeds the {MAX_ASSERTION_BYTES}-byte recipe limit"
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AssertionFormatError(f"{path} is not valid UTF-8") from error
    try:
        value = cast(
            JsonValue,
            json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_nonfinite,
            ),
        )
    except json.JSONDecodeError as error:
        raise AssertionFormatError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise AssertionFormatError(f"{path} must contain one JSON object")

    required = {"assertion_id", "text", "expected_exit_code"}
    observed = set(value)
    if observed != required:
        missing = sorted(required - observed)
        unknown = sorted(observed - required)
        raise AssertionFormatError(
            f"{path} fields must exactly match the recipe contract; "
            f"missing={missing}, unknown={unknown}"
        )

    assertion_id = value["assertion_id"]
    assertion_text = value["text"]
    expected_exit_code = value["expected_exit_code"]
    if (
        not isinstance(assertion_id, str)
        or not assertion_id.strip()
        or len(assertion_id) > 128
    ):
        raise AssertionFormatError(
            "assertion_id must be non-empty text of at most 128 characters"
        )
    if (
        not isinstance(assertion_text, str)
        or not assertion_text.strip()
        or len(assertion_text) > 10_000
    ):
        raise AssertionFormatError(
            "text must be non-empty text of at most 10,000 characters"
        )
    if (
        isinstance(expected_exit_code, bool)
        or not isinstance(expected_exit_code, int)
        or not 0 <= expected_exit_code <= 255
    ):
        raise AssertionFormatError(
            "expected_exit_code must be an integer from 0 through 255"
        )
    return (
        ModelAssertion(
            assertion_id=assertion_id,
            text=assertion_text,
            expected_exit_code=expected_exit_code,
        ),
        content,
    )


def _command(arguments: Sequence[str]) -> tuple[str, ...]:
    command = tuple(arguments)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("a repository-owned check command is required after --")
    if len(command) > MAX_COMMAND_ARGUMENTS:
        raise ValueError(
            f"check command exceeds the {MAX_COMMAND_ARGUMENTS}-argument limit"
        )
    if any(not argument or "\0" in argument for argument in command):
        raise ValueError("check command arguments must be non-empty and contain no NUL")
    if sum(len(argument.encode("utf-8")) for argument in command) > MAX_COMMAND_BYTES:
        raise ValueError(f"check command exceeds the {MAX_COMMAND_BYTES}-byte limit")
    return command


def _execute(command: tuple[str, ...], timeout_seconds: float) -> CheckObservation:
    started_at = datetime.now(UTC)
    started_ns = time.monotonic_ns()
    test_status = TestStatus.COMPLETED
    observed_exit_code: int | None = None
    execution_failure: str | None = None
    try:
        completed = subprocess.run(
            command,
            check=False,
            shell=False,
            timeout=timeout_seconds,
        )
        observed_exit_code = completed.returncode
    except subprocess.TimeoutExpired:
        test_status = TestStatus.FAILED
        execution_failure = "timeout"
    except FileNotFoundError:
        test_status = TestStatus.UNAVAILABLE
        execution_failure = "command_not_found"
    except PermissionError:
        test_status = TestStatus.UNAVAILABLE
        execution_failure = "command_not_executable"
    except OSError:
        test_status = TestStatus.UNAVAILABLE
        execution_failure = "command_start_failed"
    completed_at = datetime.now(UTC)
    duration_ms = max(0, (time.monotonic_ns() - started_ns) // 1_000_000)
    return CheckObservation(
        command=command,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        test_status=test_status,
        observed_exit_code=observed_exit_code,
        execution_failure=execution_failure,
    )


def _outcome(
    assertion: ModelAssertion,
    observation: CheckObservation,
) -> AssuranceOutcome:
    if observation.observed_exit_code is None:
        return AssuranceOutcome(
            relation=EvidenceRelationType.CONTEXTUALIZES,
            verdict=VerdictOutcome.INCONCLUSIVE,
            claim_status=ClaimStatus.INCONCLUSIVE,
            disposition=DecisionDisposition.DEFERRED,
            gate_exit_code=2,
            rationale=(
                "The configured external check did not produce an exit code, "
                "so the assertion remains inconclusive and action is deferred."
            ),
        )
    if observation.observed_exit_code == assertion.expected_exit_code:
        return AssuranceOutcome(
            relation=EvidenceRelationType.SUPPORTS,
            verdict=VerdictOutcome.SUPPORTED,
            claim_status=ClaimStatus.SUPPORTED,
            disposition=DecisionDisposition.APPROVED,
            gate_exit_code=0,
            rationale=(
                "The repository-owned check returned the exit code declared "
                "by the model assertion."
            ),
        )
    return AssuranceOutcome(
        relation=EvidenceRelationType.CONTRADICTS,
        verdict=VerdictOutcome.REFUTED,
        claim_status=ClaimStatus.REFUTED,
        disposition=DecisionDisposition.REJECTED,
        gate_exit_code=1,
        rationale=(
            "The repository-owned check returned a different exit code from "
            "the one declared by the model assertion."
        ),
    )


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _header(
    record_id: str,
    base: datetime,
    offset: int,
    actor: Actor,
) -> RecordHeader:
    return RecordHeader(
        record_id=record_id,
        created_at=base + timedelta(microseconds=offset),
        created_by=actor,
    )


def _result_object(
    *,
    check_name: str,
    source_revision: str,
    observation: CheckObservation,
) -> JsonObject:
    value: JsonObject = {
        "check_name": check_name,
        "source_revision": source_revision,
        "command_argv": [argument for argument in observation.command],
        "started_at": _timestamp(observation.started_at),
        "completed_at": _timestamp(observation.completed_at),
        "duration_ms": observation.duration_ms,
        "test_status": observation.test_status.value,
    }
    if observation.observed_exit_code is not None:
        value["observed_exit_code"] = observation.observed_exit_code
    if observation.execution_failure is not None:
        value["execution_failure"] = observation.execution_failure
    return value


def _build_ledger(
    *,
    assertion: ModelAssertion,
    assertion_content: bytes,
    check_name: str,
    source_revision: str,
    model_actor_id: str,
    model_implementation: str | None,
    observation: CheckObservation,
    outcome: AssuranceOutcome,
    result_content: bytes,
) -> Ledger:
    model = Actor(
        actor_id=model_actor_id,
        actor_type=ActorType.MODEL,
        role=ActorRole.PROPOSER,
        implementation_ref=model_implementation,
    )
    operator = Actor(
        actor_id="ci-check-runner",
        actor_type=ActorType.SOFTWARE,
        role=ActorRole.OPERATOR,
        implementation_ref="itself-cookbook-agent-change-gate@1",
    )
    evaluator = Actor(
        actor_id="ci-assurance-policy",
        actor_type=ActorType.SOFTWARE,
        role=ActorRole.EVALUATOR,
        implementation_ref="itself-cookbook-exit-code-policy@1",
    )
    scope = Scope(
        description="One repository-owned CI check for one source revision",
        dimensions={
            "assertion_id": assertion.assertion_id,
            "check_name": check_name,
            "source_revision": source_revision,
        },
    )
    authority = Authority(
        authority_type=AuthorityType.POLICY_ENGINE,
        actor_ref=evaluator.actor_id,
        basis=(
            "Exact comparison of the observed process exit code with the "
            "model assertion's expected exit code"
        ),
        independent_of_subject=True,
    )
    oracle = Oracle(
        adapter="process-exit-code-equality",
        version="1",
        authority_type=AuthorityType.DETERMINISTIC_TOOL,
        declared_scope="The configured command process on this CI runner",
    )
    base = observation.started_at
    executed_test_evidence = (
        (EVIDENCE_ID,) if observation.test_status is TestStatus.COMPLETED else ()
    )
    evidence_test_ref = (
        "test-agent-change-run"
        if observation.test_status is TestStatus.COMPLETED
        else None
    )
    evidence_result: JsonObject = {
        "expected_exit_code": assertion.expected_exit_code,
        "matched": outcome.claim_status is ClaimStatus.SUPPORTED,
        "test_status": observation.test_status.value,
    }
    if observation.observed_exit_code is not None:
        evidence_result["observed_exit_code"] = observation.observed_exit_code
    if observation.execution_failure is not None:
        evidence_result["execution_failure"] = observation.execution_failure

    records = (
        artifact_reference_record(
            _header(ASSERTION_ARTIFACT_ID, base, 0, model),
            uri=ASSERTION_ARTIFACT_PATH,
            media_type="application/json",
            title="Structured model assertion",
            digest=Digest(DigestAlgorithm.SHA256, _sha256(assertion_content)),
            captured_at=observation.started_at,
        ),
        claim_record(
            _header(CLAIM_ID, base, 1, model),
            proposition=assertion.text,
            claim_type=ClaimType.PREDICTIVE,
            scope=scope,
            dependency_refs=(ASSERTION_ARTIFACT_ID,),
            invalidation_conditions=(
                "The source revision or configured check changes.",
            ),
        ),
        status_transition_record(
            _header("transition-agent-change-testable", base, 2, operator),
            subject_ref=CLAIM_ID,
            from_status=ClaimStatus.PROPOSED,
            to_status=ClaimStatus.TESTABLE,
            authorized_by=operator,
            reason="Repository policy maps the assertion to a deterministic check.",
        ),
        protocol_test_record(
            _header("test-agent-change-plan", base, 3, operator),
            question=(
                f"Does {check_name!r} return exit code {assertion.expected_exit_code}?"
            ),
            design=TestDesign.DETERMINISTIC_CHECK,
            status=TestStatus.PLANNED,
            oracle=oracle,
            scope=scope,
            subject_refs=(CLAIM_ID,),
        ),
        status_transition_record(
            _header("transition-agent-change-under-test", base, 4, operator),
            subject_ref=CLAIM_ID,
            from_status=ClaimStatus.TESTABLE,
            to_status=ClaimStatus.UNDER_TEST,
            authorized_by=operator,
            reason="The repository-owned check was started by the CI runner.",
        ),
        artifact_reference_record(
            _header(CHECK_ARTIFACT_ID, base, 5, evaluator),
            uri=CHECK_ARTIFACT_PATH,
            media_type="application/json",
            title="CI check observation",
            digest=Digest(DigestAlgorithm.SHA256, _sha256(result_content)),
            captured_at=observation.completed_at,
        ),
        protocol_test_record(
            _header("test-agent-change-run", base, 6, operator),
            question=(
                f"Does {check_name!r} return exit code {assertion.expected_exit_code}?"
            ),
            design=TestDesign.DETERMINISTIC_CHECK,
            status=observation.test_status,
            oracle=oracle,
            scope=scope,
            subject_refs=(CLAIM_ID,),
            plan_ref="test-agent-change-plan",
            evidence_refs=executed_test_evidence,
            cost=TestCost(wall_time_ms=observation.duration_ms),
        ),
        evidence_record(
            _header(EVIDENCE_ID, base, 7, evaluator),
            evidence_type=EvidenceType.DETERMINISTIC_TEST,
            relations=(
                EvidenceRelation(
                    subject_ref=CLAIM_ID,
                    relation=outcome.relation,
                    public_note=outcome.rationale,
                ),
            ),
            authority=authority,
            scope=scope,
            artifact_refs=(CHECK_ARTIFACT_ID,),
            test_ref=evidence_test_ref,
            result=evidence_result,
        ),
        verdict_record(
            _header(VERDICT_ID, base, 8, evaluator),
            subject_ref=CLAIM_ID,
            outcome=outcome.verdict,
            evidence_refs=(EVIDENCE_ID,),
            authority=authority,
            scope=scope,
            public_rationale=outcome.rationale,
        ),
        status_transition_record(
            _header("transition-agent-change-final", base, 9, evaluator),
            subject_ref=CLAIM_ID,
            from_status=ClaimStatus.UNDER_TEST,
            to_status=outcome.claim_status,
            authorized_by=evaluator,
            reason="The declared external check and policy determined the state.",
            evidence_refs=(EVIDENCE_ID,),
            verdict_ref=VERDICT_ID,
        ),
        decision_record(
            _header("decision-agent-change", base, 10, evaluator),
            question="May software proceed on this model assertion?",
            disposition=outcome.disposition,
            authorized_by=evaluator,
            scope=scope,
            relied_on_claim_refs=(
                (CLAIM_ID,)
                if outcome.disposition is DecisionDisposition.APPROVED
                else ()
            ),
            reconsider_when=(
                "The source revision, check configuration, or recorded evidence changes.",
            ),
            public_rationale=outcome.rationale,
        ),
    )
    return Ledger(records)


def _summary(
    *,
    assertion: ModelAssertion,
    check_name: str,
    source_revision: str,
    observation: CheckObservation,
    outcome: AssuranceOutcome,
    bundle_id: str,
) -> str:
    observed = (
        str(observation.observed_exit_code)
        if observation.observed_exit_code is not None
        else f"none ({observation.execution_failure})"
    )
    return "\n".join(
        (
            "# Itself agent-change assurance",
            "",
            f"- Decision: `{outcome.disposition.value}`",
            f"- Assertion state: `{outcome.claim_status.value}`",
            f"- Model assertion: {assertion.text}",
            f"- External check: {check_name}",
            f"- Command: `{shlex.join(observation.command)}`",
            f"- Expected exit code: `{assertion.expected_exit_code}`",
            f"- Observed exit code: `{observed}`",
            f"- Source revision: `{source_revision}`",
            f"- Evidence bundle: `{bundle_id}`",
            "",
            outcome.rationale,
            "",
        )
    )


def _write_outputs(
    *,
    output: Path,
    assertion: ModelAssertion,
    assertion_content: bytes,
    check_name: str,
    source_revision: str,
    model_actor_id: str,
    model_implementation: str | None,
    observation: CheckObservation,
    outcome: AssuranceOutcome,
) -> str:
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"output path already exists: {output}")

    result = _result_object(
        check_name=check_name,
        source_revision=source_revision,
        observation=observation,
    )
    result_content = canonical_bundle_json(result)
    ledger = _build_ledger(
        assertion=assertion,
        assertion_content=assertion_content,
        check_name=check_name,
        source_revision=source_revision,
        model_actor_id=model_actor_id,
        model_implementation=model_implementation,
        observation=observation,
        outcome=outcome,
        result_content=result_content,
    )
    verified = EvidenceBundleBuilder().build(
        output / "bundle",
        ledger=ledger,
        title=f"Agent-change assurance: {check_name}",
        created_at=observation.completed_at,
        files=(
            EvidenceBundleFile(
                path=ASSERTION_ARTIFACT_PATH,
                content=assertion_content,
                media_type="application/json",
                role=EvidenceBundleFileRole.ARTIFACT,
                record_ref=ASSERTION_ARTIFACT_ID,
            ),
            EvidenceBundleFile(
                path=CHECK_ARTIFACT_PATH,
                content=result_content,
                media_type="application/json",
                role=EvidenceBundleFileRole.ARTIFACT,
                record_ref=CHECK_ARTIFACT_ID,
            ),
        ),
        limitations=(
            *DEFAULT_EVIDENCE_BUNDLE_LIMITATIONS,
            (
                "The recorded exit code applies only to the configured command, "
                "runner, and source revision."
            ),
            (
                "Repository policy, not Itself, determines whether this check is "
                "sufficient to authorize an action."
            ),
        ),
    )
    bundle_id = cast(str, verified.manifest["bundle_id"])
    summary = _summary(
        assertion=assertion,
        check_name=check_name,
        source_revision=source_revision,
        observation=observation,
        outcome=outcome,
        bundle_id=bundle_id,
    )
    (output / "summary.md").write_text(summary, encoding="utf-8")
    outcome_document: JsonObject = {
        "bundle_id": bundle_id,
        "claim_status": outcome.claim_status.value,
        "decision": outcome.disposition.value,
        "gate_exit_code": outcome.gate_exit_code,
    }
    (output / "outcome.json").write_bytes(canonical_bundle_json(outcome_document))
    return bundle_id


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assertion", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--check-name", required=True)
    parser.add_argument("--model-actor-id", default="ai-agent")
    parser.add_argument("--model-implementation")
    parser.add_argument(
        "--timeout-seconds",
        type=_positive_float,
        default=900.0,
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="repository-owned command, preceded by --",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the external check, materialize assurance outputs, and return gate status."""

    parser = _parser()
    args = parser.parse_args(argv)
    try:
        assertion, assertion_content = _load_assertion(cast(Path, args.assertion))
        command = _command(cast(list[str], args.command))
        output = cast(Path, args.output)
        if output.exists() or output.is_symlink():
            raise FileExistsError(f"output path already exists: {output}")
        observation = _execute(command, cast(float, args.timeout_seconds))
        outcome = _outcome(assertion, observation)
        bundle_id = _write_outputs(
            output=output,
            assertion=assertion,
            assertion_content=assertion_content,
            check_name=cast(str, args.check_name),
            source_revision=cast(str, args.source_revision),
            model_actor_id=cast(str, args.model_actor_id),
            model_implementation=cast(str | None, args.model_implementation),
            observation=observation,
            outcome=outcome,
        )
    except (AssertionFormatError, FileExistsError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    label = {
        DecisionDisposition.APPROVED: "PASS",
        DecisionDisposition.REJECTED: "REJECT",
        DecisionDisposition.DEFERRED: "DEFER",
    }[outcome.disposition]
    print(f"{label} {output}: decision={outcome.disposition.value}, bundle={bundle_id}")
    return outcome.gate_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
