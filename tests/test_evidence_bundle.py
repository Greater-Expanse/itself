# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import itself.evidence_bundle as evidence_bundle_module
from itself import (
    BundleIntegrityError,
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
    StrPath,
    VerifiedEvidenceBundle,
    build_reasoning_receipt,
    canonical_ledger_bytes,
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


def _rebind_manifest(root: Path, manifest: JsonObject) -> None:
    identity = {key: value for key, value in manifest.items() if key != "bundle_id"}
    manifest["bundle_id"] = evidence_bundle_id(identity)
    _pretty_write(root / "bundle.json", manifest)


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


def test_builder_runs_full_validation_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validated: list[Path] = []
    validate = EvidenceBundleValidator.validate

    def recording_validate(
        validator: EvidenceBundleValidator,
        path: StrPath,
    ) -> VerifiedEvidenceBundle:
        validated.append(Path(path))
        return validate(validator, path)

    monkeypatch.setattr(EvidenceBundleValidator, "validate", recording_validate)
    destination = tmp_path / "bundle"

    verified = EvidenceBundleBuilder().build(
        destination,
        ledger=JsonlLedgerStore(REFERENCE_BUNDLE / "ledger.jsonl").load(),
        title="Deterministic cache-key causal diagnosis",
        created_at=datetime(2026, 7, 23, 12, tzinfo=UTC),
        files=_reference_bundle_files(),
        limitations=(
            "The diagnostician is a deterministic software fixture, not a hosted model.",
            "Structural integrity does not establish that a real-world claim is true.",
            "The causal oracle is authoritative only within this controlled case.",
        ),
    )

    assert len(validated) == 1
    assert validated[0].parent == tmp_path
    assert validated[0].name.startswith(".bundle.")
    assert verified.path == destination
    assert verified.manifest["bundle_id"] == EXPECTED_BUNDLE_ID


def test_builder_rejects_bundle_changed_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = JsonlLedgerStore(REFERENCE_BUNDLE / "ledger.jsonl").load()

    def publish_after_edit(source: Path, target: Path) -> None:
        request = source / "inputs" / "diagnosis-request.json"
        request.write_bytes(request.read_bytes() + b"\n")
        publish_path_no_replace(source, target)

    monkeypatch.setattr(
        evidence_bundle_module,
        "publish_path_no_replace",
        publish_after_edit,
    )

    with pytest.raises(
        EvidenceBundleValidationError,
        match="size mismatch for 'inputs/diagnosis-request.json'",
    ):
        EvidenceBundleBuilder().build(
            tmp_path / "bundle",
            ledger=ledger,
            title="Changed between validation and publication",
            files=_reference_bundle_files(),
        )

    assert not (tmp_path / "bundle").exists()
    assert list(tmp_path.iterdir()) == []


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


def test_validator_decodes_only_bytes_checked_against_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "bundle"
    shutil.copytree(REFERENCE_BUNDLE, destination)
    records = list(JsonlLedgerStore(REFERENCE_BUNDLE / "ledger.jsonl").load().records)
    injected = deepcopy(
        next(record for record in records if record["kind"] == "hypothesis")
    )
    injected["id"] = "hypothesis-injected"
    injected["statement"] = "Injected after the inventory pass."
    forged = Ledger([*records, injected])

    def replace_after_inventory(manifest_without_id: JsonObject) -> str:
        (destination / "ledger.jsonl").write_bytes(canonical_ledger_bytes(forged))
        _pretty_write(
            destination / "reasoning-receipt.json",
            build_reasoning_receipt(forged),
        )
        return evidence_bundle_id(manifest_without_id)

    # The bundle_id check is the validator's first step after the inventory pass.
    monkeypatch.setattr(
        evidence_bundle_module,
        "evidence_bundle_id",
        replace_after_inventory,
    )

    with pytest.raises(
        EvidenceBundleValidationError,
        match="mismatch for 'ledger.jsonl'",
    ):
        EvidenceBundleValidator().validate(destination)


def test_validator_rejects_fractional_size_bytes(tmp_path: Path) -> None:
    destination = tmp_path / "bundle"
    shutil.copytree(REFERENCE_BUNDLE, destination)
    manifest = _load_object(destination / "bundle.json")
    entry = cast(list[JsonObject], manifest["files"])[0]
    entry["size_bytes"] = float(cast(int, entry["size_bytes"]))
    _rebind_manifest(destination, manifest)

    with pytest.raises(
        EvidenceBundleValidationError,
        match="must write size_bytes as an integer literal, not 3302.0",
    ):
        EvidenceBundleValidator().validate(destination)


def test_validator_reports_inventory_paths_it_cannot_inspect(tmp_path: Path) -> None:
    destination = tmp_path / "bundle"
    shutil.copytree(REFERENCE_BUNDLE, destination)
    manifest = _load_object(destination / "bundle.json")
    entry = cast(list[JsonObject], manifest["files"])[0]
    entry["path"] = f"inputs/{'a' * 300}.json"
    _rebind_manifest(destination, manifest)

    # Most Python versions raise for the overlong name and the validator reports
    # it; Python 3.14's pathlib reports such a path as absent instead.
    with pytest.raises(
        EvidenceBundleValidationError,
        match="cannot be inspected|is missing or not regular",
    ):
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
    with pytest.raises(EvidenceBundleBuildError):
        EvidenceBundleFile(
            path=value,
            content=b"data",
            role=EvidenceBundleFileRole.INPUT,
            media_type="application/octet-stream",
        )


@pytest.mark.parametrize(
    "value",
    [
        "artifacts/%2e%2e/%2e%2e/outside.json",
        "artifacts/model-assertion.json#fragment",
        "artifacts/model-assertion.json?version=1",
        "inputs/100%.json",
    ],
)
def test_builder_rejects_uri_sensitive_paths_the_validator_accepts(value: str) -> None:
    assert parse_bundle_path(value).as_posix() == value
    with pytest.raises(EvidenceBundleBuildError, match="must not contain"):
        EvidenceBundleFile(
            path=value,
            content=b"data",
            role=EvidenceBundleFileRole.ARTIFACT,
            media_type="application/json",
            record_ref="artifact-diagnostician-output",
        )


def test_bundle_validation_can_require_trusted_authorizers() -> None:
    trusted = EvidenceBundleValidator(
        trusted_authorizers=["case-001-promotion-policy"]
    ).validate(REFERENCE_BUNDLE)

    assert (
        trusted.manifest["bundle_id"]
        == (EvidenceBundleValidator().validate(REFERENCE_BUNDLE).manifest["bundle_id"])
    )
    with pytest.raises(BundleIntegrityError) as raised:
        EvidenceBundleValidator(trusted_authorizers=()).validate(REFERENCE_BUNDLE)
    issues = raised.value.issues
    assert issues
    assert {issue.code.value for issue in issues} == {"untrusted_authorizer"}
    assert {issue.reference for issue in issues} == {"case-001-promotion-policy"}
    assert EvidenceBundleValidator(
        trusted_authorizers=["a", "a"]
    ).trusted_authorizers == frozenset({"a"})
