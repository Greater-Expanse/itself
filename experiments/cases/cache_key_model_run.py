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
from typing import cast

from experiments._contract_values import integer_value as _integer
from experiments._contract_values import object_value as _object
from experiments.model_adapters import (
    HttpTransport,
    ModelAdapterError,
    OpenAIChatDiagnostician,
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
)
from itself import JsonObject, StrPath

from .cache_key_exchange import build_exchange_request
from .cache_key_scripted_run import run_scripted_case


def _utc_now() -> datetime:
    return datetime.now(UTC)


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

    with TrialBundleBuilder(bundle_path, manifest, now=now()) as builder:
        adapter = OpenAIChatDiagnostician(
            endpoint=endpoint,
            artifact_sink=builder.artifact_sink,
            transport=UrllibHttpTransport() if transport is None else transport,
            environment=os.environ if environment is None else environment,
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
    except (
        FileExistsError,
        TrialBundleValidationError,
        TrialManifestValidationError,
        ValueError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    print(json.dumps(_summary(bundle_path, bundle_manifest), indent=2, sort_keys=True))
    return 0 if bundle_manifest["outcome"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
