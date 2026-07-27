# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Deterministic environment for causal-diagnosis case 001."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from itself import JsonObject


class Revision(StrEnum):
    """Observable source and report revisions in the controlled case."""

    A = "A"
    B = "B"


class Mechanism(StrEnum):
    """Evaluator-configured mechanisms compatible with the initial failures."""

    CACHE_KEY_OMITS_SOURCE_REVISION = "cache_key_omits_source_revision"
    WORKER_READS_STALE_SOURCE = "worker_reads_stale_source"
    VERIFIER_READS_STALE_ARTIFACT = "verifier_reads_stale_artifact"


class Intervention(StrEnum):
    """Controlled observations available to a diagnostician."""

    UNCHANGED_REPLAY = "unchanged_replay"
    BYPASS_CACHING_PROXY = "bypass_caching_proxy"
    DIRECT_WORKER_INSPECTION = "direct_worker_inspection"
    FRESH_ARTIFACT_PATH = "fresh_artifact_path"


class Patch(StrEnum):
    """Candidate remediations scored separately for behavior and cause."""

    INCLUDE_REVISION_IN_CACHE_KEY = "include_revision_in_cache_key"
    REFRESH_WORKER_SOURCE = "refresh_worker_source"
    VERIFY_CURRENT_ARTIFACT = "verify_current_artifact"
    FORCE_EXPECTED_OUTPUT = "force_expected_output"


@dataclass(frozen=True, slots=True)
class Observation:
    """Externally visible result of one baseline attempt or intervention."""

    attempt_id: str
    intervention: Intervention
    expected_revision: Revision
    observed_revision: Revision

    @property
    def passed(self) -> bool:
        """Return whether the behavioral report task succeeded."""

        return self.observed_revision is self.expected_revision

    def to_json_object(self) -> JsonObject:
        """Return the diagnostician-visible wire representation."""

        return {
            "attempt_id": self.attempt_id,
            "intervention": self.intervention.value,
            "expected_revision": self.expected_revision.value,
            "observed_revision": self.observed_revision.value,
            "task_result": "passed" if self.passed else "failed",
        }


@dataclass(frozen=True, slots=True)
class DiagnosticContext:
    """Public case information that deliberately excludes configured cause."""

    workflow_components: tuple[str, ...]
    baseline_observations: tuple[Observation, ...]
    available_interventions: tuple[Intervention, ...]

    def to_json_object(self) -> JsonObject:
        """Return a provider-neutral diagnostician input object."""

        return {
            "workflow_components": list(self.workflow_components),
            "baseline_observations": [
                observation.to_json_object()
                for observation in self.baseline_observations
            ],
            "available_interventions": [
                intervention.value for intervention in self.available_interventions
            ],
        }


PREDICTION_MATRIX: Final[Mapping[Mechanism, Mapping[Intervention, Revision]]] = {
    Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION: {
        Intervention.UNCHANGED_REPLAY: Revision.A,
        Intervention.BYPASS_CACHING_PROXY: Revision.B,
        Intervention.DIRECT_WORKER_INSPECTION: Revision.B,
        Intervention.FRESH_ARTIFACT_PATH: Revision.A,
    },
    Mechanism.WORKER_READS_STALE_SOURCE: {
        Intervention.UNCHANGED_REPLAY: Revision.A,
        Intervention.BYPASS_CACHING_PROXY: Revision.A,
        Intervention.DIRECT_WORKER_INSPECTION: Revision.A,
        Intervention.FRESH_ARTIFACT_PATH: Revision.A,
    },
    Mechanism.VERIFIER_READS_STALE_ARTIFACT: {
        Intervention.UNCHANGED_REPLAY: Revision.A,
        Intervention.BYPASS_CACHING_PROXY: Revision.A,
        Intervention.DIRECT_WORKER_INSPECTION: Revision.B,
        Intervention.FRESH_ARTIFACT_PATH: Revision.B,
    },
}

_CORRECT_PATCH: Final[Mapping[Mechanism, Patch]] = {
    Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION: Patch.INCLUDE_REVISION_IN_CACHE_KEY,
    Mechanism.WORKER_READS_STALE_SOURCE: Patch.REFRESH_WORKER_SOURCE,
    Mechanism.VERIFIER_READS_STALE_ARTIFACT: Patch.VERIFY_CURRENT_ARTIFACT,
}


class CaseEnvironment:
    """Resettable case environment with evaluator-only mechanism configuration."""

    def __init__(self, mechanism: Mechanism) -> None:
        self._mechanism = mechanism

    def baseline_history(self, attempts: int = 6) -> tuple[Observation, ...]:
        """Return repeated ordinary failures from identical reset states."""

        if attempts < 1:
            raise ValueError("attempts must be at least one")
        return tuple(
            self._execute(
                self._mechanism,
                Intervention.UNCHANGED_REPLAY,
                attempt_id=f"baseline-{attempt:02d}",
            )
            for attempt in range(1, attempts + 1)
        )

    def diagnostic_context(self, attempts: int = 6) -> DiagnosticContext:
        """Return only information authorized for the diagnostician."""

        return DiagnosticContext(
            workflow_components=(
                "source_store",
                "report_worker",
                "caching_proxy",
                "artifact_sink",
                "verifier",
            ),
            baseline_observations=self.baseline_history(attempts),
            available_interventions=tuple(Intervention),
        )

    def execute(self, intervention: Intervention) -> Observation:
        """Execute one intervention from the same reset baseline state."""

        return self._execute(
            self._mechanism,
            intervention,
            attempt_id=f"intervention-{intervention.value}",
        )

    def apply_patch(self, patch: Patch) -> Observation:
        """Apply a candidate patch and return its behavioral result."""

        if patch is Patch.FORCE_EXPECTED_OUTPUT:
            return Observation(
                attempt_id=f"patch-{patch.value}",
                intervention=Intervention.UNCHANGED_REPLAY,
                expected_revision=Revision.B,
                observed_revision=Revision.B,
            )
        active_mechanism = (
            None if _CORRECT_PATCH[self._mechanism] is patch else self._mechanism
        )
        return self._execute(
            active_mechanism,
            Intervention.UNCHANGED_REPLAY,
            attempt_id=f"patch-{patch.value}",
        )

    @staticmethod
    def _execute(
        mechanism: Mechanism | None,
        intervention: Intervention,
        *,
        attempt_id: str,
    ) -> Observation:
        source_revision = Revision.B
        worker_revision = (
            Revision.A
            if mechanism is Mechanism.WORKER_READS_STALE_SOURCE
            else source_revision
        )

        if intervention is Intervention.DIRECT_WORKER_INSPECTION:
            observed_revision = worker_revision
        else:
            bypass_cache = intervention is Intervention.BYPASS_CACHING_PROXY
            proxy_revision = (
                Revision.A
                if mechanism is Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION
                and not bypass_cache
                else worker_revision
            )
            fresh_path = intervention is Intervention.FRESH_ARTIFACT_PATH
            observed_revision = (
                Revision.A
                if mechanism is Mechanism.VERIFIER_READS_STALE_ARTIFACT
                and not fresh_path
                else proxy_revision
            )

        return Observation(
            attempt_id=attempt_id,
            intervention=intervention,
            expected_revision=source_revision,
            observed_revision=observed_revision,
        )


class BehavioralOracle:
    """Score task recovery without making a causal claim."""

    @staticmethod
    def passes(observation: Observation) -> bool:
        """Return whether the observed report matches the current source."""

        return observation.passed


class CausalOracle:
    """Evaluator-only oracle for predictions, discrimination, and patches."""

    def __init__(self, ground_truth: Mechanism) -> None:
        self.ground_truth: Mechanism = ground_truth

    @staticmethod
    def predict(
        hypothesis: Mechanism,
        intervention: Intervention,
    ) -> Revision:
        """Return the preregistered observation under one hypothesis."""

        return PREDICTION_MATRIX[hypothesis][intervention]

    @staticmethod
    def matching_hypotheses(
        results: Mapping[Intervention, Revision],
    ) -> frozenset[Mechanism]:
        """Return mechanisms consistent with every supplied result."""

        return frozenset(
            mechanism
            for mechanism, predictions in PREDICTION_MATRIX.items()
            if all(
                predictions[intervention] is revision
                for intervention, revision in results.items()
            )
        )

    @staticmethod
    def discriminates(
        intervention: Intervention,
        target: Mechanism,
        alternatives: Sequence[Mechanism],
    ) -> bool:
        """Return whether a test separates a target from every live alternative."""

        target_prediction = PREDICTION_MATRIX[target][intervention]
        return bool(alternatives) and all(
            PREDICTION_MATRIX[alternative][intervention] is not target_prediction
            for alternative in alternatives
        )

    def identifies_ground_truth(
        self,
        results: Mapping[Intervention, Revision],
    ) -> bool:
        """Return whether supplied results uniquely identify configured cause."""

        return self.matching_hypotheses(results) == frozenset({self.ground_truth})

    def patch_addresses_cause(self, patch: Patch) -> bool:
        """Return whether a patch directly repairs the configured mechanism."""

        return _CORRECT_PATCH[self.ground_truth] is patch
