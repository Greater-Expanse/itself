# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Run or validate one preregistered, evidence-enforced model trial bundle."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Final, cast

from experiments._contract_values import integer_value as _integer
from experiments._contract_values import object_value as _object
from experiments._study_contracts import (
    V2_CONTRACT,
    RunnerFailure,
    publish_runner_failure,
)
from experiments.diagnostician import DiagnosisRequest
from experiments.model_adapters import (
    HttpTransport,
    ModelAdapterError,
    OpenAIChatDiagnostician,
    OpenAIChatEndpoint,
    UrllibHttpTransport,
)
from experiments.trial_bundles import (
    TrialBundleBuilder,
    TrialBundleValidationError,
    TrialBundleValidator,
)
from experiments.trial_manifests import (
    TrialManifestValidationError,
    TrialManifestValidator,
    endpoint_from_trial_manifest,
    load_trial_manifest,
    require_running_shipped_adapter,
    trial_manifest_contract,
)
from itself import JsonObject, StrPath

from .cache_key_exchange import build_exchange_request
from .cache_key_scripted_run import SCRIPTED_RUN_METRICS, run_scripted_case

# Errors in the operator's input or in an existing bundle.  Anything else is
# also reported with exit 2, prefixed with its type, because exit 1 means only
# that a valid failed-attempt bundle was published or verified.
_INPUT_ERRORS: Final = (
    FileExistsError,
    TrialBundleValidationError,
    TrialManifestValidationError,
    ValueError,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _error_line(error: Exception) -> str:
    message = "; ".join(line for line in str(error).splitlines() if line)
    if isinstance(error, _INPUT_ERRORS):
        return f"ERROR: {message}"
    return f"ERROR: {type(error).__name__}: {message}"


def _credentials(
    environment: Mapping[str, str],
    endpoint: OpenAIChatEndpoint,
) -> tuple[str, ...]:
    credential = environment.get(endpoint.api_key_env)
    return (credential,) if credential else ()


def run_cache_key_model_case(
    manifest_path: StrPath,
    bundle_path: StrPath,
    *,
    base_url: str,
    api_key_env: str,
    transport: HttpTransport | None = None,
    environment: Mapping[str, str] | None = None,
    now: Callable[[], datetime] = _utc_now,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
) -> JsonObject:
    """Execute exactly one registered attempt and publish its immutable bundle."""

    manifest = load_trial_manifest(manifest_path)
    manifest_validator = TrialManifestValidator()
    manifest_validator.validate(manifest)
    if manifest["condition"] != "evidence_enforced":
        raise TrialManifestValidationError(
            "case-001 model runner requires condition evidence_enforced"
        )
    execution = _object(manifest["execution"], "execution")
    if _integer(execution["trial_count"], "trial_count") != 1:
        raise TrialManifestValidationError(
            "case-001 one-shot runner requires execution.trial_count equal to 1"
        )

    endpoint = endpoint_from_trial_manifest(
        manifest,
        base_url=base_url,
        api_key_env=api_key_env,
    )
    frozen_request = build_exchange_request()
    manifest_validator.validate_against(manifest, endpoint, frozen_request)
    if trial_manifest_contract(manifest) is V2_CONTRACT:
        require_running_shipped_adapter(manifest)

    active_transport = UrllibHttpTransport() if transport is None else transport
    active_environment = os.environ if environment is None else environment
    with TrialBundleBuilder(
        bundle_path,
        manifest,
        now=now,
        metric_fields=SCRIPTED_RUN_METRICS,
    ) as builder:
        if trial_manifest_contract(manifest) is V2_CONTRACT:
            return _run_v2_attempt(
                builder,
                endpoint,
                frozen_request,
                transport=active_transport,
                environment=active_environment,
                now=now,
                monotonic_ns=monotonic_ns,
            )
        adapter = OpenAIChatDiagnostician(
            endpoint=endpoint,
            artifact_sink=builder.artifact_sink,
            transport=active_transport,
            environment=active_environment,
            now=now,
            monotonic_ns=monotonic_ns,
        )
        try:
            result = run_scripted_case(adapter)
        except ModelAdapterError as error:
            return builder.fail(error)
        if result.request.to_json_object() != frozen_request.to_json_object():
            raise RuntimeError("case runner request drifted after manifest validation")
        return builder.complete(result)


def _run_v2_attempt(
    builder: TrialBundleBuilder,
    endpoint: OpenAIChatEndpoint,
    frozen_request: DiagnosisRequest,
    *,
    transport: HttpTransport,
    environment: Mapping[str, str],
    now: Callable[[], datetime],
    monotonic_ns: Callable[[], int],
) -> JsonObject:
    """Run one v2 attempt, publishing a runner failure after its request.

    An interrupt after the request is published as an ``interrupted`` runner
    failure and then raised again, so it still stops the caller.
    """

    try:
        adapter = OpenAIChatDiagnostician(
            endpoint=endpoint,
            artifact_sink=builder.artifact_sink,
            transport=builder.observed_transport(transport),
            environment=environment,
            now=now,
            monotonic_ns=monotonic_ns,
        )
        try:
            result = run_scripted_case(adapter)
        except ModelAdapterError as error:
            return builder.fail(error)
        if result.request.to_json_object() != frozen_request.to_json_object():
            raise RuntimeError("case runner request drifted after manifest validation")
        return builder.complete(result)
    except (Exception, KeyboardInterrupt) as error:
        if not builder.records_runner_failure:
            raise
        failure = RunnerFailure.from_exception(
            error,
            secrets=_credentials(environment, endpoint),
        )
        return publish_runner_failure(error, lambda: builder.fail_runner(failure))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="execute one registered model trial")
    run_parser.add_argument("--manifest", required=True)
    run_parser.add_argument("--bundle", required=True)
    run_parser.add_argument("--base-url", required=True)
    run_parser.add_argument("--api-key-env", required=True)

    validate_parser = commands.add_parser(
        "validate",
        help="verify a completed or failed trial bundle",
    )
    validate_parser.add_argument("--bundle", required=True)
    return parser


def _summary(bundle_path: str, bundle_manifest: JsonObject) -> JsonObject:
    return {
        "bundle": bundle_path,
        "bundle_id": bundle_manifest["bundle_id"],
        "outcome": bundle_manifest["outcome"],
        "trial_series_id": bundle_manifest["trial_series_id"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Execute a registered trial or independently verify an existing bundle."""

    args = _parser().parse_args(argv)
    command = cast(str, args.command)
    bundle_path = cast(str, args.bundle)
    try:
        if command == "run":
            bundle_manifest = run_cache_key_model_case(
                cast(str, args.manifest),
                bundle_path,
                base_url=cast(str, args.base_url),
                api_key_env=cast(str, args.api_key_env),
            )
        else:
            verified = TrialBundleValidator().validate(
                bundle_path,
                build_exchange_request(),
            )
            bundle_manifest = verified.bundle_manifest
    except Exception as error:
        print(_error_line(error), file=sys.stderr)
        return 2

    print(json.dumps(_summary(bundle_path, bundle_manifest), indent=2, sort_keys=True))
    return 0 if bundle_manifest["outcome"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
