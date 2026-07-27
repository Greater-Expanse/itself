# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Deterministic diagnostician fixture for causal-diagnosis case 001."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from experiments.diagnostician import (
    CONTRACT_VERSION,
    DiagnosisAssertion,
    DiagnosisObservation,
    DiagnosisRequest,
    DiagnosisResult,
    DiagnosisUsage,
    DiagnosticianIdentity,
    DiagnosticPrediction,
    DiagnosticTestOption,
    HypothesisOption,
    RawOutputArtifact,
)
from experiments.narrative_diagnostician import NarrativeAssertion, NarrativeResult
from itself import ActorType

from .cache_key_omission import (
    PREDICTION_MATRIX,
    DiagnosticContext,
    Intervention,
    Mechanism,
)

CASE_ID: Final = "001-cache-key-omission"
TASK: Final = (
    "Identify the cause of repeated stale report output and select one "
    "discriminating test."
)

HYPOTHESIS_STATEMENTS: Final[Mapping[Mechanism, str]] = {
    Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION: (
        "The caching proxy reuses revision A because its key omits source_revision."
    ),
    Mechanism.WORKER_READS_STALE_SOURCE: (
        "The report worker reads a stale source-store view at revision A."
    ),
    Mechanism.VERIFIER_READS_STALE_ARTIFACT: (
        "The verifier reads a previous artifact containing revision A."
    ),
}

TEST_QUESTIONS: Final[Mapping[Intervention, str]] = {
    Intervention.UNCHANGED_REPLAY: ("What revision appears after an unchanged replay?"),
    Intervention.BYPASS_CACHING_PROXY: (
        "What revision appears when the caching proxy is bypassed?"
    ),
    Intervention.DIRECT_WORKER_INSPECTION: (
        "What revision does a direct worker response contain?"
    ),
    Intervention.FRESH_ARTIFACT_PATH: (
        "What revision is verified through a fresh artifact path?"
    ),
}


def build_cache_key_request(context: DiagnosticContext) -> DiagnosisRequest:
    """Build case input exclusively from diagnostician-visible context."""

    return DiagnosisRequest(
        case_id=CASE_ID,
        task=TASK,
        workflow_components=context.workflow_components,
        observations=tuple(
            DiagnosisObservation(
                id=observation.attempt_id,
                summary="The report task returned a stale revision.",
                result=observation.to_json_object(),
            )
            for observation in context.baseline_observations
        ),
        hypothesis_options=tuple(
            HypothesisOption(id=mechanism.value, statement=statement)
            for mechanism, statement in HYPOTHESIS_STATEMENTS.items()
        ),
        test_options=tuple(
            DiagnosticTestOption(
                id=intervention.value,
                question=question,
                estimated_cost_units=1,
            )
            for intervention, question in TEST_QUESTIONS.items()
        ),
    )


@dataclass(frozen=True, slots=True)
class CacheKeyFixtureDiagnostician:
    """Transparent fixture proving the adapter contract without a model call."""

    primary_hypothesis: Mechanism = Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION
    selected_test: Intervention = Intervention.BYPASS_CACHING_PROXY

    def diagnose(self, request: DiagnosisRequest, /) -> DiagnosisResult:
        """Return deterministic predictions for all supplied case hypotheses."""

        retained_ids = tuple(option.id for option in request.hypothesis_options)
        predictions = tuple(
            DiagnosticPrediction(
                hypothesis_id=hypothesis_id,
                test_id=self.selected_test.value,
                expected_observation=PREDICTION_MATRIX[Mechanism(hypothesis_id)][
                    self.selected_test
                ].value,
                falsified_when=(
                    "The observed revision differs from the predicted revision."
                ),
            )
            for hypothesis_id in retained_ids
        )
        assertion = DiagnosisAssertion(
            retained_hypothesis_ids=retained_ids,
            primary_hypothesis_id=self.primary_hypothesis.value,
            predictions=predictions,
            selected_test_id=self.selected_test.value,
            public_explanation=(
                "The selected intervention compares preregistered outcomes across "
                "all retained causal hypotheses."
            ),
            expressed_confidence=2 / 3,
        )
        raw_output = json.dumps(
            assertion.to_json_object(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return DiagnosisResult(
            contract_version=CONTRACT_VERSION,
            diagnostician=DiagnosticianIdentity(
                actor_id="case-001-fixture-diagnostician",
                actor_type=ActorType.SOFTWARE,
                adapter_id="deterministic-case-001",
                adapter_version="1",
            ),
            assertion=assertion,
            raw_output_artifact=RawOutputArtifact(
                uri="urn:gxp:fixture:case-001:diagnostician-output",
                media_type="application/json",
                sha256=hashlib.sha256(raw_output).hexdigest(),
                captured_at="2026-07-22T13:00:00Z",
            ),
            usage=DiagnosisUsage(),
        )


@dataclass(frozen=True, slots=True)
class CacheKeyNarrativeFixtureDiagnostician:
    """Transparent narrative-only fixture for comparative condition A."""

    selected_hypothesis: Mechanism = Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION

    def diagnose(self, request: DiagnosisRequest, /) -> NarrativeResult:
        """Select one supplied hypothesis without predictions or test choice."""

        del request
        assertion = NarrativeAssertion(
            selected_hypothesis_id=self.selected_hypothesis.value,
            public_explanation=(
                "The repeated stale report is most consistent with reuse by the "
                "caching proxy."
            ),
            expressed_confidence=2 / 3,
        )
        raw_output = json.dumps(
            assertion.to_json_object(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return NarrativeResult(
            diagnostician=DiagnosticianIdentity(
                actor_id="case-001-fixture-narrative-diagnostician",
                actor_type=ActorType.SOFTWARE,
                adapter_id="deterministic-case-001-narrative",
                adapter_version="1",
            ),
            assertion=assertion,
            raw_output_artifact=RawOutputArtifact(
                uri="urn:gxp:fixture:case-001:narrative-output",
                media_type="application/json",
                sha256=hashlib.sha256(raw_output).hexdigest(),
                captured_at="2026-07-22T13:00:00Z",
            ),
            usage=DiagnosisUsage(),
        )
