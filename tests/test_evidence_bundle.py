# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import itself.evidence_bundle as evidence_bundle_module
from itself import (
    EvidenceBundleBuilder,
    EvidenceBundleBuildError,
    EvidenceBundleFile,
    EvidenceBundleFileRole,
    EvidenceBundleLimits,
    EvidenceBundleValidationError,
    EvidenceBundleValidator,
    JsonlLedgerStore,
    JsonObject,
    JsonValue,
    Ledger,
    LedgerFormatError,
    evidence_bundle_id,
    parse_bundle_path,
)
from itself._filesystem import publish_path_no_replace

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_BUNDLE = ROOT / "examples" / "cache-key-diagnosis" / "bundle"
EXPECTED_BUNDLE_ID = (
    "urn:sha256:b76cced905c1b9efd3a38b48746ef8ae2f12e10a54deeadb98e81cfea08511e4"
)


def _load_object(path: Path) -> JsonObject:
    value = cast(JsonValue, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain one object")
    return value


def _pretty_write(path: Path, value: JsonValue) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _rebind_file_and_bundle(root: Path, relative_path: str) -> None:
    manifest_path = root / "bundle.json"
    manifest = _load_object(manifest_path)
    entries = cast(list[JsonObject], manifest["files"])
    content = (root / relative_path).read_bytes()
    entry = next(item for item in entries if item["path"] == relative_path)
    entry["size_bytes"] = len(content)
    entry["digest"] = {
        "algorithm": "sha256",
        "value": hashlib.sha256(content).hexdigest(),
    }
    identity = {key: value for key, value in manifest.items() if key != "bundle_id"}
    manifest["bundle_id"] = evidence_bundle_id(identity)
    _pretty_write(manifest_path, manifest)


def _reference_bundle_files() -> tuple[EvidenceBundleFile, ...]:
    return (
        EvidenceBundleFile.from_path(
            REFERENCE_BUNDLE / "inputs" / "diagnosis-request.json",
            path="inputs/diagnosis-request.json",
            role=EvidenceBundleFileRole.INPUT,
            media_type="application/json",
        ),
        EvidenceBundleFile.from_path(
            REFERENCE_BUNDLE / "artifacts" / "model-assertion.json",
            path="artifacts/model-assertion.json",
            role=EvidenceBundleFileRole.ARTIFACT,
            media_type="application/json",
            record_ref="artifact-diagnostician-output",
        ),
    )


def test_checked_in_reference_bundle_is_closed_and_recomputable() -> None:
    verified = EvidenceBundleValidator().validate(REFERENCE_BUNDLE)

    assert verified.manifest["bundle_id"] == EXPECTED_BUNDLE_ID
    assert verified.ledger.snapshot.record_count == 23
    assert verified.receipt["receipt_id"] == (
        "urn:sha256:1506be1be189673e3a11ae7092339800e16e9e4231d1e0d3362f07e7813357cc"
    )
    assert verified.ledger.snapshot.current_states == {
        "hypothesis-cache-key": "supported",
        "hypothesis-worker-source": "refuted",
        "hypothesis-verifier-artifact": "refuted",
    }


def test_builder_reproduces_checked_in_reference_bundle(tmp_path: Path) -> None:
    destination = tmp_path / "bundle"
    ledger = JsonlLedgerStore(REFERENCE_BUNDLE / "ledger.jsonl").load()
    verified = EvidenceBundleBuilder().build(
        destination,
        ledger=ledger,
        title="Deterministic cache-key causal diagnosis",
        created_at=datetime(2026, 7, 23, 12, tzinfo=UTC),
        files=_reference_bundle_files(),
        limitations=(
            "The diagnostician is a deterministic software fixture, not a hosted model.",
            "Structural integrity does not establish that a real-world claim is true.",
            "The causal oracle is authoritative only within this controlled case.",
        ),
    )

    assert verified.path == destination
    assert verified.manifest["bundle_id"] == EXPECTED_BUNDLE_ID
    for source in REFERENCE_BUNDLE.rglob("*"):
        if source.is_file():
            relative = source.relative_to(REFERENCE_BUNDLE)
            assert (destination / relative).read_bytes() == source.read_bytes()


def test_builder_cleans_staging_after_artifact_mismatch(tmp_path: Path) -> None:
    destination = tmp_path / "bundle"
    ledger = JsonlLedgerStore(REFERENCE_BUNDLE / "ledger.jsonl").load()

    with pytest.raises(EvidenceBundleValidationError, match="digest"):
        EvidenceBundleBuilder().build(
            destination,
            ledger=ledger,
            title="Tampered bundle",
            files=(
                EvidenceBundleFile(
                    path="artifacts/model-assertion.json",
                    content=b"tampered",
                    role=EvidenceBundleFileRole.ARTIFACT,
                    media_type="application/json",
                    record_ref="artifact-diagnostician-output",
                ),
            ),
        )

    assert not destination.exists()
    assert tuple(tmp_path.iterdir()) == ()


def test_builder_reports_artifact_without_closed_bundle_digest(
    tmp_path: Path,
) -> None:
    records = list(JsonlLedgerStore(REFERENCE_BUNDLE / "ledger.jsonl").load().records)
    artifact = next(
        record for record in records if record["kind"] == "artifact_reference"
    )
    del artifact["digest"]
    ledger = Ledger(records)

    with pytest.raises(
        EvidenceBundleValidationError,
        match="must declare a digest",
    ):
        EvidenceBundleBuilder().build(
            tmp_path / "bundle",
            ledger=ledger,
            title="Missing digest",
            files=(
                EvidenceBundleFile.from_path(
                    REFERENCE_BUNDLE / "artifacts" / "model-assertion.json",
                    path="artifacts/model-assertion.json",
                    role=EvidenceBundleFileRole.ARTIFACT,
                    media_type="application/json",
                    record_ref="artifact-diagnostician-output",
                ),
            ),
        )


def test_builder_refuses_to_overwrite_existing_destination(tmp_path: Path) -> None:
    destination = tmp_path / "bundle"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")
    ledger = JsonlLedgerStore(REFERENCE_BUNDLE / "ledger.jsonl").load()

    with pytest.raises(FileExistsError):
        EvidenceBundleBuilder().build(
            destination,
            ledger=ledger,
            title="No overwrite",
            files=(),
        )

    assert marker.read_text(encoding="utf-8") == "preserve"


def test_builder_preserves_destination_created_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "bundle"
    ledger = JsonlLedgerStore(REFERENCE_BUNDLE / "ledger.jsonl").load()

    def publish_after_competitor(source: Path, target: Path) -> None:
        target.mkdir()
        publish_path_no_replace(source, target)

    monkeypatch.setattr(
        evidence_bundle_module,
        "publish_path_no_replace",
        publish_after_competitor,
    )

    with pytest.raises(FileExistsError):
        EvidenceBundleBuilder().build(
            destination,
            ledger=ledger,
            title="Concurrent destination",
            files=_reference_bundle_files(),
        )

    assert destination.is_dir()
    assert tuple(destination.iterdir()) == ()
    assert tuple(tmp_path.glob(".bundle.*")) == ()


@pytest.mark.parametrize(
    "path",
    ["bundle.json", "ledger.jsonl", "reasoning-receipt.json"],
)
def test_builder_rejects_reserved_file_paths(path: str) -> None:
    with pytest.raises(EvidenceBundleBuildError, match="reserved"):
        EvidenceBundleFile(
            path=path,
            content=b"data",
            role=EvidenceBundleFileRole.INPUT,
            media_type="application/octet-stream",
        )


def test_bundle_file_from_path_rejects_source_over_limit(tmp_path: Path) -> None:
    source = tmp_path / "large.bin"
    source.write_bytes(b"1234")

    with pytest.raises(EvidenceBundleBuildError, match="exceeds limit 3"):
        EvidenceBundleFile.from_path(
            source,
            path="inputs/large.bin",
            role=EvidenceBundleFileRole.INPUT,
            media_type="application/octet-stream",
            max_bytes=3,
        )


def test_tampered_inventory_file_is_rejected(tmp_path: Path) -> None:
    destination = tmp_path / "bundle"
    shutil.copytree(REFERENCE_BUNDLE, destination)
    (destination / "ledger.jsonl").write_bytes(
        (destination / "ledger.jsonl").read_bytes() + b"\n"
    )

    with pytest.raises(EvidenceBundleValidationError, match="size mismatch"):
        EvidenceBundleValidator().validate(destination)


def test_schema_valid_receipt_tampering_fails_ledger_binding(tmp_path: Path) -> None:
    destination = tmp_path / "bundle"
    shutil.copytree(REFERENCE_BUNDLE, destination)
    receipt_path = destination / "reasoning-receipt.json"
    receipt = _load_object(receipt_path)
    limitations = cast(list[JsonValue], receipt["limitations"])
    limitations.append("A schema-valid but noncanonical mutation.")
    _pretty_write(receipt_path, receipt)
    _rebind_file_and_bundle(destination, "reasoning-receipt.json")

    with pytest.raises(
        ValueError,
        match="does not match the canonical projection",
    ):
        EvidenceBundleValidator().validate(destination)


def test_uninventoried_file_is_rejected(tmp_path: Path) -> None:
    destination = tmp_path / "bundle"
    shutil.copytree(REFERENCE_BUNDLE, destination)
    (destination / "unexpected.txt").write_text("not inventoried", encoding="utf-8")

    with pytest.raises(EvidenceBundleValidationError, match="inventory mismatch"):
        EvidenceBundleValidator().validate(destination)


def test_validator_rejects_oversized_manifest_before_decoding() -> None:
    manifest_size = (REFERENCE_BUNDLE / "bundle.json").stat().st_size
    validator = EvidenceBundleValidator(
        limits=EvidenceBundleLimits(max_manifest_bytes=manifest_size - 1)
    )

    with pytest.raises(EvidenceBundleValidationError, match="exceeds limit"):
        validator.validate(REFERENCE_BUNDLE)


def test_validator_rejects_inventory_file_count_over_limit() -> None:
    validator = EvidenceBundleValidator(limits=EvidenceBundleLimits(max_files=3))

    with pytest.raises(EvidenceBundleValidationError, match="file count 4"):
        validator.validate(REFERENCE_BUNDLE)


def test_validator_rejects_individual_file_over_limit() -> None:
    validator = EvidenceBundleValidator(
        limits=EvidenceBundleLimits(max_file_bytes=1000)
    )

    with pytest.raises(EvidenceBundleValidationError, match="exceeding limit 1000"):
        validator.validate(REFERENCE_BUNDLE)


def test_validator_rejects_aggregate_bytes_over_limit() -> None:
    manifest = _load_object(REFERENCE_BUNDLE / "bundle.json")
    entries = cast(list[JsonObject], manifest["files"])
    total_bytes = sum(cast(int, entry["size_bytes"]) for entry in entries)
    validator = EvidenceBundleValidator(
        limits=EvidenceBundleLimits(max_total_bytes=total_bytes - 1)
    )

    with pytest.raises(EvidenceBundleValidationError, match="byte total"):
        validator.validate(REFERENCE_BUNDLE)


@pytest.mark.parametrize(
    ("field", "relative_path"),
    [
        ("max_ledger_bytes", "ledger.jsonl"),
        ("max_receipt_bytes", "reasoning-receipt.json"),
    ],
)
def test_validator_applies_role_specific_byte_limits(
    field: str,
    relative_path: str,
) -> None:
    size = (REFERENCE_BUNDLE / relative_path).stat().st_size
    limits = EvidenceBundleLimits(**{field: size - 1})

    with pytest.raises(EvidenceBundleValidationError, match="exceeding limit"):
        EvidenceBundleValidator(limits=limits).validate(REFERENCE_BUNDLE)


def test_validator_stops_ledger_decoding_at_record_limit() -> None:
    validator = EvidenceBundleValidator(
        limits=EvidenceBundleLimits(max_ledger_records=22)
    )

    with pytest.raises(LedgerFormatError, match="record count exceeds limit 22"):
        validator.validate(REFERENCE_BUNDLE)


def test_validator_bounds_directory_traversal() -> None:
    validator = EvidenceBundleValidator(
        limits=EvidenceBundleLimits(max_directory_entries=3)
    )

    with pytest.raises(EvidenceBundleValidationError, match="entry count"):
        validator.validate(REFERENCE_BUNDLE)


def test_inventory_digests_are_streamed_without_path_read_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_read_bytes(_path: Path) -> bytes:
        raise AssertionError("inventory validation must stream file bytes")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    EvidenceBundleValidator().validate(REFERENCE_BUNDLE)


def test_validator_rejects_symbolic_link_manifest(tmp_path: Path) -> None:
    destination = tmp_path / "bundle"
    shutil.copytree(REFERENCE_BUNDLE, destination)
    manifest = destination / "bundle.json"
    external = tmp_path / "external-manifest.json"
    external.write_bytes(manifest.read_bytes())
    manifest.unlink()
    try:
        manifest.symlink_to(external)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(EvidenceBundleValidationError, match="non-symlink"):
        EvidenceBundleValidator().validate(destination)


def test_builder_applies_limits_before_creating_staging_directory(
    tmp_path: Path,
) -> None:
    ledger = JsonlLedgerStore(REFERENCE_BUNDLE / "ledger.jsonl").load()
    builder = EvidenceBundleBuilder(
        validator=EvidenceBundleValidator(
            limits=EvidenceBundleLimits(max_ledger_records=22)
        )
    )

    with pytest.raises(EvidenceBundleBuildError, match="record count 23"):
        builder.build(
            tmp_path / "bundle",
            ledger=ledger,
            title="Over the configured record limit",
            files=(),
        )

    assert tuple(tmp_path.iterdir()) == ()


@pytest.mark.parametrize(
    ("value", "error_type"),
    [
        (-1, ValueError),
        (True, TypeError),
        (1.5, TypeError),
    ],
)
def test_limits_reject_invalid_values(
    value: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        EvidenceBundleLimits(max_files=cast(int, value))


@pytest.mark.parametrize(
    "value",
    ["", "/absolute.json", "../escape.json", "a//b.json", "a\\b.json", "C:file"],
)
def test_bundle_paths_reject_smuggling(value: str) -> None:
    with pytest.raises(EvidenceBundleValidationError):
        parse_bundle_path(value)
