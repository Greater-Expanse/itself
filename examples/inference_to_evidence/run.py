# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Build an evidence bundle from a model assertion and an external test."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, cast
from urllib.parse import unquote, urlsplit

from itself import (
    Actor,
    ActorRole,
    ActorType,
    Authority,
    AuthorityType,
    ClaimStatus,
    ClaimType,
    DecisionDisposition,
    Digest,
    DigestAlgorithm,
    DirectoryArtifactSink,
    EvidenceBundleBuilder,
    EvidenceBundleFile,
    EvidenceBundleFileRole,
    EvidenceRelation,
    EvidenceRelationType,
    EvidenceType,
    HttpRequest,
    HttpResponse,
    JsonObject,
    JsonValue,
    Ledger,
    OpenAICompatibleEndpoint,
    Oracle,
    PredictionCondition,
    RecordHeader,
    Scope,
    StructuredInferenceAdapter,
    StructuredInferenceClient,
    StructuredOutputProfile,
    TestDesign,
    TestStatus,
    VerdictOutcome,
    VerifiedEvidenceBundle,
    artifact_reference_record,
    canonical_bundle_json,
    claim_record,
    decision_record,
    evidence_record,
    hypothesis_record,
    prediction_record,
    protocol_test_record,
    status_transition_record,
    verdict_record,
)

CREATED_AT: Final = datetime(2026, 7, 23, 15, tzinfo=UTC)
RAW_RESPONSE_RECORD_ID: Final = "artifact-provider-response"
ASSERTION_RECORD_ID: Final = "artifact-model-assertion"
OBSERVATION_RECORD_ID: Final = "artifact-verifier-observation"
RAW_RESPONSE_PATH: Final = "artifacts/provider-response.json"
ASSERTION_PATH: Final = "artifacts/model-assertion.json"
OBSERVATION_PATH: Final = "artifacts/verifier-observation.json"


def _incident() -> JsonObject:
    return {
        "incident_id": "report-184",
        "symptom": "The generated report returned a stale revision.",
        "observed_revision": "revision-A",
        "source_revision": "revision-B",
    }


def _fixture_assertion() -> JsonObject:
    return {
        "hypothesis": (
            "The stale report is caused by the cache path rather than the "
            "current source record."
        ),
        "predicted_observation": "revision-B",
        "recommended_test": "bypass_cache",
    }


def _pretty_json(value: JsonValue) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _fixture_response() -> HttpResponse:
    assertion_text = canonical_bundle_json(_fixture_assertion()).decode("utf-8")
    envelope: JsonObject = {
        "id": "fixture-completion-1",
        "model": "fixture/diagnostician-v1",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": assertion_text,
                },
            }
        ],
        "usage": {
            "prompt_tokens": 96,
            "completion_tokens": 38,
            "total_tokens": 134,
        },
    }
    return HttpResponse(
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=canonical_bundle_json(envelope),
    )


def _empty_requests() -> list[HttpRequest]:
    return []


@dataclass(slots=True)
class FixtureTransport:
    """Return one reviewed response while recording the real rendered request."""

    response: HttpResponse
    requests: list[HttpRequest] = field(default_factory=_empty_requests)

    def send(self, request: HttpRequest, /) -> HttpResponse:
        """Implement the public transport boundary without network access."""

        self.requests.append(request)
        return self.response


@dataclass(slots=True)
class FixtureClock:
    """Produce a deterministic four-millisecond inference duration."""

    calls: int = 0

    def __call__(self) -> int:
        self.calls += 1
        return 0 if self.calls == 1 else 4_000_000


@dataclass(frozen=True, slots=True)
class ReportEnvironment:
    """Tiny external system exercised by the declared controlled test."""

    cached_revision: str
    source_revision: str

    def read_report(self, *, bypass_cache: bool) -> str:
        """Return the observable report revision under one intervention."""

        return self.source_revision if bypass_cache else self.cached_revision


def _assertion_profile() -> StructuredOutputProfile:
    schema: JsonObject = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "hypothesis",
            "predicted_observation",
            "recommended_test",
        ],
        "properties": {
            "hypothesis": {"type": "string", "minLength": 1},
            "predicted_observation": {"type": "string", "minLength": 1},
            "recommended_test": {"type": "string", "minLength": 1},
        },
    }
    return StructuredOutputProfile(
        profile_id="incident-diagnosis-v1",
        schema_name="incident_diagnosis",
        system_prompt=(
            "Return one structured causal assertion. Do not claim that it has "
            "been verified."
        ),
        user_instruction=(
            "State one falsifiable hypothesis, its predicted observation, and "
            "one recommended test."
        ),
        input_label="INCIDENT",
        schema=schema,
    )


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        raise RuntimeError(f"{context} must be a JSON object")
    return value


def _required_text(value: JsonObject, key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise RuntimeError(f"model assertion field {key!r} must be non-empty text")
    return item


def _captured_file(uri: str) -> Path:
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.netloc:
        raise RuntimeError("the example requires a local DirectoryArtifactSink")
    return Path(unquote(parsed.path))


def _run_external_test(
    test_name: str,
    environment: ReportEnvironment,
) -> JsonObject:
    if test_name != "bypass_cache":
        raise RuntimeError(f"no external verifier is registered for {test_name!r}")
    return {
        "test_name": test_name,
        "baseline_revision": environment.read_report(bypass_cache=False),
        "observed_revision": environment.read_report(bypass_cache=True),
    }


def _header(record_id: str, second: int, actor: Actor) -> RecordHeader:
    return RecordHeader(
        record_id=record_id,
        created_at=CREATED_AT + timedelta(seconds=second),
        created_by=actor,
    )


def _ledger(
    assertion: JsonObject,
    observation: JsonObject,
    *,
    raw_response_digest: str,
    assertion_digest: str,
    observation_digest: str,
    model_implementation: str,
) -> Ledger:
    model = Actor(
        actor_id="fixture-diagnostician",
        actor_type=ActorType.MODEL,
        role=ActorRole.PROPOSER,
        implementation_ref=model_implementation,
    )
    operator = Actor(
        actor_id="example-runner",
        actor_type=ActorType.SOFTWARE,
        role=ActorRole.OPERATOR,
        implementation_ref="examples.inference_to_evidence.run",
    )
    evaluator = Actor(
        actor_id="cache-bypass-verifier",
        actor_type=ActorType.SOFTWARE,
        role=ActorRole.EVALUATOR,
        implementation_ref="examples.inference_to_evidence.run@1",
    )
    scope = Scope(
        description="One controlled stale-report incident fixture",
        dimensions={
            "incident_id": "report-184",
            "environment": "deterministic-example",
        },
    )
    authority = Authority(
        authority_type=AuthorityType.DETERMINISTIC_TOOL,
        actor_ref=evaluator.actor_id,
        basis="Direct observation from the declared cache-bypass intervention",
        independent_of_subject=True,
    )
    oracle = Oracle(
        adapter="cache-bypass-exact-match",
        version="1",
        authority_type=AuthorityType.DETERMINISTIC_TOOL,
        declared_scope="Exact report revision returned by the fixture environment",
    )

    hypothesis = _required_text(assertion, "hypothesis")
    prediction = _required_text(assertion, "predicted_observation")
    test_name = _required_text(assertion, "recommended_test")
    observed_revision = _required_text(observation, "observed_revision")
    supported = observed_revision == prediction
    relation = (
        EvidenceRelationType.SUPPORTS if supported else EvidenceRelationType.CONTRADICTS
    )
    outcome = VerdictOutcome.SUPPORTED if supported else VerdictOutcome.REFUTED
    final_status = ClaimStatus.SUPPORTED if supported else ClaimStatus.REFUTED
    disposition = (
        DecisionDisposition.APPROVED if supported else DecisionDisposition.REJECTED
    )

    records = (
        artifact_reference_record(
            _header(RAW_RESPONSE_RECORD_ID, 0, model),
            uri=RAW_RESPONSE_PATH,
            media_type="application/json",
            title="Reviewed fixture provider response",
            digest=Digest(DigestAlgorithm.SHA256, raw_response_digest),
            captured_at=CREATED_AT,
        ),
        artifact_reference_record(
            _header(ASSERTION_RECORD_ID, 1, model),
            uri=ASSERTION_PATH,
            media_type="application/json",
            title="Schema-validated model assertion",
            digest=Digest(DigestAlgorithm.SHA256, assertion_digest),
            captured_at=CREATED_AT,
            derived_from_refs=(RAW_RESPONSE_RECORD_ID,),
        ),
        hypothesis_record(
            _header("hypothesis-cache-path", 2, model),
            statement=hypothesis,
            scope=scope,
            dependency_refs=(ASSERTION_RECORD_ID,),
            prediction_refs=("prediction-cache-bypass",),
        ),
        status_transition_record(
            _header("transition-hypothesis-testable", 3, model),
            subject_ref="hypothesis-cache-path",
            from_status=ClaimStatus.PROPOSED,
            to_status=ClaimStatus.TESTABLE,
            authorized_by=model,
            reason="The assertion includes a falsifiable prediction and test.",
        ),
        prediction_record(
            _header("prediction-cache-bypass", 4, model),
            hypothesis_ref="hypothesis-cache-path",
            condition=PredictionCondition.HYPOTHESIS_TRUE,
            expected_observation=prediction,
            falsified_when=(
                "Bypassing the cache does not return the predicted source revision."
            ),
            test_ref="test-cache-bypass-plan",
            scope=scope,
        ),
        protocol_test_record(
            _header("test-cache-bypass-plan", 5, model),
            question=f"What revision is returned by the {test_name} intervention?",
            design=TestDesign.CONTROLLED_ABLATION,
            status=TestStatus.PLANNED,
            oracle=oracle,
            scope=scope,
            subject_refs=("hypothesis-cache-path",),
            prediction_refs=("prediction-cache-bypass",),
        ),
        status_transition_record(
            _header("transition-hypothesis-under-test", 6, operator),
            subject_ref="hypothesis-cache-path",
            from_status=ClaimStatus.TESTABLE,
            to_status=ClaimStatus.UNDER_TEST,
            authorized_by=operator,
            reason="The external cache-bypass verifier is executing.",
        ),
        artifact_reference_record(
            _header(OBSERVATION_RECORD_ID, 7, evaluator),
            uri=OBSERVATION_PATH,
            media_type="application/json",
            title="Deterministic verifier observation",
            digest=Digest(DigestAlgorithm.SHA256, observation_digest),
            captured_at=CREATED_AT + timedelta(seconds=7),
        ),
        protocol_test_record(
            _header("test-cache-bypass-run", 8, operator),
            question=f"What revision is returned by the {test_name} intervention?",
            design=TestDesign.CONTROLLED_ABLATION,
            status=TestStatus.COMPLETED,
            oracle=oracle,
            scope=scope,
            subject_refs=("hypothesis-cache-path",),
            prediction_refs=("prediction-cache-bypass",),
            plan_ref="test-cache-bypass-plan",
            evidence_refs=("evidence-cache-bypass",),
        ),
        evidence_record(
            _header("evidence-cache-bypass", 9, evaluator),
            evidence_type=EvidenceType.DETERMINISTIC_TEST,
            relations=(
                EvidenceRelation(
                    subject_ref="hypothesis-cache-path",
                    relation=relation,
                    public_note=(
                        "The external verifier compared the observed revision "
                        "with the model's declared prediction."
                    ),
                ),
                EvidenceRelation(
                    subject_ref="claim-cache-path-cause",
                    relation=relation,
                    public_note=(
                        "The same external observation bears on the derived "
                        "scoped causal claim."
                    ),
                ),
            ),
            authority=authority,
            scope=scope,
            artifact_refs=(OBSERVATION_RECORD_ID,),
            test_ref="test-cache-bypass-run",
            result={
                "expected_revision": prediction,
                "observed_revision": observed_revision,
                "matched": supported,
            },
        ),
        verdict_record(
            _header("verdict-cache-path", 10, evaluator),
            subject_ref="hypothesis-cache-path",
            outcome=outcome,
            evidence_refs=("evidence-cache-bypass",),
            authority=authority,
            scope=scope,
            public_rationale=(
                "The external observation matched the declared prediction."
                if supported
                else "The external observation contradicted the declared prediction."
            ),
        ),
        status_transition_record(
            _header("transition-hypothesis-final", 11, evaluator),
            subject_ref="hypothesis-cache-path",
            from_status=ClaimStatus.UNDER_TEST,
            to_status=final_status,
            authorized_by=evaluator,
            reason="External evidence determined the scoped epistemic state.",
            evidence_refs=("evidence-cache-bypass",),
            verdict_ref="verdict-cache-path",
        ),
        claim_record(
            _header("claim-cache-path-cause", 12, evaluator),
            proposition=(
                "The cache path caused the stale report in the declared fixture."
            ),
            claim_type=ClaimType.CAUSAL,
            scope=scope,
            evidence_refs=("evidence-cache-bypass",),
            dependency_refs=("hypothesis-cache-path",),
            invalidation_conditions=(
                "The fixture configuration or cache behavior changes.",
            ),
        ),
        status_transition_record(
            _header("transition-claim-testable", 13, evaluator),
            subject_ref="claim-cache-path-cause",
            from_status=ClaimStatus.PROPOSED,
            to_status=ClaimStatus.TESTABLE,
            authorized_by=evaluator,
            reason="The derived claim names a testable cause within the fixture.",
        ),
        status_transition_record(
            _header("transition-claim-under-test", 14, evaluator),
            subject_ref="claim-cache-path-cause",
            from_status=ClaimStatus.TESTABLE,
            to_status=ClaimStatus.UNDER_TEST,
            authorized_by=evaluator,
            reason="The recorded external observation is being adjudicated.",
        ),
        verdict_record(
            _header("verdict-cache-path-claim", 15, evaluator),
            subject_ref="claim-cache-path-cause",
            outcome=outcome,
            evidence_refs=("evidence-cache-bypass",),
            authority=authority,
            scope=scope,
            public_rationale=(
                "The external observation matched the causal claim's prediction."
                if supported
                else "The external observation contradicted the causal claim."
            ),
        ),
        status_transition_record(
            _header("transition-claim-final", 16, evaluator),
            subject_ref="claim-cache-path-cause",
            from_status=ClaimStatus.UNDER_TEST,
            to_status=final_status,
            authorized_by=evaluator,
            reason="The verdict determined the claim's scoped epistemic state.",
            evidence_refs=("evidence-cache-bypass",),
            verdict_ref="verdict-cache-path-claim",
        ),
        decision_record(
            _header("decision-close-incident", 17, evaluator),
            question="May this fixture incident be closed as a cache-path failure?",
            disposition=disposition,
            authorized_by=evaluator,
            scope=scope,
            relied_on_claim_refs=(("claim-cache-path-cause",) if supported else ()),
            reconsider_when=("New evidence contradicts the cache-bypass observation.",),
            public_rationale=(
                "External evidence supports the scoped causal claim."
                if supported
                else "External evidence refutes the scoped causal claim."
            ),
        ),
    )
    return Ledger(records)


def run_example(
    destination: Path,
    *,
    private_artifact_root: Path,
) -> VerifiedEvidenceBundle:
    """Run the offline workflow and publish one independently verified bundle."""

    transport = FixtureTransport(_fixture_response())
    inference: StructuredInferenceAdapter = StructuredInferenceClient(
        endpoint=OpenAICompatibleEndpoint(
            actor_id="fixture-diagnostician",
            base_url="https://models.example.test/v1",
            model="fixture/diagnostician-v1",
            credential=None,
        ),
        artifact_sink=DirectoryArtifactSink(private_artifact_root / "responses"),
        transport=transport,
        environment={},
        now=lambda: CREATED_AT,
        monotonic_ns=FixtureClock(),
    )
    result = inference.invoke(_incident(), _assertion_profile())
    if len(transport.requests) != 1:
        raise RuntimeError("the structured inference adapter did not issue one request")

    assertion = _object(result.value, "structured model assertion")
    test_name = _required_text(assertion, "recommended_test")
    observation = _run_external_test(
        test_name,
        ReportEnvironment(
            cached_revision="revision-A",
            source_revision="revision-B",
        ),
    )

    raw_response_source = _captured_file(result.raw_response.uri)
    raw_response_content = raw_response_source.read_bytes()
    if _sha256(raw_response_content) != result.raw_response.sha256:
        raise RuntimeError("captured provider response digest does not match its bytes")
    assertion_content = canonical_bundle_json(assertion)
    observation_content = canonical_bundle_json(observation)
    resolved_model = result.identity.resolved_model or result.identity.requested_model
    model_implementation = (
        f"{result.identity.adapter_id}@{result.identity.adapter_version}:"
        f"{resolved_model}"
    )
    ledger = _ledger(
        assertion,
        observation,
        raw_response_digest=result.raw_response.sha256,
        assertion_digest=_sha256(assertion_content),
        observation_digest=_sha256(observation_content),
        model_implementation=model_implementation,
    )

    return EvidenceBundleBuilder().build(
        destination,
        ledger=ledger,
        title="Structured inference to externally verified evidence",
        created_at=CREATED_AT + timedelta(seconds=14),
        files=(
            EvidenceBundleFile(
                path="inputs/incident.json",
                content=_pretty_json(_incident()),
                media_type="application/json",
                role=EvidenceBundleFileRole.INPUT,
            ),
            EvidenceBundleFile(
                path=RAW_RESPONSE_PATH,
                content=raw_response_content,
                media_type=result.raw_response.media_type,
                role=EvidenceBundleFileRole.ARTIFACT,
                record_ref=RAW_RESPONSE_RECORD_ID,
            ),
            EvidenceBundleFile(
                path=ASSERTION_PATH,
                content=assertion_content,
                media_type="application/json",
                role=EvidenceBundleFileRole.ARTIFACT,
                record_ref=ASSERTION_RECORD_ID,
            ),
            EvidenceBundleFile(
                path=OBSERVATION_PATH,
                content=observation_content,
                media_type="application/json",
                role=EvidenceBundleFileRole.ARTIFACT,
                record_ref=OBSERVATION_RECORD_ID,
            ),
        ),
        limitations=(
            "The model response is a deterministic reviewed fixture, not a hosted call.",
            "The cache-bypass verifier is authoritative only in this fixture.",
            "Structural integrity does not establish truth outside the declared scope.",
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--private-artifacts", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the example and print the verified bundle identity."""

    args = _parser().parse_args(argv)
    destination = cast(Path, args.output)
    private_artifacts = cast(Path, args.private_artifacts)
    verified = run_example(
        destination,
        private_artifact_root=private_artifacts,
    )
    print(
        f"PASS {destination}: {verified.manifest['bundle_id']} "
        f"({len(verified.ledger)} records)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
