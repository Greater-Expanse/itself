# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

# pyright: reportPrivateUsage=false

"""Run every registered v6 slot over fake transports, then report the study."""

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
from itself import InferenceError, InferenceFailure, JsonObject

SECRET = "v6-study-secret-that-must-not-leak"
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
TARGETS = {target.target_id: target for target in STUDY_TARGETS_BY_VERSION["6"]}
SLOTS = STUDY_EXECUTION_SLOTS_BY_VERSION["6"]
# The slots that stop, and how: two provider failures, one response that
# reports no token usage, and one failure of the harness after both responses.
RATE_LIMITED = ("together-glm-5p2", 2)
PAST_DEADLINE = ("together-glm-5p2", 5)
WITHOUT_USAGE = ("together-minimax-m3p1-flash", 4)
HARNESS_FAILURE = ("together-qwen3p7-plus", 3)


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
    return [
        _streamed(target, narrative.assertion.to_json_object(), input_tokens=157),
        _streamed(
            target,
            structured.assertion.to_model_json_object(),
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
    report: JsonObject
    requests: tuple[tuple[str, int, HttpRequest], ...]


# One run of all fifteen slots serves every test in this module.
@pytest.fixture(scope="module")
def study_run(tmp_path_factory: pytest.TempPathFactory) -> StudyRun:
    with pytest.MonkeyPatch.context() as monkeypatch:
        return _run_study(tmp_path_factory.mktemp("v6-study"), monkeypatch)


def _run_study(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StudyRun:
    manifests = tmp_path / "manifests"
    bundles = tmp_path / "bundles"
    write_study_manifests(manifests, registered_at=NOW, study_version="6")
    # The run executes under the client its manifests pin.
    monkeypatch.setattr(
        shipped_inference,
        "ADAPTER_VERSION",
        V6_SHIPPED_ADAPTER.adapter_version,
    )
    clock = itertools.count(1_000_000, 1_000_000)
    sent: list[tuple[str, int, HttpRequest]] = []
    for target_id, replicate_index in SLOTS:
        target = TARGETS[target_id]
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
    report = build_comparison_report(bundles, study_version="6", reported_at=NOW)
    return StudyRun(report=report, requests=tuple(sent))


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
    for target_id, _, request in study_run.requests:
        payload = cast(JsonObject, json.loads(request.body))
        assert request.url.startswith(TOGETHER_ENDPOINT.base_url + "/")
        assert request.deadline_seconds == 600.0
        assert request.timeout_seconds == 300.0
        assert payload["model"] == TARGETS[target_id].model_id
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        assert payload["temperature"] == 1.0
        assert payload["top_p"] == 0.95


def test_the_v6_report_counts_each_kind_of_stopped_run(study_run: StudyRun) -> None:
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

    assert report["study_version"] == "6"
    assert report["report_version"] == "0.4.0"
    assert totals["registered_replicates"] == 15
    assert totals["completed_replicates"] == 11
    assert totals["failed_replicates"] == 3
    assert totals["runner_failed_replicates"] == 1
    assert sorted(
        (cast(str, failure["attempt_id"]), cast(str, failure["kind"]))
        for failure in cast(list[JsonObject], totals["failures"])
    ) == [
        (NARRATIVE_ATTEMPT_ID, "http_rate_limit"),
        (NARRATIVE_ATTEMPT_ID, "transport"),
        (STRUCTURED_ATTEMPT_ID, "response_usage"),
    ]
    assert totals["runner_failures"] == [
        {"attempt_id": STRUCTURED_ATTEMPT_ID, "kind": "runner_error", "count": 1}
    ]
    assert {
        target_id: summary["completed_replicates"]
        for target_id, summary in summaries.items()
    } == {
        "together-glm-5p2": 3,
        "together-minimax-m3p1-flash": 4,
        "together-qwen3p7-plus": 4,
    }
    assert outcomes[HARNESS_FAILURE] == "runner_failed"
    assert [
        outcomes[slot] for slot in (RATE_LIMITED, PAST_DEADLINE, WITHOUT_USAGE)
    ] == ["adapter_failed"] * 3
    assert SECRET not in json.dumps(report)
    assert "projection failed" not in json.dumps(report)


def test_the_v6_report_carries_its_design(study_run: StudyRun) -> None:
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


def test_the_v6_markdown_states_its_design_and_failures(study_run: StudyRun) -> None:
    markdown = render_markdown(study_run.report)

    assert markdown == render_markdown(
        cast(JsonObject, json.loads(json.dumps(study_run.report)))
    )
    assert markdown.startswith("# Case 001 comparison study v6\n\n## What the")
    assert "Status: historical study version" not in markdown
    assert (
        "We scheduled that two-call, three-path procedure 15 times—5 times for "
        "each of 3 models."
    ) in markdown
    assert (
        "Of those planned runs, 11 completed both model calls and all three "
        "paths. Of the other 4, 3 stopped on an explicit provider or "
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
        "harness: 1 test-plan request: harness failed after the request was "
        "sent (`runner_error`)",
    ):
        assert label in markdown
    assert (
        "## How the experiment was controlled\n\n"
        "V6 binds `expected_observation` to the exact revision codes `A` and `B` "
        "in every manifest, output schema, and rendered payload. The "
        "deterministic scorer therefore compares like-for-like categorical "
        "values without a text parser or post-hoc reinterpretation.\n\n"
        "V6 fixes one provider and one compatible base endpoint across all three "
        "model targets. Calls are executed round-robin by replicate index to "
        "distribute temporal serving variation. Provider identity is not a "
        "behavioral metric, and no model output or scorecard from an earlier "
        "version is reused. V6 differs from v5 in provider, in one model, and in "
        "study contract, so the two results are not pooled or compared.\n\n"
        "V6 ran under study contract v2: every request had a 600-second "
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


def test_the_v6_report_states_its_limitations(study_run: StudyRun) -> None:
    limitations = cast(list[str], study_run.report["limitations"])

    for limitation in (
        "V6 fixes one provider as controlled infrastructure; provider identity "
        "is provenance only.",
        "V6 differs from v5 in provider, in one model, and in study contract; "
        "the two are not pooled or compared.",
        "No model output or scorecard from an earlier version is reused in v6.",
        "Harness failures after a request was sent are counted apart from "
        "provider and model-output failures; they are not scored as wrong "
        "diagnoses.",
        "Qwen 3.7 Plus is a hosted-only comparator, not an open-weight result.",
    ):
        assert limitation in limitations
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
