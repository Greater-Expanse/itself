# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

ROOT: Final = Path(__file__).resolve().parents[1]
COOKBOOK: Final = ROOT / "cookbook"
FULL_SHA: Final = re.compile(r"^[0-9a-f]{40}$")
USES: Final = re.compile(r"^\s*uses:\s+[^@\s]+@([^\s#]+)", re.MULTILINE)

GITHUB_WORKFLOWS: Final = tuple(sorted(COOKBOOK.rglob("github-actions.yml")))
GITLAB_WORKFLOWS: Final = tuple(sorted(COOKBOOK.rglob("gitlab-ci.yml")))

GITHUB_STEP: Final = re.compile(r"^ *- name:", re.MULTILINE)
RUN_KEY: Final = re.compile(r"^(?P<indent> *)run:[ \t]*(?P<rest>.*)$")
SCRIPT_KEY: Final = re.compile(r"^(?P<indent> *)(?:before_)?script:[ \t]*$")
BLOCK_INDICATOR: Final = re.compile(r"^(?:- )?[|>][-+]?$")
PIPE: Final = re.compile(r"(?<!\|)\|(?!\|)")
PIPEFAIL: Final = re.compile(r"\bset -[a-z]*o pipefail\b")
# GitHub runs `shell: bash` as `bash --noprofile --norc -eo pipefail {0}`; the
# default shell for a step without one is `bash -e {0}`, without pipefail.
GITHUB_PIPEFAIL_SHELL: Final = re.compile(r"^ *shell: bash[ \t]*$", re.MULTILINE)
GATE_SCRIPT: Final = "cookbook/agent-change-gate/run.py"
CONTINUE_ON_ERROR: Final = re.compile(r"^ *continue-on-error:", re.MULTILINE)
STATUS_MASK: Final = re.compile(r"\bset \+e\b|\|\|\s*(?:true\b|:)|;\s*true\b")
CONTROL_OPERATOR: Final = re.compile(r"[;&|]")


def _case_id(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _indented_block(lines: list[str], start: int, indent: int) -> list[str]:
    block: list[str] = []
    for line in lines[start:]:
        if line.strip() and len(line) - len(line.lstrip(" ")) <= indent:
            break
        block.append(line)
    return block


def _shell_lines(lines: list[str]) -> list[str]:
    """Return script lines without YAML block indicators or list markers."""

    shell: list[str] = []
    for line in lines:
        text = line.strip()
        if not text or BLOCK_INDICATOR.fullmatch(text):
            continue
        shell.append(text.removeprefix("- "))
    return shell


def _unprotected_pipeline(script: list[str]) -> str | None:
    """Return the first pipeline line not preceded by ``set -o pipefail``."""

    for line in script:
        if PIPEFAIL.search(line):
            return None
        if PIPE.search(line):
            return line
    return None


def _github_steps(content: str) -> list[str]:
    starts = [match.start() for match in GITHUB_STEP.finditer(content)]
    return [
        content[start:end]
        for start, end in zip(starts, (*starts[1:], len(content)), strict=True)
    ]


def _run_script(step: str) -> list[str]:
    """Return the shell lines of a GitHub step's ``run`` value."""

    lines = step.splitlines()
    for position, line in enumerate(lines):
        match = RUN_KEY.match(line)
        if match is None:
            continue
        script = (
            [match["rest"]]
            if match["rest"] and not BLOCK_INDICATOR.fullmatch(match["rest"])
            else _indented_block(lines, position + 1, len(match["indent"]))
        )
        return _shell_lines(script)
    return []


def _github_unprotected_pipelines(content: str) -> list[str]:
    failures: list[str] = []
    for step in _github_steps(content):
        if GITHUB_PIPEFAIL_SHELL.search(step):
            continue
        pipeline = _unprotected_pipeline(_run_script(step))
        if pipeline is not None:
            failures.append(pipeline)
    return failures


def _gate_status_problems(step: str) -> list[str]:
    """Return why a step that runs the gate might not fail with its status."""

    problems: list[str] = []
    if not GITHUB_PIPEFAIL_SHELL.search(step):
        problems.append("the step does not set shell: bash")
    if CONTINUE_ON_ERROR.search(step):
        problems.append("the step sets continue-on-error")
    script = _run_script(step)
    problems.extend(
        f"the script masks a status: {line}"
        for line in script
        if STATUS_MASK.search(line)
    )
    start = next(
        (position for position, line in enumerate(script) if GATE_SCRIPT in line),
        None,
    )
    if start is None:
        return [*problems, "the script does not run the gate"]
    end = start
    # The invocation continues while its lines end in a backslash.
    while end < len(script) - 1 and script[end].endswith("\\"):
        end += 1
    problems.extend(
        f"a command runs after the gate: {line}"
        for line in script[start : end + 1]
        if CONTROL_OPERATOR.search(line)
    )
    problems.extend(
        f"a command runs after the gate: {line}" for line in script[end + 1 :]
    )
    return problems


def _gitlab_unprotected_pipelines(content: str) -> list[str]:
    lines = content.splitlines()
    script: list[str] = []
    for position, line in enumerate(lines):
        match = SCRIPT_KEY.match(line)
        if match is not None:
            script.extend(_indented_block(lines, position + 1, len(match["indent"])))
    pipeline = _unprotected_pipeline(_shell_lines(script))
    return [] if pipeline is None else [pipeline]


def test_pipefail_check_flags_a_pipeline_that_hides_a_failure() -> None:
    github_step = (
        "      - name: Verify\n"
        "        run: |\n"
        '          itself bundle validate "$B" 2>&1 \\\n'
        "            | tee verification.txt\n"
    )
    gitlab_job = (
        "job:\n"
        "  script:\n"
        "    - >-\n"
        '      itself bundle validate "$B"\n'
        "      | tee verification.txt\n"
    )

    assert _github_unprotected_pipelines(github_step) == ["| tee verification.txt"]
    assert (
        _github_unprotected_pipelines(
            github_step.replace("        run:", "        shell: bash\n        run:")
        )
        == []
    )
    assert _gitlab_unprotected_pipelines(gitlab_job) == ["| tee verification.txt"]
    assert (
        _gitlab_unprotected_pipelines(
            gitlab_job.replace("  script:\n", "  script:\n    - set -o pipefail\n")
        )
        == []
    )


def test_gate_status_check_flags_a_step_that_hides_the_gate_status() -> None:
    gate_step = (
        "      - name: Check\n"
        "        shell: bash\n"
        "        run: |\n"
        f"          python {GATE_SCRIPT} \\\n"
        "            -- python -m pytest\n"
    )

    assert _gate_status_problems(gate_step) == []
    assert _gate_status_problems(gate_step.replace("        shell: bash\n", "")) == [
        "the step does not set shell: bash"
    ]
    assert _gate_status_problems(
        gate_step.replace(
            "        run:", "        continue-on-error: true\n        run:"
        )
    ) == ["the step sets continue-on-error"]
    for masked in ("pytest || true", "pytest || :", "pytest; true"):
        assert _gate_status_problems(gate_step.replace("pytest\n", f"{masked}\n"))
    assert _gate_status_problems(
        gate_step.replace("run: |\n", "run: |\n          set +e\n")
    ) == ["the script masks a status: set +e"]
    assert _gate_status_problems(gate_step + "          echo done\n") == [
        "a command runs after the gate: echo done"
    ]


def test_github_gate_step_fails_with_the_gate_status() -> None:
    workflow = COOKBOOK / "agent-change-gate" / "github-actions.yml"
    steps = [
        step
        for step in _github_steps(workflow.read_text(encoding="utf-8"))
        if GATE_SCRIPT in step
    ]

    assert len(steps) == 1
    assert _gate_status_problems(steps[0]) == []


@pytest.mark.parametrize("workflow", GITHUB_WORKFLOWS, ids=_case_id)
def test_github_recipe_pipelines_fail_when_a_command_fails(workflow: Path) -> None:
    content = workflow.read_text(encoding="utf-8")

    assert _github_unprotected_pipelines(content) == []


@pytest.mark.parametrize("workflow", GITLAB_WORKFLOWS, ids=_case_id)
def test_gitlab_recipe_pipelines_fail_when_a_command_fails(workflow: Path) -> None:
    content = workflow.read_text(encoding="utf-8")

    assert _gitlab_unprotected_pipelines(content) == []


@pytest.mark.parametrize("workflow", GITHUB_WORKFLOWS, ids=_case_id)
def test_github_recipe_uses_read_only_permissions_and_immutable_actions(
    workflow: Path,
) -> None:
    content = workflow.read_text(encoding="utf-8")

    assert "pull_request_target" not in content
    assert "permissions:\n  contents: read" in content
    action_revisions = USES.findall(content)
    assert action_revisions
    assert all(FULL_SHA.fullmatch(revision) for revision in action_revisions)
    assert 'ITSELF_INSTALL_SPEC: "."' in content
    assert "@main" not in content


@pytest.mark.parametrize("workflow", GITLAB_WORKFLOWS, ids=_case_id)
def test_gitlab_recipe_retains_outputs_and_declares_pipeline_rules(
    workflow: Path,
) -> None:
    content = workflow.read_text(encoding="utf-8")

    assert 'CI_PIPELINE_SOURCE == "merge_request_event"' in content
    assert "artifacts:" in content
    assert "when: always" in content
    assert "expire_in: 14 days" in content
    assert "expose_as:" in content
    assert 'ITSELF_INSTALL_SPEC: "."' in content
    assert "@main" not in content


def test_cookbook_has_one_wrapper_per_recipe_and_platform() -> None:
    recipe_directories = {
        path.parent for path in COOKBOOK.glob("*/README.md") if path.parent != COOKBOOK
    }

    assert recipe_directories == {
        COOKBOOK / "agent-change-gate",
        COOKBOOK / "publish-reasoning-receipt",
        COOKBOOK / "verify-evidence-bundle",
    }
    for recipe in recipe_directories:
        assert (recipe / "github-actions.yml").is_file()
        assert (recipe / "gitlab-ci.yml").is_file()
