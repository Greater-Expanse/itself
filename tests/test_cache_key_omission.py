# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import json

import pytest

from experiments.cases.cache_key_omission import (
    PREDICTION_MATRIX,
    BehavioralOracle,
    CaseEnvironment,
    CausalOracle,
    Intervention,
    Mechanism,
    Patch,
    Revision,
)


@pytest.mark.parametrize(
    ("mechanism", "intervention", "expected_revision"),
    [
        (mechanism, intervention, expected_revision)
        for mechanism, predictions in PREDICTION_MATRIX.items()
        for intervention, expected_revision in predictions.items()
    ],
)
def test_intervention_matrix_matches_preregistered_predictions(
    mechanism: Mechanism,
    intervention: Intervention,
    expected_revision: Revision,
) -> None:
    observation = CaseEnvironment(mechanism).execute(intervention)

    assert observation.observed_revision is expected_revision


@pytest.mark.parametrize("mechanism", list(Mechanism))
def test_all_mechanisms_produce_same_six_failure_surface(
    mechanism: Mechanism,
) -> None:
    history = CaseEnvironment(mechanism).baseline_history()

    assert len(history) == 6
    assert len({observation.attempt_id for observation in history}) == 6
    assert all(observation.observed_revision is Revision.A for observation in history)
    assert all(not observation.passed for observation in history)


@pytest.mark.parametrize("mechanism", list(Mechanism))
def test_diagnostic_context_does_not_disclose_configured_mechanism(
    mechanism: Mechanism,
) -> None:
    context = CaseEnvironment(mechanism).diagnostic_context()
    serialized = json.dumps(context.to_json_object(), sort_keys=True)

    assert mechanism.value not in serialized


def test_baseline_observation_retains_all_hypotheses() -> None:
    matches = CausalOracle.matching_hypotheses(
        {Intervention.UNCHANGED_REPLAY: Revision.A}
    )

    assert matches == frozenset(Mechanism)


def test_bypass_result_uniquely_identifies_cache_key_mechanism() -> None:
    environment = CaseEnvironment(Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION)
    result = environment.execute(Intervention.BYPASS_CACHING_PROXY)
    oracle = CausalOracle(Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION)

    assert oracle.identifies_ground_truth(
        {
            Intervention.UNCHANGED_REPLAY: Revision.A,
            Intervention.BYPASS_CACHING_PROXY: result.observed_revision,
        }
    )


def test_bypass_discriminates_cache_hypothesis_from_both_alternatives() -> None:
    assert CausalOracle.discriminates(
        Intervention.BYPASS_CACHING_PROXY,
        Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION,
        [
            Mechanism.WORKER_READS_STALE_SOURCE,
            Mechanism.VERIFIER_READS_STALE_ARTIFACT,
        ],
    )


def test_bypass_does_not_discriminate_two_non_cache_hypotheses() -> None:
    assert not CausalOracle.discriminates(
        Intervention.BYPASS_CACHING_PROXY,
        Mechanism.WORKER_READS_STALE_SOURCE,
        [Mechanism.VERIFIER_READS_STALE_ARTIFACT],
    )


def test_negative_control_restores_behavior_without_addressing_cause() -> None:
    environment = CaseEnvironment(Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION)
    oracle = CausalOracle(Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION)
    result = environment.apply_patch(Patch.FORCE_EXPECTED_OUTPUT)

    assert BehavioralOracle.passes(result)
    assert not oracle.patch_addresses_cause(Patch.FORCE_EXPECTED_OUTPUT)


@pytest.mark.parametrize(
    ("mechanism", "patch"),
    [
        (
            Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION,
            Patch.INCLUDE_REVISION_IN_CACHE_KEY,
        ),
        (Mechanism.WORKER_READS_STALE_SOURCE, Patch.REFRESH_WORKER_SOURCE),
        (
            Mechanism.VERIFIER_READS_STALE_ARTIFACT,
            Patch.VERIFY_CURRENT_ARTIFACT,
        ),
    ],
)
def test_mechanism_specific_patch_restores_behavior_and_addresses_cause(
    mechanism: Mechanism,
    patch: Patch,
) -> None:
    environment = CaseEnvironment(mechanism)
    oracle = CausalOracle(mechanism)

    assert BehavioralOracle.passes(environment.apply_patch(patch))
    assert oracle.patch_addresses_cause(patch)


def test_each_execution_resets_to_same_controlled_state() -> None:
    environment = CaseEnvironment(Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION)

    first = environment.execute(Intervention.BYPASS_CACHING_PROXY)
    environment.execute(Intervention.FRESH_ARTIFACT_PATH)
    repeated = environment.execute(Intervention.BYPASS_CACHING_PROXY)

    assert first.observed_revision is repeated.observed_revision
    assert first.to_json_object() == repeated.to_json_object()


def test_baseline_attempt_count_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least one"):
        CaseEnvironment(Mechanism.CACHE_KEY_OMITS_SOURCE_REVISION).baseline_history(0)
