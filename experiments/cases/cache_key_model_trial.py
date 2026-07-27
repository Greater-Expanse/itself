# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Run one opt-in model-adapter conformance trial against causal case 001."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from experiments.diagnostician import DiagnosticianContractValidator
from experiments.model_adapters import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    PROTOCOL_ID,
    DirectoryArtifactSink,
    HttpTransport,
    ModelAdapterError,
    OpenAIChatDiagnostician,
    OpenAIChatEndpoint,
    UrllibHttpTransport,
    diagnosis_output_profile,
)
from itself import JsonObject, JsonValue, StrPath, StructuredOutputProfile

from .cache_key_exchange import build_exchange_request

TRIAL_REPORT_VERSION: Final = "0.4.0"


def _canonical_json_bytes(value: JsonValue) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("trial timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _base_report(
    endpoint: OpenAIChatEndpoint,
    profile: StructuredOutputProfile,
) -> JsonObject:
    request = build_exchange_request()
    return {
        "report_version": TRIAL_REPORT_VERSION,
        "case_id": request.case_id,
        "request_sha256": hashlib.sha256(
            _canonical_json_bytes(request.to_json_object())
        ).hexdigest(),
        "assertion_schema_sha256": hashlib.sha256(
            _canonical_json_bytes(profile.schema_json())
        ).hexdigest(),
        "profile_id": profile.profile_id,
        "endpoint": {
            "actor_id": endpoint.actor_id,
            "protocol": PROTOCOL_ID,
            "base_url_sha256": _sha256_text(endpoint.base_url),
            "model": endpoint.model,
            "max_output_tokens": endpoint.max_output_tokens,
            "stream": endpoint.stream,
            "timeout_seconds": endpoint.timeout_seconds,
        },
        "adapter": {
            "id": ADAPTER_ID,
            "version": ADAPTER_VERSION,
        },
        "attempt_count": 1,
    }


def run_cache_key_model_trial(
    endpoint: OpenAIChatEndpoint,
    artifact_directory: StrPath,
    *,
    transport: HttpTransport | None = None,
    environment: Mapping[str, str] | None = None,
    now: Callable[[], datetime] = _utc_now,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    expected_observation_values: Sequence[str] = (),
) -> JsonObject:
    """Run one attempt and return a secret-free conformance report."""

    request = build_exchange_request()
    profile = diagnosis_output_profile(expected_observation_values)
    adapter = OpenAIChatDiagnostician(
        endpoint=endpoint,
        artifact_sink=DirectoryArtifactSink(Path(artifact_directory)),
        profile=profile,
        transport=UrllibHttpTransport() if transport is None else transport,
        environment=os.environ if environment is None else environment,
        now=now,
        monotonic_ns=monotonic_ns,
    )
    report = _base_report(endpoint, profile)
    try:
        result = DiagnosticianContractValidator().invoke(adapter, request)
    except ModelAdapterError as error:
        failure: JsonObject = {
            "kind": error.failure.value,
            "retryable": error.retryable,
            "status_code": error.status_code,
        }
        if error.artifact is not None:
            failure["response_artifact"] = error.artifact.to_json_object()
        report.update(
            {
                "outcome": "failed",
                "failure": failure,
                "completed_at": _format_timestamp(now()),
            }
        )
        return report

    report.update(
        {
            "outcome": "conformant",
            "result": result.to_json_object(),
            "completed_at": _format_timestamp(now()),
        }
    )
    return report


def write_trial_report(path: StrPath, report: JsonObject) -> None:
    """Atomically write a private conformance report."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                report,
                handle,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor-id", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key-env", required=True)
    parser.add_argument("--artifact-directory", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--max-output-tokens", type=int, default=2_048)
    parser.add_argument(
        "--stream",
        action="store_true",
        help="request and locally assemble a bounded SSE response",
    )
    parser.add_argument(
        "--expected-observation-value",
        action="append",
        default=[],
        help="register one exact categorical expected-observation value",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one configured live trial and write its sanitized report."""

    args = _parser().parse_args(argv)
    endpoint = OpenAIChatEndpoint(
        actor_id=args.actor_id,
        base_url=args.base_url,
        model=args.model,
        api_key_env=args.api_key_env,
        timeout_seconds=args.timeout_seconds,
        max_output_tokens=args.max_output_tokens,
        stream=args.stream,
    )
    report = run_cache_key_model_trial(
        endpoint,
        cast(str, args.artifact_directory),
        expected_observation_values=cast(list[str], args.expected_observation_value),
    )
    report_path = cast(str, args.report)
    write_trial_report(report_path, report)
    outcome = cast(str, report["outcome"])
    if outcome == "conformant":
        print(
            f"PASS {report_path}: {endpoint.actor_id} returned a conforming assertion"
        )
        return 0

    failure = cast(JsonObject, report["failure"])
    print(
        f"FAIL {report_path}: {endpoint.actor_id} "
        f"failed at {cast(str, failure['kind'])}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
