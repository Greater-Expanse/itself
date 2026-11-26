# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

# pyright: reportPrivateUsage=false

"""Run every registered v6 and v7 slot over fake transports, then report each."""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import experiments.cases.cache_key_comparison_report as report_module
import experiments.cases.cache_key_comparison_run as comparison_run
import itself.inference as shipped_inference
from experiments.cases.cache_key_comparison import (
    NARRATIVE_ATTEMPT_ID,
    STRUCTURED_ATTEMPT_ID,
    ComparativeRunResult,
)
from experiments.cases.cache_key_comparison_report import (
    build_comparison_report,
    render_markdown,
)
from experiments.cases.cache_key_comparison_run import run_cache_key_comparison
from experiments.cases.cache_key_comparison_study import (
    STUDY_EXECUTION_SLOTS_BY_VERSION,
    STUDY_TARGETS_BY_VERSION,
    TOGETHER_ENDPOINT,
    V6_SHIPPED_ADAPTER,
    ComparisonStudyTarget,
    write_study_manifests,
)
from experiments.cases.cache_key_diagnostician import (
    CacheKeyFixtureDiagnostician,
    CacheKeyNarrativeFixtureDiagnostician,
)
from experiments.diagnostician import DiagnosisRequest, DiagnosisResult
from experiments.model_adapters import HttpRequest, HttpResponse
from experiments.narrative_diagnostician import NarrativeResult
from itself import InferenceError, InferenceFailure, JsonObject, JsonValue

SECRET = "study-secret-that-must-not-leak"
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
# V7 repeats v6's targets in v6's order, so one script serves both versions.
SLOTS = STUDY_EXECUTION_SLOTS_BY_VERSION["6"]
# The slots that stop, and how: two provider failures, one response that
# reports no token usage, one test plan that repeats a retained identifier,
# and one failure of the harness after both responses.
RATE_LIMITED = ("together-glm-5p2", 2)
PAST_DEADLINE = ("together-glm-5p2", 5)
WITHOUT_USAGE = ("together-minimax-m3p1-flash", 4)
REPEATED_RETAINED_ID = ("together-minimax-m3p1-flash", 2)
HARNESS_FAILURE = ("together-qwen3p7-plus", 3)
# V6's test-plan schema states uniqueItems, so the shipped client refuses the
# repeated identifier; v7's does not, so the diagnosis contract refuses it.
REPEATED_RETAINED_ID_KIND = {"6": "response_schema", "7": "assertion_contract"}
TEST_PLAN_SCHEMA_NAME = {
    "6": "itself_diagnosis_assertion_v0_3_0",
    "7": "itself_diagnosis_assertion_v0_4_0",
}
TEST_PLAN_PROFILE_ID = {
    "6": "diagnosis-assertion-v0.3.0",
    "7": "diagnosis-assertion-v0.4.0",
}


def _targets(study_version: str) -> dict[str, ComparisonStudyTarget]:
    return {
        target.target_id: target for target in STUDY_TARGETS_BY_VERSION[study_version]
    }


def _requests() -> list[HttpRequest]:
    return []


@dataclass(slots=True)
class ScriptedTransport:
    """Answer each request with the next scripted response or failure."""

    responses: list[HttpResponse | InferenceError]
    requests: list[HttpRequest] = field(default_factory=_requests)

    def send(self, request: HttpRequest, /) -> HttpResponse:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, InferenceError):
            raise response
        return response


def _streamed(
    target: ComparisonStudyTarget,
    content: JsonObject,
    *,
    input_tokens: int,
    usage: bool = True,
) -> HttpResponse:
    chunk: JsonObject = {"id": "v6-comparison", "model": target.model_id}
    events: list[JsonObject] = [
        {
            **chunk,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": None,
                    "delta": {"role": "assistant", "content": json.dumps(content)},
                }
            ],
            "usage": None,
        },
        {
            **chunk,
            "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}],
            "usage": None,
        },
    ]
    if usage:
        events.append(
            {
                **chunk,
                "choices": [],
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": 426,
                    "total_tokens": input_tokens + 426,
                },
            }
        )
    body = b"".join(
        b"data: " + json.dumps(event).encode() + b"\n\n" for event in events
    )
    return HttpResponse(
        200,
        {"Content-Type": "text/event-stream"},
        body + b"data: [DONE]\n\n",
    )


def _request() -> DiagnosisRequest:
    return comparison_run._frozen_request()


def _script(
    target: ComparisonStudyTarget,
    replicate_index: int,
) -> list[HttpResponse | InferenceError]:
    slot = (target.target_id, replicate_index)
    if slot == RATE_LIMITED:
        return [HttpResponse(429, {}, b'{"error":"slow down"}')]
    if slot == PAST_DEADLINE:
        return [
            InferenceError(
                InferenceFailure.TRANSPORT,
                "inference endpoint exceeded the request deadline",
            )
        ]
    narrative = CacheKeyNarrativeFixtureDiagnostician().diagnose(_request())
    structured = CacheKeyFixtureDiagnostician().diagnose(_request())
    test_plan = structured.assertion.to_model_json_object()
    if slot == REPEATED_RETAINED_ID:
        retained = cast(list[JsonValue], test_plan["retained_hypothesis_ids"])
        retained.append(retained[0])
    return [
        _streamed(target, narrative.assertion.to_json_object(), input_tokens=157),
        _streamed(
            target,
            test_plan,
            input_tokens=200,
            usage=slot != WITHOUT_USAGE,
        ),
    ]


def _fail_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(
        request: DiagnosisRequest,
        narrative: NarrativeResult,
        structured: DiagnosisResult,
    ) -> ComparativeRunResult:
        del request, narrative, structured
        raise RuntimeError(f"projection failed with {SECRET}")

    monkeypatch.setattr(comparison_run, "project_comparative_results", failing)


@dataclass(frozen=True, slots=True)
class StudyRun:
    study_version: str
    report: JsonObject
    requests: tuple[tuple[str, int, HttpRequest], ...]
    bundles: Path


# One run of all fifteen slots of each version serves every test in this module.
@pytest.fixture(scope="module", params=["6", "7"])
def study_run(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> StudyRun:
    study_version = cast(str, request.param)
    with pytest.MonkeyPatch.context() as monkeypatch:
        return _run_study(
            study_version,
            tmp_path_factory.mktemp(f"v{study_version}-study"),
            monkeypatch,
        )


def _run_study(
    study_version: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> StudyRun:
    manifests = tmp_path / "manifests"
    bundles = tmp_path / "bundles"
    targets = _targets(study_version)
    assert STUDY_EXECUTION_SLOTS_BY_VERSION[study_version] == SLOTS
    write_study_manifests(manifests, registered_at=NOW, study_version=study_version)
    # The run executes under the client its manifests pin.
    monkeypatch.setattr(
        shipped_inference,
        "ADAPTER_VERSION",
        V6_SHIPPED_ADAPTER.adapter_version,
    )
    clock = itertools.count(1_000_000, 1_000_000)
    sent: list[tuple[str, int, HttpRequest]] = []
    for target_id, replicate_index in SLOTS:
        target = targets[target_id]
        transport = ScriptedTransport(_script(target, replicate_index))
        with monkeypatch.context() as patch:
            if (target_id, replicate_index) == HARNESS_FAILURE:
                _fail_projection(patch)
            run_cache_key_comparison(
                manifests / target.manifest_filename,
                bundles / target_id / f"replicate-{replicate_index:03d}",
                replicate_index=replicate_index,
                base_url=target.base_url,
                api_key_env=target.api_key_env,
                transport=transport,
                environment={target.api_key_env: SECRET},
                now=lambda: NOW,
                monotonic_ns=clock.__next__,
            )
        assert transport.responses == []
        sent.extend(
            (target_id, replicate_index, request) for request in transport.requests
        )
    report = build_comparison_report(
        bundles,
        study_version=study_version,
        reported_at=NOW,
    )
    return StudyRun(
        study_version=study_version,
        report=report,
        requests=tuple(sent),
        bundles=bundles,
    )


def test_the_runner_sends_every_slot_round_robin_to_the_one_endpoint(
    study_run: StudyRun,
) -> None:
    first_requests = list(
        dict.fromkeys((target_id, index) for target_id, index, _ in study_run.requests)
    )

    assert first_requests == list(SLOTS)
    assert first_requests[:4] == [
        ("together-glm-5p2", 1),
        ("together-minimax-m3p1-flash", 1),
        ("together-qwen3p7-plus", 1),
        ("together-glm-5p2", 2),
    ]
    assert len(study_run.requests) == 2 * len(SLOTS) - 2
    targets = _targets(study_run.study_version)
    for target_id, _, request in study_run.requests:
        payload = cast(JsonObject, json.loads(request.body))
        assert request.url.startswith(TOGETHER_ENDPOINT.base_url + "/")
        assert request.deadline_seconds == 600.0
        assert request.timeout_seconds == 300.0
        assert payload["model"] == targets[target_id].model_id
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        assert payload["temperature"] == 1.0
        assert payload["top_p"] == 0.95


def _sent_schema(request: HttpRequest) -> JsonObject:
    payload = cast(JsonObject, json.loads(request.body))
    response_format = cast(JsonObject, payload["response_format"])
    return cast(JsonObject, response_format["json_schema"])


def test_every_test_plan_request_sends_the_version_s_profile(
    study_run: StudyRun,
) -> None:
    requests_by_slot: dict[tuple[str, int], list[HttpRequest]] = {}
    for target_id, replicate_index, request in study_run.requests:
        requests_by_slot.setdefault((target_id, replicate_index), []).append(request)
    test_plan_schemas = [
        _sent_schema(requests[1])
        for requests in requests_by_slot.values()
        if len(requests) == 2
    ]
    retained_ids = [
        cast(
            JsonObject,
            cast(JsonObject, cast(JsonObject, schema["schema"])["properties"])[
                "retained_hypothesis_ids"
            ],
        )
        for schema in test_plan_schemas
    ]

    assert len(test_plan_schemas) == len(SLOTS) - 2
    assert {cast(str, schema["name"]) for schema in test_plan_schemas} == {
        TEST_PLAN_SCHEMA_NAME[study_run.study_version]
    }
    assert [retained.get("uniqueItems") for retained in retained_ids] == [
        None if study_run.study_version == "7" else True
    ] * len(retained_ids)


def test_a_refused_test_plan_is_kept_as_a_failed_bundle(study_run: StudyRun) -> None:
    target_id, replicate_index = REPEATED_RETAINED_ID
    replicate = study_run.bundles / target_id / f"replicate-{replicate_index:03d}"
    attempt_report = cast(
        JsonObject,
        json.loads((replicate / "attempt-report.json").read_text(encoding="utf-8")),
    )
    failure = cast(JsonObject, attempt_report["failure"])

    assert attempt_report["attempt_report_version"] == "0.3.0"
    assert attempt_report["attempt_id"] == STRUCTURED_ATTEMPT_ID
    assert attempt_report["profile_id"] == TEST_PLAN_PROFILE_ID[study_run.study_version]
    assert failure["kind"] == REPEATED_RETAINED_ID_KIND[study_run.study_version]


def test_the_report_counts_each_kind_of_stopped_run(study_run: StudyRun) -> None:
    report = study_run.report
    totals = cast(JsonObject, report["totals"])
    summaries = {
        cast(str, summary["target_id"]): summary
        for summary in cast(list[JsonObject], report["target_summaries"])
    }
    outcomes = {
        (cast(str, replicate["target_id"]), cast(int, replicate["replicate_index"])): (
            replicate["outcome"]
        )
        for replicate in cast(list[JsonObject], report["replicates"])
    }

    assert report["study_version"] == study_run.study_version
    assert report["report_version"] == "0.4.0"
    assert totals["registered_replicates"] == 15
    assert totals["completed_replicates"] == 10
    assert totals["failed_replicates"] == 4
    assert totals["runner_failed_replicates"] == 1
    assert sorted(
        (cast(str, failure["attempt_id"]), cast(str, failure["kind"]))
        for failure in cast(list[JsonObject], totals["failures"])
    ) == sorted(
        [
            (NARRATIVE_ATTEMPT_ID, "http_rate_limit"),
            (NARRATIVE_ATTEMPT_ID, "transport"),
            (STRUCTURED_ATTEMPT_ID, "response_usage"),
            (
                STRUCTURED_ATTEMPT_ID,
                REPEATED_RETAINED_ID_KIND[study_run.study_version],
            ),
        ]
    )
    assert totals["runner_failures"] == [
        {"attempt_id": STRUCTURED_ATTEMPT_ID, "kind": "runner_error", "count": 1}
    ]
    assert {
        target_id: summary["completed_replicates"]
        for target_id, summary in summaries.items()
    } == {
        "together-glm-5p2": 3,
        "together-minimax-m3p1-flash": 3,
        "together-qwen3p7-plus": 4,
    }
    assert outcomes[HARNESS_FAILURE] == "runner_failed"
    assert [
        outcomes[slot]
        for slot in (RATE_LIMITED, PAST_DEADLINE, WITHOUT_USAGE, REPEATED_RETAINED_ID)
    ] == ["adapter_failed"] * 4
    assert SECRET not in json.dumps(report)
    assert "projection failed" not in json.dumps(report)


def test_the_report_carries_its_design(study_run: StudyRun) -> None:
    design = cast(JsonObject, study_run.report["design"])
    provider_control = cast(JsonObject, design["provider_control"])

    assert design["deadline_seconds"] == 600.0
    assert design["stream_include_usage"] is True
    assert design["shipped_adapter"] == {
        "adapter_id": "openai-compatible-chat-completions",
        "adapter_version": "0.4.0",
    }
    assert provider_control["provider_count"] == 1
    assert provider_control["providers"] == ["Together AI"]


# What each version's report says that the other's does not.
CONTROLLED_STUDY_TEXT = {
    "6": (
        "V6 fixes one provider and one compatible base endpoint across all three "
        "model targets. Calls are executed round-robin by replicate index to "
        "distribute temporal serving variation. Provider identity is not a "
        "behavioral metric, and no model output or scorecard from an earlier "
        "version is reused. V6 differs from v5 in provider, in one model, and in "
        "study contract, so the two results are not pooled or compared."
    ),
    "7": (
        "V7 fixes one provider and one compatible base endpoint across all three "
        "model targets. Calls are executed round-robin by replicate index to "
        "distribute temporal serving variation. Provider identity is not a "
        "behavioral metric, and no model output or scorecard from an earlier "
        "version is reused. V7 repeats v6's design with a test-plan output "
        "profile whose schema omits the uniqueItems keyword, so v5, v6, and v7 "
        "results are not pooled or compared."
    ),
}
REPEATED_RETAINED_ID_LABEL = {
    "6": (
        "1 test-plan request: response did not match the required JSON shape "
        "(`response_schema`)"
    ),
    "7": (
        "1 test-plan request: response violated the requested task rules "
        "(`assertion_contract`)"
    ),
}
VERSION_LIMITATIONS = {
    "6": [
        "V6 fixes one provider as controlled infrastructure; provider identity "
        "is provenance only.",
        "V6 differs from v5 in provider, in one model, and in study contract; "
        "the two are not pooled or compared.",
        "No model output or scorecard from an earlier version is reused in v6.",
        "Harness failures after a request was sent are counted apart from "
        "provider and model-output failures; they are not scored as wrong "
        "diagnoses.",
    ],
    "7": [
        "V7 fixes one provider as controlled infrastructure; provider identity "
        "is provenance only.",
        "V7 repeats v6's design with a different test-plan output profile; v5, "
        "v6, and v7 are not pooled or compared.",
        "V7's test-plan schema omits the uniqueItems keyword; its prompt states "
        "the requirement, and a plan that repeats an identifier is refused as an "
        "assertion_contract failure.",
        "No model output or scorecard from an earlier version is reused in v7.",
        "Harness failures after a request was sent are counted apart from "
        "provider and model-output failures; they are not scored as wrong "
        "diagnoses.",
    ],
}


def test_the_markdown_states_its_design_and_failures(study_run: StudyRun) -> None:
    version = study_run.study_version
    markdown = render_markdown(study_run.report)

    assert markdown == render_markdown(
        cast(JsonObject, json.loads(json.dumps(study_run.report)))
    )
    assert markdown.startswith(f"# Case 001 comparison study v{version}\n\n## What")
    assert "Status: historical study version" not in markdown
    assert (
        "We scheduled that two-call, three-path procedure 15 times—5 times for "
        "each of 3 models."
    ) in markdown
    assert (
        "Of those planned runs, 10 completed both model calls and all three "
        "paths. Of the other 5, 4 stopped on an explicit provider or "
        "model-output failure and 1 on a failure of the harness itself after a "
        "request was sent."
    ) in markdown
    assert "All model calls used Together AI to minimize provider variability." in (
        markdown
    )
    for label in (
        "1 ordinary-answer request: endpoint rejected the request because its "
        "rate limit was reached (`http_rate_limit`)",
        "1 ordinary-answer request: request failed in transport, timed out, or "
        "passed its deadline (`transport`)",
        "1 test-plan request: response did not report valid token usage "
        "(`response_usage`)",
        REPEATED_RETAINED_ID_LABEL[version],
        "harness: 1 test-plan request: harness failed after the request was "
        "sent (`runner_error`)",
    ):
        assert label in markdown
    assert (
        "## How the experiment was controlled\n\n"
        f"V{version} binds `expected_observation` to the exact revision codes "
        "`A` and `B` in every manifest, output schema, and rendered payload. The "
        "deterministic scorer therefore compares like-for-like categorical "
        "values without a text parser or post-hoc reinterpretation.\n\n"
        f"{CONTROLLED_STUDY_TEXT[version]}\n\n"
        f"V{version} ran under study contract v2: every request had a 600-second "
        "deadline, every stream asked the provider to report its token usage, "
        "and every manifest pinned Itself's shipped client, "
        "`openai-compatible-chat-completions` `0.4.0`. A failure of the harness "
        "after a request was sent is counted apart from provider and "
        "model-output failures and is not scored as a wrong diagnosis.\n\n"
        "## Models and weight availability"
    ) in markdown
    for row in (
        "| together-glm-5p2 | `zai-org/GLM-5.2` |",
        "| together-minimax-m3p1-flash | `MiniMaxAI/MiniMax-M3.1-Flash` |",
        "| together-qwen3p7-plus | `Qwen/Qwen3.7-Plus` |",
    ):
        assert row in markdown


def test_the_report_states_its_limitations(study_run: StudyRun) -> None:
    limitations = cast(list[str], study_run.report["limitations"])
    other_version = {"6": "7", "7": "6"}[study_run.study_version]

    for limitation in (
        *VERSION_LIMITATIONS[study_run.study_version],
        "Qwen 3.7 Plus is a hosted-only comparator, not an open-weight result.",
    ):
        assert limitation in limitations
    for limitation in VERSION_LIMITATIONS[other_version]:
        if limitation not in VERSION_LIMITATIONS[study_run.study_version]:
            assert limitation not in limitations
    assert not any("v4" in limitation for limitation in limitations)


def test_the_v2_design_paragraph_states_what_the_design_records() -> None:
    text = report_module._v2_design_text(
        "7",
        {
            "deadline_seconds": None,
            "stream_include_usage": False,
            "shipped_adapter": {"adapter_id": "client", "adapter_version": "1.2.3"},
        },
    )

    assert text.startswith(
        "V7 ran under study contract v2: no request had a deadline, no stream "
        "asked the provider to report its token usage, and every manifest pinned "
        "Itself's shipped client, `client` `1.2.3`. "
    )
