# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Run or validate one preregistered paired model-comparison replicate."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, cast

from experiments._study_contracts import (
    V2_CONTRACT,
    RunnerFailure,
    publish_runner_failure,
)
from experiments.comparison_bundles import (
    ComparisonBundleBuilder,
    ComparisonBundleValidationError,
    ComparisonBundleValidator,
)
from experiments.comparison_manifests import (
    ComparisonManifestValidationError,
    ComparisonManifestValidator,
    comparison_manifest_contract,
    diagnosis_profile_from_comparison_manifest,
    endpoint_from_comparison_manifest,
    load_comparison_manifest,
    require_running_shipped_adapter,
)
from experiments.diagnostician import DiagnosisRequest
from experiments.model_adapters import (
    HttpTransport,
    ModelAdapterError,
    OpenAIChatDiagnostician,
    OpenAIChatEndpoint,
    OpenAIChatStructuredInvoker,
    StructuredOutputProfile,
    UrllibHttpTransport,
)
from experiments.narrative_diagnostician import (
    ModelNarrativeDiagnostician,
    NarrativeResult,
)
from itself import JsonObject, StrPath

from .cache_key_comparison import (
    NARRATIVE_ATTEMPT_ID,
    STRUCTURED_ATTEMPT_ID,
    build_comparison_request,
    project_comparative_results,
)
from .cache_key_omission import CaseEnvironment, Mechanism

# Errors in the operator's input or in an existing bundle.  Anything else is
# also reported with exit 2, prefixed with its type, because exit 1 means only
# that a valid failed-attempt bundle was published or verified.
_INPUT_ERRORS: Final = (
    FileExistsError,
    ComparisonBundleValidationError,
    ComparisonManifestValidationError,
    ValueError,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _error_line(error: Exception) -> str:
    message = "; ".join(line for line in str(error).splitlines() if line)
    if isinstance(error, _INPUT_ERRORS):
        return f"ERROR: {message}"
    return f"ERROR: {type(error).__name__}: {message}"


def _frozen_request() -> DiagnosisRequest:
    context = CaseEnvironment(
        Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION
    ).diagnostic_context()
    return build_comparison_request(context)


def run_cache_key_comparison(
    manifest_path: StrPath,
    bundle_path: StrPath,
    *,
    replicate_index: int,
    base_url: str,
    api_key_env: str,
    transport: HttpTransport | None = None,
    environment: Mapping[str, str] | None = None,
    now: Callable[[], datetime] = _utc_now,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
) -> JsonObject:
    """Execute one registered two-attempt comparison without retry or repair."""

    manifest = load_comparison_manifest(manifest_path)
    endpoint = endpoint_from_comparison_manifest(
        manifest,
        base_url=base_url,
        api_key_env=api_key_env,
    )
    structured_profile = diagnosis_profile_from_comparison_manifest(manifest)
    request = _frozen_request()
    ComparisonManifestValidator().validate_against(manifest, endpoint, request)
    if comparison_manifest_contract(manifest) is V2_CONTRACT:
        require_running_shipped_adapter(manifest)

    active_transport = UrllibHttpTransport() if transport is None else transport
    active_environment = os.environ if environment is None else environment
    with ComparisonBundleBuilder(
        bundle_path,
        manifest,
        request,
        replicate_index=replicate_index,
        now=now,
    ) as builder:
        if comparison_manifest_contract(manifest) is V2_CONTRACT:
            return _V2Replicate(
                builder=builder,
                endpoint=endpoint,
                structured_profile=structured_profile,
                request=request,
                transport=active_transport,
                environment=active_environment,
                now=now,
                monotonic_ns=monotonic_ns,
            ).run()
        narrative = ModelNarrativeDiagnostician(
            OpenAIChatStructuredInvoker(
                endpoint=endpoint,
                artifact_sink=builder.narrative_artifact_sink,
                transport=active_transport,
                environment=active_environment,
                now=now,
                monotonic_ns=monotonic_ns,
            )
        )
        try:
            narrative_result = narrative.diagnose(request)
        except ModelAdapterError as error:
            return builder.fail(
                error,
                attempt_id=NARRATIVE_ATTEMPT_ID,
            )

        structured = OpenAIChatDiagnostician(
            endpoint=endpoint,
            artifact_sink=builder.structured_artifact_sink,
            profile=structured_profile,
            transport=active_transport,
            environment=active_environment,
            now=now,
            monotonic_ns=monotonic_ns,
        )
        try:
            structured_result = structured.diagnose(request)
        except ModelAdapterError as error:
            return builder.fail(
                error,
                attempt_id=STRUCTURED_ATTEMPT_ID,
                narrative_result=narrative_result,
            )

        result = project_comparative_results(
            request,
            narrative_result,
            structured_result,
        )
        return builder.complete(result)


@dataclass(slots=True)
class _V2Replicate:
    """One v2 replicate, tracking which attempt a runner failure belongs to.

    A runner failure after a request is published against the attempt whose
    result was not yet recorded: the attempt in progress, or the structured
    attempt once both responses have arrived.  An interrupt is published the
    same way, as ``interrupted``, and then raised again.
    """

    builder: ComparisonBundleBuilder
    endpoint: OpenAIChatEndpoint
    structured_profile: StructuredOutputProfile
    request: DiagnosisRequest
    transport: HttpTransport
    environment: Mapping[str, str]
    now: Callable[[], datetime]
    monotonic_ns: Callable[[], int]
    attempt_id: str = NARRATIVE_ATTEMPT_ID
    narrative_result: NarrativeResult | None = None

    def run(self) -> JsonObject:
        """Run both attempts, publishing exactly one bundle for the replicate."""

        try:
            return self._attempts()
        except (Exception, KeyboardInterrupt) as error:
            if not self.builder.records_runner_failure:
                raise
            failure = RunnerFailure.from_exception(
                error,
                secrets=_credentials(self.environment, self.endpoint),
            )
            return publish_runner_failure(
                error,
                lambda: self.builder.fail_runner(
                    failure,
                    attempt_id=self.attempt_id,
                    narrative_result=self.narrative_result,
                ),
            )

    def _attempts(self) -> JsonObject:
        narrative = ModelNarrativeDiagnostician(
            OpenAIChatStructuredInvoker(
                endpoint=self.endpoint,
                artifact_sink=self.builder.narrative_artifact_sink,
                transport=self.builder.observed_transport(
                    self.transport,
                    attempt_id=NARRATIVE_ATTEMPT_ID,
                ),
                environment=self.environment,
                now=self.now,
                monotonic_ns=self.monotonic_ns,
            )
        )
        try:
            narrative_result = narrative.diagnose(self.request)
        except ModelAdapterError as error:
            return self.builder.fail(error, attempt_id=NARRATIVE_ATTEMPT_ID)

        self.narrative_result = narrative_result
        self.attempt_id = STRUCTURED_ATTEMPT_ID
        structured = OpenAIChatDiagnostician(
            endpoint=self.endpoint,
            artifact_sink=self.builder.structured_artifact_sink,
            profile=self.structured_profile,
            transport=self.builder.observed_transport(
                self.transport,
                attempt_id=STRUCTURED_ATTEMPT_ID,
            ),
            environment=self.environment,
            now=self.now,
            monotonic_ns=self.monotonic_ns,
        )
        try:
            structured_result = structured.diagnose(self.request)
        except ModelAdapterError as error:
            return self.builder.fail(
                error,
                attempt_id=STRUCTURED_ATTEMPT_ID,
                narrative_result=narrative_result,
            )

        result = project_comparative_results(
            self.request,
            narrative_result,
            structured_result,
        )
        return self.builder.complete(result)


def _credentials(
    environment: Mapping[str, str],
    endpoint: OpenAIChatEndpoint,
) -> tuple[str, ...]:
    credential = environment.get(endpoint.api_key_env)
    return (credential,) if credential else ()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser(
        "run",
        help="execute one registered paired-comparison replicate",
    )
    run_parser.add_argument("--manifest", required=True)
    run_parser.add_argument("--bundle", required=True)
    run_parser.add_argument("--replicate-index", required=True, type=int)
    run_parser.add_argument("--base-url", required=True)
    run_parser.add_argument("--api-key-env", required=True)

    validate_parser = commands.add_parser(
        "validate",
        help="verify a completed or failed paired-comparison bundle",
    )
    validate_parser.add_argument("--bundle", required=True)
    return parser


def _summary(bundle_path: str, bundle_manifest: JsonObject) -> JsonObject:
    return {
        "bundle": bundle_path,
        "bundle_id": bundle_manifest["bundle_id"],
        "comparison_series_id": bundle_manifest["comparison_series_id"],
        "replicate_index": bundle_manifest["replicate_index"],
        "outcome": bundle_manifest["outcome"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Execute a registered replicate or independently verify a bundle."""

    args = _parser().parse_args(argv)
    command = cast(str, args.command)
    bundle_path = cast(str, args.bundle)
    try:
        if command == "run":
            bundle_manifest = run_cache_key_comparison(
                cast(str, args.manifest),
                bundle_path,
                replicate_index=cast(int, args.replicate_index),
                base_url=cast(str, args.base_url),
                api_key_env=cast(str, args.api_key_env),
            )
        else:
            verified = ComparisonBundleValidator().validate(
                bundle_path,
                _frozen_request(),
            )
            bundle_manifest = verified.bundle_manifest
    except Exception as error:
        print(_error_line(error), file=sys.stderr)
        return 2

    print(json.dumps(_summary(bundle_path, bundle_manifest), indent=2, sort_keys=True))
    return 0 if bundle_manifest["outcome"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
