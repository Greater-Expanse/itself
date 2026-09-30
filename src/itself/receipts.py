# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Deterministic reasoning receipts derived from validated protocol ledgers."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections import defaultdict
from collections.abc import Iterator, Sequence
from importlib.resources import files
from pathlib import Path
from typing import Final, Protocol, cast

import rfc8785
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ._formats import schema_format_checker
from ._json import format_json_path as _format_path
from ._json import strict_json_loads
from .ledger import Ledger
from .reporting import summarize_ledger
from .types import JsonObject, JsonValue, StrPath

RECEIPT_VERSION: Final = "0.1.0-alpha.2"
LEDGER_CANONICALIZATION: Final = "rfc8785-jsonl-v1"

_ASSURANCE_BASIS: Final = (
    "protocol_schema_conformance",
    "bundle_reference_integrity",
    "deterministic_state_replay",
    "canonical_ledger_digest",
)
_LIMITATIONS: Final = (
    "Structural integrity does not establish factual truth.",
    "The receipt does not independently verify artifact contents or digests.",
    "The receipt does not establish that an oracle or authority is correct.",
)


class _ReceiptSchemaValidator(Protocol):
    """Typed surface used from the partially typed jsonschema dependency."""

    def iter_errors(self, instance: JsonValue) -> Iterator[ValidationError]: ...


def _string(record: JsonObject, field: str) -> str:
    value = record[field]
    if not isinstance(value, str):
        raise TypeError(f"validated field {field!r} was not a string")
    return value


def _optional_string(record: JsonObject, field: str) -> str | None:
    value = record.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"validated field {field!r} was not a string")
    return value


def _strings(record: JsonObject, field: str) -> list[JsonValue]:
    value = record.get(field)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"validated field {field!r} was not a string array")
    return [cast(str, item) for item in value]


def _object(record: JsonObject, field: str) -> JsonObject:
    value = record[field]
    if not isinstance(value, dict):
        raise TypeError(f"validated field {field!r} was not an object")
    return value


def _optional_object(record: JsonObject, field: str) -> JsonObject | None:
    value = record.get(field)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError(f"validated field {field!r} was not an object")
    return value


def _objects(record: JsonObject, field: str) -> tuple[JsonObject, ...]:
    value = record[field]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError(f"validated field {field!r} was not an object array")
    return tuple(cast(JsonObject, item) for item in value)


def _json_objects(values: Sequence[JsonObject]) -> list[JsonValue]:
    return [value for value in values]


def _json_strings(values: Sequence[str]) -> list[JsonValue]:
    return [value for value in values]


def _actor_summary(actor: JsonObject) -> JsonObject:
    value: JsonObject = {
        "id": _string(actor, "id"),
        "actor_type": _string(actor, "actor_type"),
        "role": _string(actor, "role"),
    }
    implementation_ref = _optional_string(actor, "implementation_ref")
    if implementation_ref is not None:
        value["implementation_ref"] = implementation_ref
    return value


def _authority_summary(authority: JsonObject) -> JsonObject:
    value: JsonObject = {
        "authority_type": _string(authority, "authority_type"),
        "actor_ref": _string(authority, "actor_ref"),
        "basis": _string(authority, "basis"),
    }
    independent = authority.get("independent_of_subject")
    if isinstance(independent, bool):
        value["independent_of_subject"] = independent
    return value


def _digest_summary(digest: JsonObject) -> JsonObject:
    return {
        "algorithm": _string(digest, "algorithm"),
        "value": _string(digest, "value"),
    }


def _oracle_summary(oracle: JsonObject) -> JsonObject:
    value: JsonObject = {
        "adapter": _string(oracle, "adapter"),
        "version": _string(oracle, "version"),
        "authority_type": _string(oracle, "authority_type"),
    }
    declared_scope = _optional_string(oracle, "declared_scope")
    if declared_scope is not None:
        value["declared_scope"] = declared_scope
    return value


def canonical_ledger_bytes(ledger: Ledger) -> bytes:
    """Return RFC 8785 records in authoritative order, each followed by LF."""

    return b"".join(rfc8785.dumps(record) + b"\n" for record in ledger.records)


def _canonical_records(ledger: Ledger) -> tuple[JsonObject, ...]:
    records: list[JsonObject] = []
    for record in ledger.records:
        value = strict_json_loads(rfc8785.dumps(record))
        if not isinstance(value, dict):
            raise RuntimeError("canonical protocol record was not a JSON object")
        records.append(value)
    return tuple(records)


def ledger_sha256(ledger: Ledger) -> str:
    """Return the SHA-256 digest of the canonical ledger representation."""

    return hashlib.sha256(canonical_ledger_bytes(ledger)).hexdigest()


def _subject_summaries(
    records: Sequence[JsonObject],
    ledger: Ledger,
) -> tuple[JsonObject, ...]:
    transition_ids: dict[str, list[str]] = defaultdict(list)
    for record in records:
        if _string(record, "kind") == "status_transition":
            transition_ids[_string(record, "subject_ref")].append(_string(record, "id"))

    subjects: list[JsonObject] = []
    for record in records:
        kind = _string(record, "kind")
        if kind not in {"claim", "hypothesis"}:
            continue
        subject_id = _string(record, "id")
        scope = _object(record, "scope")
        subject: JsonObject = {
            "subject_id": subject_id,
            "kind": kind,
            "text": _string(
                record,
                "proposition" if kind == "claim" else "statement",
            ),
            "scope_description": _string(scope, "description"),
            "initial_status": _string(record, "status"),
            "current_status": ledger.snapshot.current_states[subject_id].value,
            "transition_ids": _json_strings(transition_ids[subject_id]),
        }
        subjects.append(subject)
    return tuple(subjects)


def _artifact_summary(record: JsonObject) -> JsonObject:
    value: JsonObject = {
        "id": _string(record, "id"),
        "uri": _string(record, "uri"),
        "media_type": _string(record, "media_type"),
    }
    title = _optional_string(record, "title")
    if title is not None:
        value["title"] = title
    digest = _optional_object(record, "digest")
    if digest is not None:
        value["digest"] = _digest_summary(digest)
    return value


def _test_summary(record: JsonObject) -> JsonObject:
    value: JsonObject = {
        "id": _string(record, "id"),
        "status": _string(record, "status"),
        "design": _string(record, "design"),
        "subject_refs": _strings(record, "subject_refs"),
        "prediction_refs": _strings(record, "prediction_refs"),
        "evidence_refs": _strings(record, "evidence_refs"),
        "oracle": _oracle_summary(_object(record, "oracle")),
    }
    plan_ref = _optional_string(record, "plan_ref")
    if plan_ref is not None:
        value["plan_ref"] = plan_ref
    cost = _optional_object(record, "cost")
    if cost is not None:
        value["cost"] = dict(cost)
    return value


def _relation_summary(relation: JsonObject) -> JsonObject:
    value: JsonObject = {
        "subject_ref": _string(relation, "subject_ref"),
        "relation": _string(relation, "relation"),
    }
    public_note = _optional_string(relation, "public_note")
    if public_note is not None:
        value["public_note"] = public_note
    return value


def _evidence_summary(record: JsonObject) -> JsonObject:
    value: JsonObject = {
        "id": _string(record, "id"),
        "evidence_type": _string(record, "evidence_type"),
        "relations": _json_objects(
            tuple(
                _relation_summary(relation)
                for relation in _objects(record, "relations")
            )
        ),
        "authority": _authority_summary(_object(record, "authority")),
        "artifact_refs": _strings(record, "artifact_refs"),
    }
    for field in ("test_ref", "expires_at"):
        field_value = _optional_string(record, field)
        if field_value is not None:
            value[field] = field_value
    return value


def _verdict_summary(record: JsonObject) -> JsonObject:
    value: JsonObject = {
        "id": _string(record, "id"),
        "subject_ref": _string(record, "subject_ref"),
        "outcome": _string(record, "outcome"),
        "evidence_refs": _strings(record, "evidence_refs"),
        "authority": _authority_summary(_object(record, "authority")),
        "public_rationale": _string(record, "public_rationale"),
    }
    policy_ref = _optional_string(record, "policy_ref")
    if policy_ref is not None:
        value["policy_ref"] = policy_ref
    return value


def _transition_summary(record: JsonObject) -> JsonObject:
    value: JsonObject = {
        "id": _string(record, "id"),
        "subject_ref": _string(record, "subject_ref"),
        "from_status": _string(record, "from_status"),
        "to_status": _string(record, "to_status"),
        "evidence_refs": _strings(record, "evidence_refs"),
        "authorized_by": _actor_summary(_object(record, "authorized_by")),
        "reason": _string(record, "reason"),
    }
    policy_ref = _optional_string(record, "policy_ref")
    if policy_ref is not None:
        value["policy_ref"] = policy_ref
    verdict_ref = _optional_string(record, "verdict_ref")
    if verdict_ref is not None:
        value["verdict_ref"] = verdict_ref
    return value


def _decision_summary(record: JsonObject) -> JsonObject:
    value: JsonObject = {
        "id": _string(record, "id"),
        "question": _string(record, "question"),
        "disposition": _string(record, "disposition"),
        "authorized_by": _actor_summary(_object(record, "authorized_by")),
        "relied_on_claim_refs": _strings(record, "relied_on_claim_refs"),
        "unresolved_claim_refs": _strings(record, "unresolved_claim_refs"),
    }
    policy_ref = _optional_string(record, "policy_ref")
    if policy_ref is not None:
        value["policy_ref"] = policy_ref
    return value


class ReasoningReceiptValidationError(ValueError):
    """Raised when a generated or external reasoning receipt is invalid."""


class ReasoningReceiptFormatError(ValueError):
    """Raised when a receipt file cannot be decoded as a JSON object."""

    def __init__(self, path: Path, detail: str) -> None:
        self.path: Path = path
        self.detail: str = detail
        super().__init__(f"{path}: {detail}")


def decode_receipt_document(content: bytes, *, path: StrPath) -> JsonObject:
    """Decode receipt bytes into one strict JSON object without schema checks.

    ``path`` names the source in diagnostics only; nothing is read from it.
    """

    source = Path(path)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReasoningReceiptFormatError(
            source,
            f"receipt is not valid UTF-8: {error}",
        ) from error
    try:
        value = strict_json_loads(text)
    except (json.JSONDecodeError, ValueError) as error:
        raise ReasoningReceiptFormatError(
            source,
            f"invalid JSON: {error}",
        ) from error
    if not isinstance(value, dict):
        raise ReasoningReceiptFormatError(
            source,
            "receipt must contain one JSON object",
        )
    return value


class ReasoningReceiptValidator:
    """Validate reasoning receipts against the canonical packaged schema."""

    def __init__(self) -> None:
        schema_resource = files("itself").joinpath(
            "schemas",
            "v0alpha2",
            "reasoning-receipt.schema.json",
        )
        schema = cast(
            JsonObject,
            strict_json_loads(schema_resource.read_text(encoding="utf-8")),
        )
        Draft202012Validator.check_schema(schema)
        self._validator = cast(
            _ReceiptSchemaValidator,
            Draft202012Validator(schema, format_checker=schema_format_checker()),
        )

    def errors(self, receipt: JsonValue) -> list[str]:
        """Return stable, human-readable receipt validation errors."""

        issues = sorted(
            self._validator.iter_errors(receipt),
            key=lambda error: tuple(str(item) for item in error.absolute_path),
        )
        return [
            f"{_format_path(tuple(issue.absolute_path))}: {issue.message}"
            for issue in issues
        ]

    def validate(self, receipt: JsonValue) -> None:
        """Validate a receipt or raise ReasoningReceiptValidationError."""

        issues = self.errors(receipt)
        if issues:
            raise ReasoningReceiptValidationError("\n".join(issues))

    def is_valid(self, receipt: JsonValue) -> bool:
        """Return whether a receipt conforms to the canonical format."""

        return not self.errors(receipt)

    def validate_against_ledger(
        self,
        receipt: JsonValue,
        ledger: Ledger,
    ) -> None:
        """Validate shape and require the exact deterministic ledger projection."""

        self.validate(receipt)
        if receipt != build_reasoning_receipt(ledger):
            raise ReasoningReceiptValidationError(
                "receipt does not match the canonical projection of the supplied ledger"
            )


class JsonReceiptStore:
    """Load and atomically persist schema-valid reasoning receipt documents."""

    def __init__(
        self,
        path: StrPath,
        validator: ReasoningReceiptValidator | None = None,
    ) -> None:
        self.path: Path = Path(path)
        self._validator = validator or ReasoningReceiptValidator()

    def load(self, *, max_bytes: int | None = None) -> JsonObject:
        """Decode and validate a receipt document from disk."""

        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        metadata = self.path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise OSError(f"receipt path must not be a symbolic link: {self.path}")
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"receipt path must be a regular file: {self.path}")

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags)
        try:
            opened_metadata = os.fstat(descriptor)
            if not stat.S_ISREG(opened_metadata.st_mode) or (
                opened_metadata.st_dev,
                opened_metadata.st_ino,
            ) != (metadata.st_dev, metadata.st_ino):
                raise OSError(f"receipt path changed while opening: {self.path}")
            if max_bytes is not None and opened_metadata.st_size > max_bytes:
                raise ReasoningReceiptFormatError(
                    self.path,
                    f"receipt size exceeds limit {max_bytes} bytes",
                )
        except BaseException:
            os.close(descriptor)
            raise

        with os.fdopen(descriptor, "rb") as handle:
            source_bytes = handle.read(-1 if max_bytes is None else max_bytes + 1)
            final_size = os.fstat(handle.fileno()).st_size
        if max_bytes is not None and len(source_bytes) > max_bytes:
            raise ReasoningReceiptFormatError(
                self.path,
                f"receipt size exceeds limit {max_bytes} bytes",
            )
        if (
            len(source_bytes) != opened_metadata.st_size
            or final_size != opened_metadata.st_size
        ):
            raise OSError(f"receipt path changed while reading: {self.path}")
        value = decode_receipt_document(source_bytes, path=self.path)
        self._validator.validate(value)
        return value

    def write(self, receipt: JsonObject, *, max_bytes: int | None = None) -> None:
        """Validate and atomically replace the stored receipt document.

        With ``max_bytes``, a receipt that serializes to more bytes is refused
        before anything is written, so ``load`` with the same limit can read
        every receipt this method stores.
        """

        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        self._validator.validate(receipt)
        content = (
            json.dumps(
                receipt,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        if max_bytes is not None and len(content) > max_bytes:
            raise ReasoningReceiptFormatError(
                self.path,
                f"receipt size {len(content)} bytes exceeds limit {max_bytes} bytes",
            )
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            original_metadata = self.path.lstat()
        except FileNotFoundError:
            original_metadata = None
        if original_metadata is not None:
            if stat.S_ISLNK(original_metadata.st_mode):
                raise OSError(f"receipt path must not be a symbolic link: {self.path}")
            if not stat.S_ISREG(original_metadata.st_mode):
                raise OSError(f"receipt path must be a regular file: {self.path}")

        descriptor, temporary_name = tempfile.mkstemp(
            dir=parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())

            try:
                current_metadata = self.path.lstat()
            except FileNotFoundError:
                current_metadata = None
            if original_metadata is None:
                if current_metadata is not None:
                    raise OSError(f"receipt path appeared while writing: {self.path}")
            elif current_metadata is None or (
                current_metadata.st_dev,
                current_metadata.st_ino,
            ) != (
                original_metadata.st_dev,
                original_metadata.st_ino,
            ):
                raise OSError(f"receipt path changed while writing: {self.path}")
            else:
                temporary_path.chmod(stat.S_IMODE(original_metadata.st_mode))
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)


def build_reasoning_receipt(ledger: Ledger) -> JsonObject:
    """Build and validate a deterministic public receipt for one valid ledger."""

    records = _canonical_records(ledger)
    summary = summarize_ledger(ledger)
    digest = ledger_sha256(ledger)
    protocol_versions = sorted(
        {_string(record, "protocol_version") for record in records}
    )
    source_ledger: JsonObject = {
        "canonicalization": LEDGER_CANONICALIZATION,
        "record_count": len(records),
        "protocol_versions": _json_strings(protocol_versions),
        "digest": {
            "algorithm": "sha256",
            "value": digest,
        },
    }
    record_counts: JsonObject = {
        kind: count for kind, count in summary.record_counts.items()
    }
    receipt: JsonObject = {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": f"urn:sha256:{digest}",
        "source_ledger": source_ledger,
        "assurance_basis": _json_strings(_ASSURANCE_BASIS),
        "limitations": _json_strings(_LIMITATIONS),
        "record_counts": record_counts,
        "subjects": _json_objects(_subject_summaries(records, ledger)),
        "artifacts": _json_objects(
            tuple(
                _artifact_summary(record)
                for record in records
                if _string(record, "kind") == "artifact_reference"
            )
        ),
        "tests": _json_objects(
            tuple(
                _test_summary(record)
                for record in records
                if _string(record, "kind") == "test"
            )
        ),
        "evidence": _json_objects(
            tuple(
                _evidence_summary(record)
                for record in records
                if _string(record, "kind") == "evidence"
            )
        ),
        "verdicts": _json_objects(
            tuple(
                _verdict_summary(record)
                for record in records
                if _string(record, "kind") == "verdict"
            )
        ),
        "transitions": _json_objects(
            tuple(
                _transition_summary(record)
                for record in records
                if _string(record, "kind") == "status_transition"
            )
        ),
        "decisions": _json_objects(
            tuple(
                _decision_summary(record)
                for record in records
                if _string(record, "kind") == "decision"
            )
        ),
    }
    ReasoningReceiptValidator().validate(receipt)
    return receipt
