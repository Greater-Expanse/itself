# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Offline JSON exchange workflow for causal-diagnosis case 001."""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from collections.abc import Sequence
from pathlib import Path

from experiments.diagnostician import (
    DiagnosisRequest,
    DiagnosisResult,
    DiagnosticianContractValidator,
    SuppliedResultDiagnostician,
)
from itself import JsonValue, StrPath
from itself._json import strict_json_loads

from .cache_key_diagnostician import (
    CacheKeyFixtureDiagnostician,
    build_cache_key_request,
)
from .cache_key_omission import CaseEnvironment, Mechanism
from .cache_key_scripted_run import ScriptedRunResult, write_scripted_case


class DiagnosisExchangeError(ValueError):
    """Raised when an offline exchange document cannot be decoded."""

    def __init__(self, path: Path, detail: str) -> None:
        self.path: Path = path
        self.detail: str = detail
        super().__init__(f"{path}: {detail}")


def _atomic_write_json(path: Path, value: JsonValue) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                value,
                handle,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        if path.exists():
            temporary_path.chmod(stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _load_json(path: Path) -> JsonValue:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise DiagnosisExchangeError(
            path, f"document is not valid UTF-8: {error}"
        ) from error
    try:
        return strict_json_loads(text)
    except (json.JSONDecodeError, ValueError) as error:
        raise DiagnosisExchangeError(path, f"invalid JSON: {error}") from error


def build_exchange_request() -> DiagnosisRequest:
    """Return the canonical public request for the transparent reference case."""

    context = CaseEnvironment(
        Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION
    ).diagnostic_context()
    return build_cache_key_request(context)


def write_exchange_request(path: StrPath) -> DiagnosisRequest:
    """Atomically write the canonical diagnostician request document."""

    request = build_exchange_request()
    DiagnosticianContractValidator().validate_request(request)
    _atomic_write_json(Path(path), request.to_json_object())
    return request


def write_fixture_result(path: StrPath) -> DiagnosisResult:
    """Atomically write a deterministic conforming result for tool smoke tests."""

    request = build_exchange_request()
    result = DiagnosticianContractValidator().invoke(
        CacheKeyFixtureDiagnostician(),
        request,
    )
    _atomic_write_json(Path(path), result.to_json_object())
    return result


def load_supplied_result(path: StrPath, request: DiagnosisRequest) -> DiagnosisResult:
    """Decode and validate one external result against its exact request."""

    return DiagnosticianContractValidator().decode_result(
        request,
        _load_json(Path(path)),
    )


def run_supplied_result(
    result_path: StrPath,
    ledger_path: StrPath,
) -> ScriptedRunResult:
    """Execute a validated external assertion and atomically write its ledger."""

    request = build_exchange_request()
    result = load_supplied_result(result_path, request)
    return write_scripted_case(
        ledger_path,
        SuppliedResultDiagnostician(result),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser(
        "prepare",
        help="write the canonical provider-neutral request",
    )
    prepare.add_argument("request")

    fixture = commands.add_parser(
        "fixture-result",
        help="write a deterministic conforming result",
    )
    fixture.add_argument("result")

    run = commands.add_parser(
        "run",
        help="validate an external result and execute its selected test",
    )
    run.add_argument("result")
    run.add_argument("ledger")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one step of the offline request/result exchange."""

    args = _parser().parse_args(argv)
    if args.command == "prepare":
        request = write_exchange_request(args.request)
        print(f"PASS {args.request}: request for {request.case_id}")
        return 0
    if args.command == "fixture-result":
        result = write_fixture_result(args.result)
        print(
            f"PASS {args.result}: result from "
            f"{result.diagnostician.adapter_id}@{result.diagnostician.adapter_version}"
        )
        return 0
    if args.command == "run":
        run = run_supplied_result(args.result, args.ledger)
        print(
            f"PASS {args.ledger}: {len(run.ledger)} records, "
            f"selected {run.intervention.value}"
        )
        return 0
    raise RuntimeError(f"unhandled exchange command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
