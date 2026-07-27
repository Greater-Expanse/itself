# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

from pathlib import Path
from typing import cast

from examples.inference_to_evidence.run import run_example
from itself import ActorType, ClaimStatus, JsonObject


def _bundle_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_example_builds_a_closed_externally_verified_bundle(tmp_path: Path) -> None:
    bundle = run_example(
        tmp_path / "bundle",
        private_artifact_root=tmp_path / "private",
    )

    assert len(bundle.ledger) == 18
    assert bundle.ledger.snapshot.current_states == {
        "hypothesis-cache-path": ClaimStatus.SUPPORTED,
        "claim-cache-path-cause": ClaimStatus.SUPPORTED,
    }
    assert set(_bundle_files(bundle.path)) == {
        "artifacts/model-assertion.json",
        "artifacts/provider-response.json",
        "artifacts/verifier-observation.json",
        "bundle.json",
        "inputs/incident.json",
        "ledger.jsonl",
        "reasoning-receipt.json",
    }

    records = {cast(str, record["id"]): record for record in bundle.ledger.records}
    final_transition = records["transition-hypothesis-final"]
    authorizer = cast(JsonObject, final_transition["authorized_by"])
    assert authorizer["actor_type"] == ActorType.SOFTWARE
    assert final_transition["evidence_refs"] == ["evidence-cache-bypass"]
    assert final_transition["verdict_ref"] == "verdict-cache-path"

    evidence = records["evidence-cache-bypass"]
    evidence_actor = cast(JsonObject, evidence["created_by"])
    assert evidence_actor["actor_type"] == ActorType.SOFTWARE
    assert evidence["artifact_refs"] == ["artifact-verifier-observation"]

    private_responses = tuple((tmp_path / "private" / "responses").iterdir())
    assert len(private_responses) == 1
    assert private_responses[0].name.startswith("sha256-")


def test_example_bundle_is_deterministically_reproducible(tmp_path: Path) -> None:
    first = run_example(
        tmp_path / "first",
        private_artifact_root=tmp_path / "private-first",
    )
    second = run_example(
        tmp_path / "second",
        private_artifact_root=tmp_path / "private-second",
    )

    assert first.manifest["bundle_id"] == second.manifest["bundle_id"]
    assert _bundle_files(first.path) == _bundle_files(second.path)
