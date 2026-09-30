# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Command-line utilities for protocol records and local ledgers."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Final, cast

from ._json import strict_json_loads
from ._version import __version__
from .bundle import BundleIntegrityError
from .evidence_bundle import (
    DEFAULT_EVIDENCE_BUNDLE_LIMITATIONS,
    DEFAULT_EVIDENCE_BUNDLE_LIMITS,
    EvidenceBundleBuilder,
    EvidenceBundleBuildError,
    EvidenceBundleFile,
    EvidenceBundleFileRole,
    EvidenceBundleValidationError,
    EvidenceBundleValidator,
)
from .ledger import JsonlLedgerStore, Ledger, LedgerFormatError
from .receipts import (
    JsonReceiptStore,
    ReasoningReceiptFormatError,
    ReasoningReceiptValidationError,
    ReasoningReceiptValidator,
    build_reasoning_receipt,
)
from .reporting import replay_subjects, summarize_ledger
from .schema_export import SchemaExportError, export_schemas
from .types import JsonObject, JsonValue
from .validation import ProtocolValidator


class _RecordFileError(ValueError):
    pass


# Ledgers and receipts loaded here use the documented evidence-bundle ceilings,
# the bounds a default closed bundle enforces. `ledger append` loads through
# JsonlLedgerStore.extend, which takes no limits.
_CLI_LIMITS: Final = DEFAULT_EVIDENCE_BUNDLE_LIMITS

# Media types for --input and --supplemental files by lowercase suffix. The
# mimetypes module reads host files such as /etc/mime.types and changes across
# Python releases, which would make --created-at regeneration host-dependent.
_MEDIA_TYPES: Final = MappingProxyType(
    {
        ".csv": "text/csv",
        ".gif": "image/gif",
        ".htm": "text/html",
        ".html": "text/html",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".json": "application/json",
        ".jsonl": "application/x-ndjson",
        ".log": "text/plain",
        ".md": "text/markdown",
        ".ndjson": "application/x-ndjson",
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".tsv": "text/tab-separated-values",
        ".txt": "text/plain",
        ".webp": "image/webp",
        ".xml": "application/xml",
        ".yaml": "application/yaml",
        ".yml": "application/yaml",
        ".zip": "application/zip",
    }
)


def _timestamp_argument(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an ISO 8601 date-time"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("date-time must include a UTC offset")
    return parsed.astimezone(UTC)


def _add_external_refs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--external-ref",
        action="append",
        default=[],
        metavar="ID",
        help="declare an unresolved identifier external; may be repeated",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and inspect Claim and Evidence Protocol records."
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    validate = subcommands.add_parser(
        "validate",
        help="validate individual JSON protocol records",
    )
    validate.add_argument("files", nargs="+", type=Path)

    bundle = subcommands.add_parser(
        "bundle",
        help="create or validate a closed, digest-inventoried evidence bundle",
    )
    bundle_commands = bundle.add_subparsers(dest="bundle_command", required=True)
    bundle_create = bundle_commands.add_parser(
        "create",
        help="materialize a validated ledger, receipt, and referenced artifacts",
    )
    bundle_create.add_argument("ledger", type=Path)
    bundle_create.add_argument("destination", type=Path)
    bundle_create.add_argument("--title", required=True)
    bundle_create.add_argument(
        "--created-at",
        type=_timestamp_argument,
        help="bundle date-time with UTC offset; defaults to the current time",
    )
    bundle_create.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="RECORD_REF=FILE",
        help="map an artifact_reference record to its source file; may be repeated",
    )
    bundle_create.add_argument(
        "--input",
        action="append",
        default=[],
        type=Path,
        metavar="FILE",
        help="include a file under inputs/; may be repeated",
    )
    bundle_create.add_argument(
        "--supplemental",
        action="append",
        default=[],
        type=Path,
        metavar="FILE",
        help="include a file under supplemental/; may be repeated",
    )
    bundle_create.add_argument(
        "--limitation",
        action="append",
        default=[],
        metavar="TEXT",
        help="add a bundle-specific limitation; may be repeated",
    )
    bundle_validate = bundle_commands.add_parser(
        "validate",
        help="verify bundle bytes, ledger, receipt, and artifact bindings",
    )
    bundle_validate.add_argument("bundle", type=Path)

    ledger = subcommands.add_parser(
        "ledger",
        help="write and inspect a local JSON Lines ledger",
    )
    ledger_commands = ledger.add_subparsers(dest="ledger_command", required=True)

    ledger_validate = ledger_commands.add_parser(
        "validate",
        help="validate and replay a complete ledger",
    )
    ledger_validate.add_argument("ledger", type=Path)
    _add_external_refs(ledger_validate)

    append = ledger_commands.add_parser(
        "append",
        help="atomically append one or more JSON record files",
    )
    append.add_argument("ledger", type=Path)
    append.add_argument("records", nargs="+", type=Path)
    _add_external_refs(append)

    replay = ledger_commands.add_parser(
        "replay",
        help="show initial and current state for every claim and hypothesis",
    )
    replay.add_argument("ledger", type=Path)
    replay.add_argument("--format", choices=("text", "json"), default="text")
    _add_external_refs(replay)

    summary = ledger_commands.add_parser(
        "summary",
        help="summarize record kinds and current epistemic states",
    )
    summary.add_argument("ledger", type=Path)
    summary.add_argument("--format", choices=("text", "json"), default="text")
    _add_external_refs(summary)

    receipt = subcommands.add_parser(
        "receipt",
        help="generate and validate deterministic reasoning receipts",
    )
    receipt_commands = receipt.add_subparsers(dest="receipt_command", required=True)

    receipt_generate = receipt_commands.add_parser(
        "generate",
        help="generate a receipt from a validated ledger",
    )
    receipt_generate.add_argument("ledger", type=Path)
    receipt_generate.add_argument("--output", "-o", type=Path)
    _add_external_refs(receipt_generate)

    receipt_validate = receipt_commands.add_parser(
        "validate",
        help="validate receipt shape and optionally bind it to a ledger",
    )
    receipt_validate.add_argument("receipt", type=Path)
    receipt_validate.add_argument("--ledger", type=Path)
    _add_external_refs(receipt_validate)

    schema = subcommands.add_parser(
        "schema",
        help="inspect or export the canonical packaged JSON Schemas",
    )
    schema_commands = schema.add_subparsers(dest="schema_command", required=True)
    schema_export = schema_commands.add_parser(
        "export",
        help="export canonical schemas, catalogs, and checksums",
    )
    schema_export.add_argument("destination", type=Path)
    return parser


def _load_record(path: Path) -> JsonObject:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise _RecordFileError(f"{path}: cannot read record: {error}") from error
    try:
        value = strict_json_loads(source)
    except (json.JSONDecodeError, ValueError) as error:
        raise _RecordFileError(f"{path}: invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise _RecordFileError(f"{path}: protocol record must be a JSON object")
    return value


def _validate_records(paths: Sequence[Path]) -> int:
    validator = ProtocolValidator()
    failed = False
    for path in paths:
        try:
            record = _load_record(path)
        except _RecordFileError as error:
            print(f"FAIL {error}", file=sys.stderr)
            failed = True
            continue

        issues = validator.errors(record)
        if issues:
            print(f"FAIL {path}", file=sys.stderr)
            for issue in issues:
                print(f"  {issue}", file=sys.stderr)
            failed = True
        else:
            print(f"PASS {path}")
    return 1 if failed else 0


def _load_existing_ledger(
    path: Path,
    external_refs: Collection[str],
) -> Ledger:
    if not path.is_file():
        raise _RecordFileError(f"{path}: ledger file does not exist")
    return JsonlLedgerStore(path).load(
        external_refs=external_refs,
        max_records=_CLI_LIMITS.max_ledger_records,
        max_bytes=_CLI_LIMITS.max_ledger_bytes,
    )


def _record_string(record: JsonObject, field: str) -> str:
    value = record[field]
    if not isinstance(value, str):
        raise TypeError(f"validated record field {field!r} was not a string")
    return value


def _artifact_source_map(specifications: Sequence[str]) -> dict[str, Path]:
    sources: dict[str, Path] = {}
    for specification in specifications:
        record_ref, separator, source = specification.partition("=")
        if not separator or not record_ref.strip() or not source:
            raise _RecordFileError(
                "artifact mapping must use non-empty RECORD_REF=FILE syntax"
            )
        if record_ref in sources:
            raise _RecordFileError(
                f"duplicate artifact mapping for record {record_ref!r}"
            )
        sources[record_ref] = Path(source)
    return sources


def _media_type(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _named_bundle_file(
    source: Path,
    *,
    directory: str,
    role: EvidenceBundleFileRole,
) -> EvidenceBundleFile:
    return EvidenceBundleFile.from_path(
        source,
        path=f"{directory}/{source.name}",
        media_type=_media_type(source),
        role=role,
    )


def _bundle_create(
    ledger_path: Path,
    destination: Path,
    *,
    title: str,
    created_at: datetime | None,
    artifact_specs: Sequence[str],
    input_paths: Sequence[Path],
    supplemental_paths: Sequence[Path],
    additional_limitations: Sequence[str],
) -> int:
    ledger = _load_existing_ledger(ledger_path, ())
    artifact_records = {
        _record_string(record, "id"): record
        for record in ledger.records
        if _record_string(record, "kind") == "artifact_reference"
    }
    artifact_sources = _artifact_source_map(artifact_specs)
    unknown = sorted(set(artifact_sources).difference(artifact_records))
    missing = sorted(set(artifact_records).difference(artifact_sources))
    if unknown or missing:
        raise _RecordFileError(
            "artifact mappings must exactly match ledger artifact_reference records; "
            f"missing={missing}, unknown={unknown}"
        )

    bundle_files = [
        *(
            _named_bundle_file(
                source,
                directory="inputs",
                role=EvidenceBundleFileRole.INPUT,
            )
            for source in input_paths
        ),
        *(
            EvidenceBundleFile.from_path(
                artifact_sources[record_ref],
                path=_record_string(record, "uri"),
                media_type=_record_string(record, "media_type"),
                role=EvidenceBundleFileRole.ARTIFACT,
                record_ref=record_ref,
            )
            for record_ref, record in artifact_records.items()
        ),
        *(
            _named_bundle_file(
                source,
                directory="supplemental",
                role=EvidenceBundleFileRole.SUPPLEMENTAL,
            )
            for source in supplemental_paths
        ),
    ]
    limitations = (
        *DEFAULT_EVIDENCE_BUNDLE_LIMITATIONS,
        *additional_limitations,
    )
    verified = EvidenceBundleBuilder().build(
        destination,
        ledger=ledger,
        title=title,
        created_at=created_at,
        files=bundle_files,
        limitations=limitations,
    )
    file_count = len(cast(list[JsonValue], verified.manifest["files"]))
    print(
        f"PASS {destination}: {verified.ledger.snapshot.record_count} records, "
        f"{file_count} files, {verified.manifest['bundle_id']}"
    )
    return 0


def _bundle_validate(path: Path) -> int:
    verified = EvidenceBundleValidator().validate(path)
    file_count = len(cast(list[JsonValue], verified.manifest["files"]))
    print(
        f"PASS {path}: {verified.ledger.snapshot.record_count} records, "
        f"{file_count} files, {verified.manifest['bundle_id']}"
    )
    return 0


def _ledger_validate(path: Path, external_refs: Collection[str]) -> int:
    ledger = _load_existing_ledger(path, external_refs)
    summary = summarize_ledger(ledger)
    print(
        f"PASS {path}: {summary.record_count} records, {summary.subject_count} subjects"
    )
    return 0


def _ledger_append(
    path: Path,
    record_paths: Sequence[Path],
    external_refs: Collection[str],
) -> int:
    records = tuple(_load_record(record_path) for record_path in record_paths)
    snapshot = JsonlLedgerStore(path).extend(records, external_refs=external_refs)
    print(
        f"PASS {path}: appended {len(records)} records; "
        f"ledger now contains {snapshot.record_count} records"
    )
    return 0


def _ledger_replay(
    path: Path,
    external_refs: Collection[str],
    output_format: str,
) -> int:
    ledger = _load_existing_ledger(path, external_refs)
    subjects = replay_subjects(ledger)
    if output_format == "json":
        print(
            json.dumps(
                [subject.to_json_object() for subject in subjects],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if not subjects:
        print("No claims or hypotheses.")
        return 0
    for subject in subjects:
        transition_label = (
            "transition" if subject.transition_count == 1 else "transitions"
        )
        print(
            f"{subject.subject_id}\t{subject.kind.value}\t"
            f"{subject.initial_status.value} -> {subject.current_status.value}\t"
            f"{subject.transition_count} {transition_label}"
        )
    return 0


def _ledger_summary(
    path: Path,
    external_refs: Collection[str],
    output_format: str,
) -> int:
    summary = summarize_ledger(_load_existing_ledger(path, external_refs))
    if output_format == "json":
        print(
            json.dumps(
                summary.to_json_object(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(f"records: {summary.record_count}")
    print(f"subjects: {summary.subject_count}")
    print("records_by_kind:")
    for kind, count in summary.record_counts.items():
        print(f"  {kind}: {count}")
    print("current_states:")
    for status, count in summary.state_counts.items():
        print(f"  {status.value}: {count}")
    return 0


def _receipt_generate(
    ledger_path: Path,
    output_path: Path | None,
    external_refs: Collection[str],
) -> int:
    ledger = _load_existing_ledger(ledger_path, external_refs)
    if (
        output_path is not None
        and output_path.exists()
        and os.path.samefile(output_path, ledger_path)
    ):
        raise _RecordFileError(
            f"{output_path}: receipt output would replace the source ledger"
        )
    receipt = build_reasoning_receipt(ledger)
    if output_path is None:
        print(
            json.dumps(
                receipt,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    JsonReceiptStore(output_path).write(receipt)
    print(f"PASS {output_path}: receipt generated from {ledger_path}")
    return 0


def _receipt_validate(
    receipt_path: Path,
    ledger_path: Path | None,
    external_refs: Collection[str],
) -> int:
    receipt = JsonReceiptStore(receipt_path).load(
        max_bytes=_CLI_LIMITS.max_receipt_bytes
    )
    if ledger_path is None:
        print(f"PASS {receipt_path}: schema-valid receipt")
        return 0

    ledger = _load_existing_ledger(ledger_path, external_refs)
    ReasoningReceiptValidator().validate_against_ledger(receipt, ledger)
    print(f"PASS {receipt_path}: bound to {ledger_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface and return a process exit status."""

    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            return _validate_records(cast(list[Path], args.files))

        if args.command == "bundle" and args.bundle_command == "create":
            return _bundle_create(
                cast(Path, args.ledger),
                cast(Path, args.destination),
                title=cast(str, args.title),
                created_at=cast(datetime | None, args.created_at),
                artifact_specs=cast(list[str], args.artifact),
                input_paths=cast(list[Path], args.input),
                supplemental_paths=cast(list[Path], args.supplemental),
                additional_limitations=cast(list[str], args.limitation),
            )

        if args.command == "bundle" and args.bundle_command == "validate":
            return _bundle_validate(cast(Path, args.bundle))

        if args.command == "ledger":
            ledger_path = cast(Path, args.ledger)
            external_refs = cast(list[str], args.external_ref)
            if args.ledger_command == "validate":
                return _ledger_validate(ledger_path, external_refs)
            if args.ledger_command == "append":
                return _ledger_append(
                    ledger_path,
                    cast(list[Path], args.records),
                    external_refs,
                )
            if args.ledger_command == "replay":
                return _ledger_replay(
                    ledger_path,
                    external_refs,
                    cast(str, args.format),
                )
            if args.ledger_command == "summary":
                return _ledger_summary(
                    ledger_path,
                    external_refs,
                    cast(str, args.format),
                )

        if args.command == "receipt":
            external_refs = cast(list[str], args.external_ref)
            if args.receipt_command == "generate":
                return _receipt_generate(
                    cast(Path, args.ledger),
                    cast(Path | None, args.output),
                    external_refs,
                )
            if args.receipt_command == "validate":
                return _receipt_validate(
                    cast(Path, args.receipt),
                    cast(Path | None, args.ledger),
                    external_refs,
                )

        if args.command == "schema" and args.schema_command == "export":
            snapshot = export_schemas(cast(Path, args.destination))
            print(
                f"PASS {snapshot.destination}: "
                f"exported {len(snapshot.schemas)} canonical schemas"
            )
            return 0
    except (
        BundleIntegrityError,
        EvidenceBundleBuildError,
        EvidenceBundleValidationError,
        LedgerFormatError,
        ReasoningReceiptFormatError,
        ReasoningReceiptValidationError,
        SchemaExportError,
        _RecordFileError,
        OSError,
    ) as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1

    raise RuntimeError("unreachable command dispatch")


if __name__ == "__main__":
    raise SystemExit(main())
