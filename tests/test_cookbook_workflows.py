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


def _case_id(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


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
