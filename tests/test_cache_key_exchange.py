# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from experiments.cases.cache_key_exchange import (
    DiagnosisExchangeError,
    build_exchange_request,
    load_supplied_result,
    main,
    run_supplied_result,
    write_exchange_request,
    write_fixture_result,
)
from experiments.cases.cache_key_scripted_run import run_scripted_case
from itself import JsonlLedgerStore, JsonValue


def _load(path: Path) -> JsonValue:
    return cast(JsonValue, json.loads(path.read_text(encoding="utf-8")))


def test_exchange_request_is_canonical_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "request.json"

    request = write_exchange_request(path)

    assert _load(path) == request.to_json_object()
    assert request == build_exchange_request()
    assert path.read_bytes().endswith(b"\n")


def test_fixture_result_decodes_against_exact_request(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    original = write_fixture_result(path)

    decoded = load_supplied_result(path, build_exchange_request())

    assert decoded == original


def test_supplied_fixture_produces_default_reference_ledger(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    ledger_path = tmp_path / "ledger.jsonl"
    write_fixture_result(result_path)

    supplied = run_supplied_result(result_path, ledger_path)
    default = run_scripted_case()

    assert supplied.ledger.records == default.ledger.records
    assert JsonlLedgerStore(ledger_path).load().records == default.ledger.records


def test_exchange_rejects_nonstandard_json_numbers(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text('{"value": NaN}\n', encoding="utf-8")

    with pytest.raises(DiagnosisExchangeError, match="non-standard JSON"):
        load_supplied_result(path, build_exchange_request())


def test_exchange_rejects_duplicate_object_keys(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text('{"selected_hypothesis_id":"a","selected_hypothesis_id":"b"}\n')

    with pytest.raises(DiagnosisExchangeError, match="duplicate JSON object key"):
        load_supplied_result(path, build_exchange_request())


def test_exchange_rejects_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_bytes(b"\xff\xfe")

    with pytest.raises(DiagnosisExchangeError, match="not valid UTF-8"):
        load_supplied_result(path, build_exchange_request())


def test_exchange_cli_runs_complete_offline_workflow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request_path = tmp_path / "request.json"
    result_path = tmp_path / "result.json"
    ledger_path = tmp_path / "ledger.jsonl"

    assert main(["prepare", str(request_path)]) == 0
    assert main(["fixture-result", str(result_path)]) == 0
    assert main(["run", str(result_path), str(ledger_path)]) == 0

    output = capsys.readouterr().out
    assert output.count("PASS") == 3
    assert request_path.is_file()
    assert result_path.is_file()
    assert ledger_path.is_file()
