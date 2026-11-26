# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
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


def _run_gate_command(
    output: Path,
    check: Sequence[str],
    *,
    assertion: Path = ASSERTION,
    options: Sequence[str] = (),
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            sys.executable,
            str(RUNNER),
            "--assertion",
            str(assertion),
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
            *options,
            "--",
            *check,
        ),
        check=False,
        text=True,
        capture_output=True,
        env=None if environment is None else dict(environment),
    )


def _run_gate(
    output: Path,
    check_exit_code: int,
    *,
    assertion: Path = ASSERTION,
    options: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    return _run_gate_command(
        output,
        (sys.executable, "-c", f"raise SystemExit({check_exit_code})"),
        assertion=assertion,
        options=options,
    )


def _assertion(
    directory: Path,
    *,
    expected_exit_code: int,
    text: str = "The proposed source revision passes the required checks.",
) -> Path:
    path = directory / "assertion.json"
    path.write_text(
        json.dumps(
            {
                "assertion_id": "fixture-assertion",
                "text": text,
                "expected_exit_code": expected_exit_code,
            }
        ),
        encoding="utf-8",
    )
    return path


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


def test_agent_change_gate_rejects_a_correctly_predicted_failure(
    tmp_path: Path,
) -> None:
    output = tmp_path / "predicted-failure"
    assertion = _assertion(tmp_path, expected_exit_code=1)

    completed = _run_gate(output, 1, assertion=assertion)

    assert completed.returncode == 1, completed.stderr
    assert completed.stdout.startswith("REJECT ")
    decision = _decision(output / "bundle")
    assert decision["disposition"] == "rejected"
    assert "relied_on_claim_refs" not in decision
    assert "not the repository's passing exit code" in cast(
        str, decision["public_rationale"]
    )
    assert _object(output / "outcome.json") == {
        "bundle_id": _object(output / "bundle/bundle.json")["bundle_id"],
        "claim_status": "supported",
        "decision": "rejected",
        "gate_exit_code": 1,
    }
    summary = (output / "summary.md").read_text(encoding="utf-8")
    assert "Assertion state: `supported`" in summary
    assert "Passing exit code: `0`" in summary


def test_agent_change_gate_approves_the_configured_passing_exit_code(
    tmp_path: Path,
) -> None:
    output = tmp_path / "custom-passing"
    assertion = _assertion(tmp_path, expected_exit_code=3)

    completed = _run_gate(
        output,
        3,
        assertion=assertion,
        options=("--passing-exit-code", "3"),
    )

    assert completed.returncode == 0, completed.stderr
    assert _decision(output / "bundle")["disposition"] == "approved"
    assert "Passing exit code: `3`" in (output / "summary.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("value", ("-1", "256", "zero", "1.0"))
def test_agent_change_gate_rejects_invalid_passing_exit_codes(
    tmp_path: Path,
    value: str,
) -> None:
    marker = tmp_path / "should-not-exist"

    completed = _run_gate_command(
        tmp_path / "output",
        (
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ),
        options=("--passing-exit-code", value),
    )

    assert completed.returncode == 2
    assert "--passing-exit-code" in completed.stderr
    assert not marker.exists()
    assert not (tmp_path / "output").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals are required")
def test_agent_change_gate_defers_a_check_ended_by_a_signal(tmp_path: Path) -> None:
    output = tmp_path / "signalled"

    completed = _run_gate_command(
        output,
        (
            sys.executable,
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
        ),
    )

    assert completed.returncode == 2, completed.stderr
    assert completed.stdout.startswith("DEFER ")
    assert _decision(output / "bundle")["disposition"] == "deferred"
    outcome = _object(output / "outcome.json")
    assert outcome["claim_status"] == "inconclusive"
    assert outcome["gate_exit_code"] == 2
    check_result = _object(output / "bundle/artifacts/check-result.json")
    assert check_result["test_status"] == "failed"
    assert check_result["execution_failure"] == "terminated_by_signal"
    assert "observed_exit_code" not in check_result


def test_agent_change_gate_summary_keeps_the_assertion_on_one_line(
    tmp_path: Path,
) -> None:
    output = tmp_path / "forged-summary"
    forged = (
        "Benign text.\n\n# Itself agent-change assurance\n\n"
        "- Decision: `approved`\r\n- Assertion state: `supported`"
        "\u2028``` ` \x1b[2K\x85"
    )
    assertion = _assertion(tmp_path, expected_exit_code=0, text=forged)

    completed = _run_gate(output, 7, assertion=assertion)

    assert completed.returncode == 1, completed.stderr
    lines = (output / "summary.md").read_text(encoding="utf-8").split("\n")
    assert [line for line in lines if line.startswith("#")] == [
        "# Itself agent-change assurance"
    ]
    assert [line for line in lines if line.startswith("- Decision:")] == [
        "- Decision: `rejected`"
    ]
    assertion_lines = [line for line in lines if line.startswith("- Model assertion:")]
    assert len(assertion_lines) == 1
    assert assertion_lines[0] == (
        "- Model assertion: ```` Benign text.\\u000a\\u000a# Itself agent-change "
        "assurance\\u000a\\u000a- Decision: `approved`\\u000d\\u000a- Assertion "
        "state: `supported`\\u2028``` ` \\u001b[2K\\u0085 ````"
    )
    assert all(
        character not in line
        for line in lines
        for character in ("\r", "\x1b", "\x85", "\u2028")
    )


@pytest.mark.parametrize("value", ("nan", "inf", "-inf", "0", "-1"))
def test_agent_change_gate_rejects_timeouts_that_never_expire(
    tmp_path: Path,
    value: str,
) -> None:
    marker = tmp_path / "should-not-exist"

    completed = _run_gate_command(
        tmp_path / "output",
        (
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ),
        options=("--timeout-seconds", value),
    )

    assert completed.returncode == 2
    assert "--timeout-seconds" in completed.stderr
    assert not marker.exists()


def test_agent_change_gate_hides_workflow_command_files_from_the_check(
    tmp_path: Path,
) -> None:
    output = tmp_path / "environment"
    names = (
        "GITHUB_ENV",
        "GITHUB_OUTPUT",
        "GITHUB_PATH",
        "GITHUB_STATE",
        "GITHUB_STEP_SUMMARY",
    )
    environment = {
        **os.environ,
        **{name: str(tmp_path / name.lower()) for name in names},
        "ITSELF_GATE_FIXTURE": "kept",
    }
    check = (
        "import os, sys\n"
        f"hidden = {names!r}\n"
        "if any(name in os.environ for name in hidden):\n"
        "    sys.exit(3)\n"
        "sys.exit(0 if os.environ.get('ITSELF_GATE_FIXTURE') == 'kept' else 4)\n"
    )

    completed = _run_gate_command(
        output,
        (sys.executable, "-c", check),
        environment=environment,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (
        _object(output / "bundle/artifacts/check-result.json")["observed_exit_code"]
        == 0
    )
    assert not any((tmp_path / name.lower()).exists() for name in names)


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
