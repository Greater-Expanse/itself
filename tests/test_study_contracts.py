# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from experiments._study_contracts import (
    RUNNER_ERROR_MESSAGE_LIMIT,
    V1_CONTRACT,
    V2_CONTRACT,
    RunnerFailure,
    UnsupportedStudyContractError,
    declared_contract,
    publish_runner_failure,
    sanitized_error_message,
)
from experiments.cases.cache_key_comparison import build_comparison_request
from experiments.cases.cache_key_comparison_study import (
    STUDY_TARGETS_BY_VERSION,
    ComparisonStudyTarget,
)
from experiments.cases.cache_key_exchange import build_exchange_request
from experiments.cases.cache_key_omission import CaseEnvironment, Mechanism
from experiments.comparison_bundles import (
    ComparisonBundleValidationError,
    ComparisonBundleValidator,
)
from experiments.comparison_manifests import (
    ComparisonManifestValidationError,
    ComparisonManifestValidator,
    build_comparison_manifest,
    endpoint_from_comparison_manifest,
    load_comparison_manifest,
)
from experiments.comparison_manifests import (
    require_running_shipped_adapter as require_running_comparison_adapter,
)
from experiments.diagnostician import DiagnosisRequest
from experiments.model_adapters import (
    OpenAIChatEndpoint,
    ShippedAdapterIdentity,
    build_chat_completion_payload,
)
from experiments.trial_bundles import TrialBundleValidationError, TrialBundleValidator
from experiments.trial_manifests import (
    TrialManifestValidationError,
    TrialManifestValidator,
    build_trial_manifest,
    canonical_json_bytes,
    endpoint_from_trial_manifest,
    load_trial_manifest,
    require_running_shipped_adapter,
)
from itself import JsonObject, JsonValue

ROOT = Path(__file__).resolve().parents[1]
V1_EXAMPLES = ROOT / "experiments" / "contracts" / "v1" / "examples"
V2_EXAMPLES = ROOT / "experiments" / "contracts" / "v2" / "examples"
TOGETHER = "https://api.together.ai/v1"
SHIPPED = ShippedAdapterIdentity(
    adapter_id="openai-compatible-chat-completions",
    adapter_version="0.4.0",
)


def _load(path: Path) -> JsonObject:
    return cast(JsonObject, json.loads(path.read_text(encoding="utf-8")))


def _object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def _comparison_request() -> DiagnosisRequest:
    context = CaseEnvironment(
        Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION
    ).diagnostic_context()
    return build_comparison_request(context)


def _registered_at(manifest: JsonObject) -> datetime:
    value = manifest["registered_at"]
    assert isinstance(value, str)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _trial_example() -> JsonObject:
    return load_trial_manifest(V2_EXAMPLES / "trial-manifest.json")


def _comparison_example() -> JsonObject:
    return load_comparison_manifest(V2_EXAMPLES / "comparison-manifest.json")


def _rebuilt_trial(
    manifest: JsonObject,
    endpoint: OpenAIChatEndpoint,
) -> JsonObject:
    metrics = manifest["metrics"]
    limitations = manifest["limitations"]
    assert isinstance(metrics, list) and isinstance(limitations, list)
    return build_trial_manifest(
        endpoint,
        build_exchange_request(),
        trial_series_id=cast(str, manifest["trial_series_id"]),
        registered_at=_registered_at(manifest),
        study_phase=cast(str, manifest["study_phase"]),
        metrics=[cast(str, metric) for metric in metrics],
        limitations=[cast(str, limitation) for limitation in limitations],
        shipped_adapter=SHIPPED,
    )


def _v1_comparison_manifest(endpoint: OpenAIChatEndpoint) -> JsonObject:
    return build_comparison_manifest(
        endpoint,
        _comparison_request(),
        comparison_series_id="v1-v2-payload-identity",
        registered_at=datetime(2026, 11, 26, tzinfo=UTC),
        replicate_count=1,
        study_phase="engineering_pilot",
        limitations=["A limitation."],
        expected_observation_values=("A", "B"),
    )


# Version dispatch.


def test_declared_version_alone_chooses_the_contract() -> None:
    assert declared_contract({"v": "0.2.0"}, "v") is V1_CONTRACT
    assert declared_contract({"v": "0.3.0"}, "v") is V2_CONTRACT
    # A missing version is reported against v1, whose schemas require it.
    assert declared_contract({}, "v") is V1_CONTRACT
    assert declared_contract([], "v") is V1_CONTRACT
    with pytest.raises(UnsupportedStudyContractError, match="'0.4.0'"):
        declared_contract({"v": "0.4.0"}, "v")


def test_every_study_record_reader_refuses_an_unknown_version() -> None:
    trial = _trial_example()
    trial["manifest_version"] = "9.9.9"
    comparison = _comparison_example()
    comparison["manifest_version"] = "9.9.9"

    with pytest.raises(TrialManifestValidationError, match="unsupported version"):
        TrialManifestValidator().validate(trial)
    with pytest.raises(ComparisonManifestValidationError, match="unsupported version"):
        ComparisonManifestValidator().validate(comparison)
    with pytest.raises(TrialBundleValidationError, match="unsupported version"):
        TrialBundleValidator().validate_manifest({"bundle_version": "9.9.9"})
    with pytest.raises(TrialBundleValidationError, match="unsupported version"):
        TrialBundleValidator().validate_attempt_report(
            {"attempt_report_version": "9.9.9"}
        )
    with pytest.raises(ComparisonBundleValidationError, match="unsupported version"):
        ComparisonBundleValidator().validate_manifest({"bundle_version": "9.9.9"})
    with pytest.raises(ComparisonBundleValidationError, match="unsupported version"):
        ComparisonBundleValidator().validate_attempt_report(
            {"attempt_report_version": "9.9.9"}
        )


def test_a_v2_document_is_not_valid_under_the_v1_schemas() -> None:
    trial = _trial_example()
    trial["manifest_version"] = "0.2.0"
    issues = TrialManifestValidator().errors(trial)

    assert any("shipped_adapter" in issue for issue in issues)
    assert any("deadline_seconds" in issue for issue in issues)


# Examples.


def test_v2_examples_are_valid() -> None:
    trial = _trial_example()
    comparison = _comparison_example()
    TrialManifestValidator().validate(trial)
    ComparisonManifestValidator().validate(comparison)
    TrialBundleValidator().validate_attempt_report(
        _load(V2_EXAMPLES / "trial-attempt-report.json")
    )
    ComparisonBundleValidator().validate_attempt_report_against_manifest(
        _load(V2_EXAMPLES / "comparison-attempt-report.json"),
        comparison,
    )


def test_v2_manifest_examples_are_what_the_builders_produce() -> None:
    trial = _trial_example()
    trial_endpoint = endpoint_from_trial_manifest(
        trial,
        base_url=TOGETHER,
        api_key_env="TOGETHER_API_KEY",
    )
    assert canonical_json_bytes(_rebuilt_trial(trial, trial_endpoint)) == (
        canonical_json_bytes(trial)
    )

    comparison = _comparison_example()
    comparison_endpoint = endpoint_from_comparison_manifest(
        comparison,
        base_url=TOGETHER,
        api_key_env="TOGETHER_API_KEY",
    )
    design = _object(comparison["design"])
    limitations = comparison["limitations"]
    assert isinstance(limitations, list)
    rebuilt = build_comparison_manifest(
        comparison_endpoint,
        _comparison_request(),
        comparison_series_id=cast(str, comparison["comparison_series_id"]),
        registered_at=_registered_at(comparison),
        replicate_count=cast(int, design["replicate_count"]),
        study_phase=cast(str, comparison["study_phase"]),
        limitations=[cast(str, limitation) for limitation in limitations],
        expected_observation_values=("A", "B"),
        contract=V2_CONTRACT,
        shipped_adapter=SHIPPED,
    )
    assert canonical_json_bytes(rebuilt) == canonical_json_bytes(comparison)


# Item 5 and the payload rule: the same settings send the same bytes.


def test_v1_and_v2_trials_render_the_same_payload_for_the_same_settings() -> None:
    v1 = load_trial_manifest(V1_EXAMPLES / "trial-manifest.json")
    v1_endpoint = endpoint_from_trial_manifest(
        v1,
        base_url=TOGETHER,
        api_key_env="K",
    )
    v2 = _rebuilt_trial(v1, replace(v1_endpoint, deadline_seconds=300.0))
    v2_endpoint = endpoint_from_trial_manifest(v2, base_url=TOGETHER, api_key_env="K")
    request = build_exchange_request()

    assert v2["manifest_version"] == "0.3.0"
    assert canonical_json_bytes(
        build_chat_completion_payload(v2_endpoint, request)
    ) == canonical_json_bytes(build_chat_completion_payload(v1_endpoint, request))
    assert (
        _object(v2["generation"])["prompt_payload_sha256"]
        == (_object(v1["generation"])["prompt_payload_sha256"])
    )


def test_a_v2_usage_request_adds_only_stream_options_to_the_payload() -> None:
    request = _comparison_request()
    streamed = OpenAIChatEndpoint(
        actor_id="identity",
        base_url=TOGETHER,
        model="openai/gpt-oss-20b",
        api_key_env="K",
        stream=True,
        extra_body={"temperature": 0.0, "seed": 7},
    )
    v1 = _v1_comparison_manifest(streamed)
    v1_endpoint = endpoint_from_comparison_manifest(
        v1,
        base_url=TOGETHER,
        api_key_env="K",
    )
    v2 = _comparison_example()
    v2_endpoint = replace(
        endpoint_from_comparison_manifest(v2, base_url=TOGETHER, api_key_env="K"),
        actor_id="identity",
        extra_body={
            "temperature": 0.0,
            "seed": 7,
            "stream_options": {"include_usage": True},
        },
    )
    v1_payload = build_chat_completion_payload(v1_endpoint, request)
    v2_payload = build_chat_completion_payload(v2_endpoint, request)

    assert v2_payload.pop("stream_options") == {"include_usage": True}
    assert canonical_json_bytes(v2_payload) == canonical_json_bytes(v1_payload)


def test_the_stream_usage_request_is_bound_from_the_manifest() -> None:
    trial = _trial_example()
    endpoint = endpoint_from_trial_manifest(trial, base_url=TOGETHER, api_key_env="K")
    TrialManifestValidator().validate_against(trial, endpoint, build_exchange_request())
    assert endpoint.stream is True
    assert endpoint.extra_body["stream_options"] == {"include_usage": True}

    without_usage = replace(endpoint, extra_body={"temperature": 0.0, "seed": 20261126})
    with pytest.raises(TrialManifestValidationError) as raised:
        TrialManifestValidator().validate_against(
            trial,
            without_usage,
            build_exchange_request(),
        )
    assert "generation.stream_include_usage does not match" in str(raised.value)
    assert "generation.prompt_payload_sha256 does not match" in str(raised.value)

    comparison = _comparison_example()
    comparison_endpoint = endpoint_from_comparison_manifest(
        comparison,
        base_url=TOGETHER,
        api_key_env="K",
    )
    assert comparison_endpoint.extra_body["stream_options"] == {"include_usage": True}


def test_a_v2_trial_binds_its_stream_setting() -> None:
    trial = _trial_example()
    endpoint = endpoint_from_trial_manifest(trial, base_url=TOGETHER, api_key_env="K")

    with pytest.raises(TrialManifestValidationError, match="generation.stream does"):
        TrialManifestValidator().validate_against(
            trial,
            replace(endpoint, stream=False),
            build_exchange_request(),
        )


def test_the_usage_request_is_part_of_the_bound_prompt_digest() -> None:
    trial = _trial_example()
    endpoint = endpoint_from_trial_manifest(trial, base_url=TOGETHER, api_key_env="K")
    without_usage = _rebuilt_trial(
        trial,
        replace(endpoint, extra_body={"temperature": 0.0, "seed": 20261126}),
    )

    assert _object(without_usage["generation"])["stream_include_usage"] is False
    assert (
        _object(without_usage["generation"])["prompt_payload_sha256"]
        != (_object(trial["generation"])["prompt_payload_sha256"])
    )


def test_a_malformed_stream_options_value_is_refused() -> None:
    trial = _trial_example()
    endpoint = endpoint_from_trial_manifest(trial, base_url=TOGETHER, api_key_env="K")
    malformed = replace(
        endpoint,
        extra_body={
            "temperature": 0.0,
            "seed": 20261126,
            "stream_options": {"include_usage": False},
        },
    )

    with pytest.raises(TrialManifestValidationError, match="stream_options must be"):
        TrialManifestValidator().validate_against(
            trial,
            malformed,
            build_exchange_request(),
        )


def test_v1_trial_manifests_still_refuse_a_stream_usage_option() -> None:
    v1 = load_trial_manifest(V1_EXAMPLES / "trial-manifest.json")
    endpoint = endpoint_from_trial_manifest(v1, base_url=TOGETHER, api_key_env="K")
    with_usage = replace(
        endpoint,
        extra_body={**endpoint.extra_body, "stream_options": {"include_usage": True}},
    )

    with pytest.raises(TrialManifestValidationError, match="unregistered options"):
        TrialManifestValidator().validate_against(
            v1,
            with_usage,
            build_exchange_request(),
        )


@pytest.mark.parametrize("load", [_trial_example, _comparison_example])
def test_only_a_streamed_manifest_may_ask_for_stream_usage(
    load: Callable[[], JsonObject],
) -> None:
    manifest = load()
    generation = _object(manifest["generation"])
    generation["stream"] = False
    validator = (
        TrialManifestValidator()
        if "trial_series_id" in manifest
        else ComparisonManifestValidator()
    )

    assert any("stream_include_usage" in issue for issue in validator.errors(manifest))
    generation["stream_include_usage"] = False
    assert validator.errors(manifest) == []


# Item 2: the request deadline.


def test_the_deadline_is_declared_bound_and_checked() -> None:
    trial = _trial_example()
    endpoint = endpoint_from_trial_manifest(trial, base_url=TOGETHER, api_key_env="K")
    request = build_exchange_request()

    assert endpoint.deadline_seconds == 300.0
    assert endpoint.as_compatible_endpoint().deadline_seconds == 300.0
    with pytest.raises(
        TrialManifestValidationError, match="execution.deadline_seconds"
    ):
        TrialManifestValidator().validate_against(
            trial,
            replace(endpoint, deadline_seconds=None),
            request,
        )

    comparison = _comparison_example()
    comparison_endpoint = endpoint_from_comparison_manifest(
        comparison,
        base_url=TOGETHER,
        api_key_env="K",
    )
    assert comparison_endpoint.deadline_seconds == 300.0
    with pytest.raises(
        ComparisonManifestValidationError,
        match="execution.deadline_seconds",
    ):
        ComparisonManifestValidator().validate_against(
            comparison,
            replace(comparison_endpoint, deadline_seconds=60.0),
            _comparison_request(),
        )


@pytest.mark.parametrize(
    ("deadline", "valid"),
    [(None, True), (0.5, True), (0, False), (-1.0, False), ("300", False)],
)
def test_the_deadline_is_positive_or_null(deadline: JsonValue, valid: bool) -> None:
    trial = _trial_example()
    comparison = _comparison_example()
    _object(trial["execution"])["deadline_seconds"] = deadline
    _object(comparison["execution"])["deadline_seconds"] = deadline

    assert (TrialManifestValidator().errors(trial) == []) is valid
    assert (ComparisonManifestValidator().errors(comparison) == []) is valid


def test_a_v2_manifest_must_declare_its_deadline() -> None:
    trial = _trial_example()
    del _object(trial["execution"])["deadline_seconds"]

    assert any(
        "deadline_seconds" in issue for issue in TrialManifestValidator().errors(trial)
    )


def test_a_v1_binding_refuses_any_deadline() -> None:
    v1 = load_trial_manifest(V1_EXAMPLES / "trial-manifest.json")
    endpoint = endpoint_from_trial_manifest(v1, base_url=TOGETHER, api_key_env="K")
    assert endpoint.deadline_seconds is None
    with pytest.raises(
        TrialManifestValidationError, match="deadline_seconds must be None"
    ):
        TrialManifestValidator().validate_against(
            v1,
            replace(endpoint, deadline_seconds=300.0),
            build_exchange_request(),
        )

    v1_comparison = load_comparison_manifest(V1_EXAMPLES / "comparison-manifest.json")
    comparison_endpoint = endpoint_from_comparison_manifest(
        v1_comparison,
        base_url=TOGETHER,
        api_key_env="K",
    )
    assert comparison_endpoint.deadline_seconds is None
    with pytest.raises(
        ComparisonManifestValidationError,
        match="deadline_seconds must be None",
    ):
        ComparisonManifestValidator().validate_against(
            v1_comparison,
            replace(comparison_endpoint, deadline_seconds=300.0),
            _comparison_request(),
        )


# Item 3: the shipped adapter pin.


@pytest.mark.parametrize("load", [_trial_example, _comparison_example])
def test_a_v2_manifest_pins_the_shipped_client(
    load: Callable[[], JsonObject],
) -> None:
    manifest = load()
    validator = (
        TrialManifestValidator()
        if "trial_series_id" in manifest
        else ComparisonManifestValidator()
    )
    diagnostician = _object(manifest["diagnostician"])
    _object(diagnostician["shipped_adapter"])["adapter_id"] = "another-client"
    assert any("shipped_adapter" in issue for issue in validator.errors(manifest))

    del diagnostician["shipped_adapter"]
    assert any("shipped_adapter" in issue for issue in validator.errors(manifest))


def test_only_a_v2_manifest_can_be_checked_against_the_running_client() -> None:
    v1 = load_trial_manifest(V1_EXAMPLES / "trial-manifest.json")
    with pytest.raises(TrialManifestValidationError, match="only a v0.3.0"):
        require_running_shipped_adapter(v1)
    v1_comparison = load_comparison_manifest(V1_EXAMPLES / "comparison-manifest.json")
    with pytest.raises(ComparisonManifestValidationError, match="only a v0.3.0"):
        require_running_comparison_adapter(v1_comparison)


def test_a_v1_comparison_manifest_cannot_pin_a_shipped_client() -> None:
    endpoint = endpoint_from_comparison_manifest(
        _comparison_example(),
        base_url=TOGETHER,
        api_key_env="K",
    )
    with pytest.raises(ValueError, match="pins no shipped client"):
        build_comparison_manifest(
            endpoint,
            _comparison_request(),
            comparison_series_id="refused",
            registered_at=datetime(2026, 11, 26, tzinfo=UTC),
            replicate_count=1,
            study_phase="engineering_pilot",
            limitations=["A limitation."],
            shipped_adapter=SHIPPED,
        )


def test_study_targets_keep_v1_settings_out_of_v1_and_pin_v2() -> None:
    target = STUDY_TARGETS_BY_VERSION["5"][0]
    assert target.contract is V1_CONTRACT
    assert target.endpoint().deadline_seconds is None
    assert "stream_options" not in target.endpoint().extra_body

    with pytest.raises(ValueError, match="no shipped client"):
        replace(target, deadline_seconds=300.0)
    with pytest.raises(ValueError, match="must pin"):
        replace(target, contract=V2_CONTRACT)
    with pytest.raises(ValueError, match="only a streamed target"):
        replace(
            target,
            contract=V2_CONTRACT,
            shipped_adapter=SHIPPED,
            stream=False,
            stream_include_usage=True,
        )
    v2 = replace(
        target,
        contract=V2_CONTRACT,
        shipped_adapter=SHIPPED,
        stream=True,
        stream_include_usage=True,
        deadline_seconds=300.0,
    )
    assert isinstance(v2, ComparisonStudyTarget)
    assert v2.endpoint().deadline_seconds == 300.0
    assert v2.endpoint().extra_body["stream_options"] == {"include_usage": True}


# Items 1 and 4: what an attempt report records.


def _trial_report() -> JsonObject:
    return _load(V2_EXAMPLES / "trial-attempt-report.json")


def _comparison_report() -> JsonObject:
    return _load(V2_EXAMPLES / "comparison-attempt-report.json")


@pytest.mark.parametrize("status", [999, 42, 200, None])
def test_a_v2_report_keeps_any_observed_status(status: JsonValue) -> None:
    report = _comparison_report()
    _object(report["failure"])["status_code"] = status
    ComparisonBundleValidator().validate_attempt_report(report)

    runner = _trial_report()
    _object(runner["failure"])["status_code"] = status
    TrialBundleValidator().validate_attempt_report(runner)


def test_a_v1_report_still_bounds_its_status() -> None:
    report = _comparison_report()
    report["attempt_report_version"] = "0.2.0"
    del report["shipped_adapter"]
    _object(report["failure"])["status_code"] = 999

    with pytest.raises(ComparisonBundleValidationError, match="999"):
        ComparisonBundleValidator().validate_attempt_report(report)


def test_a_runner_failure_report_has_its_own_shape() -> None:
    validator = TrialBundleValidator()
    report = _trial_report()
    _object(report["failure"])["retryable"] = True
    with pytest.raises(TrialBundleValidationError):
        validator.validate_attempt_report(report)

    report = _trial_report()
    del _object(report["failure"])["error"]
    with pytest.raises(TrialBundleValidationError, match="error"):
        validator.validate_attempt_report(report)

    report = _trial_report()
    report["outcome"] = "adapter_failed"
    with pytest.raises(TrialBundleValidationError):
        validator.validate_attempt_report(report)

    report = _comparison_report()
    report["outcome"] = "runner_failed"
    with pytest.raises(ComparisonBundleValidationError):
        ComparisonBundleValidator().validate_attempt_report(report)


@pytest.mark.parametrize(
    "error",
    [
        {"type": "OSError", "message": "two\nlines"},
        {"type": "OSError", "message": ""},
        {"type": "OSError", "message": "x" * (RUNNER_ERROR_MESSAGE_LIMIT + 1)},
        {"type": "not a name", "message": "message"},
        {"type": "OSError"},
    ],
)
def test_a_runner_error_is_one_bounded_line(error: JsonValue) -> None:
    report = _trial_report()
    _object(report["failure"])["error"] = error

    with pytest.raises(TrialBundleValidationError):
        TrialBundleValidator().validate_attempt_report(report)


def test_a_v2_report_records_the_shipped_client() -> None:
    report = _trial_report()
    del report["shipped_adapter"]
    with pytest.raises(TrialBundleValidationError, match="shipped_adapter"):
        TrialBundleValidator().validate_attempt_report(report)


def test_a_comparison_report_must_name_the_pinned_shipped_client() -> None:
    manifest = _comparison_example()
    report = _comparison_report()
    _object(report["shipped_adapter"])["adapter_version"] = "0.5.0"

    with pytest.raises(ComparisonBundleValidationError, match="shipped_adapter"):
        ComparisonBundleValidator().validate_attempt_report_against_manifest(
            report,
            manifest,
        )


def test_a_report_must_declare_its_manifest_version() -> None:
    v1_manifest = load_comparison_manifest(V1_EXAMPLES / "comparison-manifest.json")
    report = _comparison_report()
    report["comparison_series_id"] = v1_manifest["comparison_series_id"]

    with pytest.raises(ComparisonBundleValidationError, match="version"):
        ComparisonBundleValidator().validate_attempt_report_against_manifest(
            report,
            v1_manifest,
        )


# Item 4: sanitizing and publishing a runner failure.


def test_a_runner_error_message_is_redacted_and_one_line() -> None:
    secret = "sk-secret-value"
    error = RuntimeError(f"first {secret}\n\n  second\x07line")
    message = sanitized_error_message(error, secrets=(secret, ""))

    assert secret not in message
    assert message == "first [redacted]; second line"
    assert sanitized_error_message(ValueError()) == "ValueError"
    long_message = sanitized_error_message(ValueError("y" * 900))
    assert len(long_message) == RUNNER_ERROR_MESSAGE_LIMIT
    assert long_message.endswith("...")


def test_a_secret_is_redacted_before_the_message_is_truncated() -> None:
    secret = "s" * 40
    error = RuntimeError("x" * (RUNNER_ERROR_MESSAGE_LIMIT - 10) + secret)
    message = sanitized_error_message(error, secrets=(secret,))

    assert "s" * 10 not in message


def test_an_interrupt_is_recorded_as_interrupted() -> None:
    interrupt = RunnerFailure.from_exception(KeyboardInterrupt())
    error = RunnerFailure.from_exception(OSError(28, "No space left on device"))

    assert interrupt.kind == "interrupted"
    assert interrupt.error_json_object() == {
        "type": "KeyboardInterrupt",
        "message": "KeyboardInterrupt",
    }
    assert error.kind == "runner_error"
    assert error.error_json_object() == {
        "type": "OSError",
        "message": "[Errno 28] No space left on device",
    }


def test_a_runner_error_returns_its_published_bundle() -> None:
    published: JsonObject = {"outcome": "runner_failed"}

    assert publish_runner_failure(OSError("disk"), lambda: published) is published


def test_an_interrupt_is_raised_again_after_its_bundle_is_published() -> None:
    calls: list[str] = []
    interrupt = KeyboardInterrupt()

    def publish() -> JsonObject:
        calls.append("published")
        return {}

    with pytest.raises(KeyboardInterrupt) as raised:
        publish_runner_failure(interrupt, publish)
    assert raised.value is interrupt
    assert calls == ["published"]


def test_an_interrupt_still_propagates_when_publishing_fails() -> None:
    def publish() -> JsonObject:
        raise OSError("read-only file system")

    with pytest.raises(KeyboardInterrupt) as raised:
        publish_runner_failure(KeyboardInterrupt(), publish)
    assert raised.value.__notes__ == [
        "the interrupted attempt could not be published: read-only file system"
    ]

    with pytest.raises(OSError, match="read-only"):
        publish_runner_failure(RuntimeError("runner"), publish)
