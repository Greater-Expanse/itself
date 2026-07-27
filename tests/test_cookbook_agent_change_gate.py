# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Final, cast

import pytest

from itself import (
    DecisionDisposition,
    EvidenceBundleValidator,
    JsonObject,
    ReasoningReceiptValidator,
)

ROOT: Final = Path(__file__).resolve().parents[1]
RUNNER: Final = ROOT / "cookbook/agent-change-gate/run.py"
ASSERTION: Final = ROOT / "cookbook/fixtures/model-assertion.json"


def _run_gate(output: Path, check_exit_code: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            sys.executable,
            str(RUNNER),
            "--assertion",
            str(ASSERTION),
            "--output",
            str(output),
            "--source-revision",
            "test-revision",
            "--check-name",
            "fixture check",
            "--model-actor-id",
            "fixture-agent",
            "--model-implementation",
            "fixture-agent@1",
            "--",
            sys.executable,
            "-c",
            f"raise SystemExit({check_exit_code})",
        ),
        check=False,
        text=True,
        capture_output=True,
    )


def _object(path: Path) -> JsonObject:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(JsonObject, value)


def _decision(bundle_path: Path) -> JsonObject:
    verified = EvidenceBundleValidator().validate(bundle_path)
    decisions = [
        record for record in verified.ledger.records if record["kind"] == "decision"
    ]
    assert len(decisions) == 1
    ReasoningReceiptValidator().validate_against_ledger(
        verified.receipt,
        verified.ledger,
    )
    return decisions[0]


@pytest.mark.parametrize(
    ("check_exit_code", "gate_exit_code", "disposition", "claim_status"),
    (
        (0, 0, DecisionDisposition.APPROVED, "supported"),
        (7, 1, DecisionDisposition.REJECTED, "refuted"),
    ),
)
def test_agent_change_gate_records_both_check_outcomes(
    tmp_path: Path,
    check_exit_code: int,
    gate_exit_code: int,
    disposition: DecisionDisposition,
    claim_status: str,
) -> None:
    output = tmp_path / disposition.value

    completed = _run_gate(output, check_exit_code)

    assert completed.returncode == gate_exit_code, completed.stderr
    assert _decision(output / "bundle")["disposition"] == disposition.value
    assert _object(output / "outcome.json") == {
        "bundle_id": _object(output / "bundle/bundle.json")["bundle_id"],
        "claim_status": claim_status,
        "decision": disposition.value,
        "gate_exit_code": gate_exit_code,
    }
    summary = (output / "summary.md").read_text(encoding="utf-8")
    assert f"Decision: `{disposition.value}`" in summary
    assert f"Observed exit code: `{check_exit_code}`" in summary


def test_agent_change_gate_records_unavailable_command_as_deferred(
    tmp_path: Path,
) -> None:
    output = tmp_path / "deferred"

    completed = subprocess.run(
        (
            sys.executable,
            str(RUNNER),
            "--assertion",
            str(ASSERTION),
            "--output",
            str(output),
            "--source-revision",
            "test-revision",
            "--check-name",
            "missing fixture check",
            "--",
            str(tmp_path / "does-not-exist"),
        ),
        check=False,
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 2
    assert _decision(output / "bundle")["disposition"] == "deferred"
    outcome = _object(output / "outcome.json")
    assert outcome["claim_status"] == "inconclusive"
    assert outcome["gate_exit_code"] == 2
    check_result = _object(output / "bundle/artifacts/check-result.json")
    assert check_result["execution_failure"] == "command_not_found"


def test_agent_change_gate_rejects_unknown_assertion_fields_before_running(
    tmp_path: Path,
) -> None:
    assertion = tmp_path / "assertion.json"
    assertion.write_text(
        json.dumps(
            {
                "assertion_id": "unsafe",
                "text": "This must not run.",
                "expected_exit_code": 0,
                "command": "touch should-not-exist",
            }
        ),
        encoding="utf-8",
    )
    marker = tmp_path / "should-not-exist"

    completed = subprocess.run(
        (
            sys.executable,
            str(RUNNER),
            "--assertion",
            str(assertion),
            "--output",
            str(tmp_path / "output"),
            "--source-revision",
            "test-revision",
            "--check-name",
            "must not run",
            "--",
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ),
        check=False,
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 2
    assert "unknown=['command']" in completed.stderr
    assert not marker.exists()
