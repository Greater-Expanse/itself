# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Build the deterministic cache-key diagnosis evidence bundle."""

from __future__ import annotations

# ruff: noqa: E402
import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from experiments.cases.cache_key_diagnostician import (
    CacheKeyFixtureDiagnostician,
)
from experiments.cases.cache_key_exchange import build_exchange_request
from experiments.cases.cache_key_scripted_run import run_scripted_case
from itself import (
    EvidenceBundleBuilder,
    EvidenceBundleFile,
    EvidenceBundleFileRole,
    JsonObject,
    JsonValue,
    Ledger,
    canonical_bundle_json,
)

CREATED_AT: Final = datetime(2026, 7, 23, 12, tzinfo=UTC)
ARTIFACT_RECORD_ID: Final = "artifact-diagnostician-output"
ARTIFACT_PATH: Final = "artifacts/model-assertion.json"


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


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _materialized_ledger() -> tuple[Ledger, JsonObject, bytes]:
    request = build_exchange_request()
    diagnosis = CacheKeyFixtureDiagnostician().diagnose(request)
    result = run_scripted_case(CacheKeyFixtureDiagnostician())
    records = list(result.ledger.records)
    artifact_record = next(
        record for record in records if record["id"] == ARTIFACT_RECORD_ID
    )
    artifact_record["uri"] = ARTIFACT_PATH
    ledger = Ledger(records)

    assertion = diagnosis.assertion.to_json_object()
    artifact_content = canonical_bundle_json(assertion)
    digest = cast(JsonObject, artifact_record["digest"])
    if digest["value"] != _sha256(artifact_content):
        raise RuntimeError("fixture assertion digest drifted")
    return ledger, request.to_json_object(), artifact_content


def build_bundle(destination: Path) -> JsonObject:
    """Build and verify the example without overwriting an existing path."""

    ledger, request, artifact_content = _materialized_ledger()
    verified = EvidenceBundleBuilder().build(
        destination,
        ledger=ledger,
        title="Deterministic cache-key causal diagnosis",
        created_at=CREATED_AT,
        files=(
            EvidenceBundleFile(
                path="inputs/diagnosis-request.json",
                content=_pretty_json(request),
                role=EvidenceBundleFileRole.INPUT,
                media_type="application/json",
            ),
            EvidenceBundleFile(
                path=ARTIFACT_PATH,
                content=artifact_content,
                role=EvidenceBundleFileRole.ARTIFACT,
                media_type="application/json",
                record_ref=ARTIFACT_RECORD_ID,
            ),
        ),
        limitations=(
            "The diagnostician is a deterministic software fixture, not a hosted model.",
            "Structural integrity does not establish that a real-world claim is true.",
            "The causal oracle is authoritative only within this controlled case.",
        ),
    )
    return verified.manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Build one deterministic bundle and print its content-derived identity."""

    args = _parser().parse_args(argv)
    destination = cast(Path, args.output)
    manifest = build_bundle(destination)
    print(f"PASS {destination}: {manifest['bundle_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
