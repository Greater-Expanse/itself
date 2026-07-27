# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

"""Static assertions for the installed package's public typing contract."""

from datetime import UTC, datetime
from pathlib import Path
from typing import assert_type

from itself import (
    Actor,
    ActorRole,
    ActorType,
    Authority,
    AuthorityType,
    BundleSnapshot,
    BundleValidator,
    ClaimStatus,
    DirectoryArtifactSink,
    EnvironmentCredential,
    EvidenceBundleBuilder,
    EvidenceBundleFile,
    EvidenceBundleFileRole,
    EvidenceBundleLimits,
    EvidenceBundleValidator,
    IntegrityIssue,
    JsonlLedgerStore,
    JsonObject,
    JsonReceiptStore,
    JsonValue,
    Ledger,
    LedgerSummary,
    OpenAICompatibleEndpoint,
    ProtocolValidator,
    ReasoningReceiptValidator,
    RecordHeader,
    Scope,
    StructuredInferenceAdapter,
    StructuredInferenceClient,
    StructuredInferenceResult,
    StructuredOutputProfile,
    SubjectReplay,
    TransitionRequest,
    VerifiedEvidenceBundle,
    build_reasoning_receipt,
    canonical_ledger_bytes,
    hypothesis_record,
    ledger_sha256,
    replay_subjects,
    summarize_ledger,
    validate_transition,
)

record: JsonObject = {
    "protocol_version": "0.1.0-alpha.3",
    "kind": "claim",
}
json_value: JsonValue = record
validator = ProtocolValidator()

assert_type(validator.errors(json_value), list[str])
assert_type(validator.is_valid(json_value), bool)
assert_type(validator.validate(json_value), None)

records: list[JsonObject] = []
assert_type(BundleValidator().validate(records), BundleSnapshot)
assert_type(BundleValidator().errors(records), list[IntegrityIssue])

ledger = Ledger(records)
assert_type(ledger.records, tuple[JsonObject, ...])
assert_type(ledger.snapshot, BundleSnapshot)
assert_type(ledger.append(record), BundleSnapshot)
assert_type(
    JsonlLedgerStore("ledger.jsonl").load(max_records=100, max_bytes=1024),
    Ledger,
)
assert_type(replay_subjects(ledger), tuple[SubjectReplay, ...])
assert_type(summarize_ledger(ledger), LedgerSummary)
assert_type(summarize_ledger(ledger).to_json_object(), JsonObject)
assert_type(build_reasoning_receipt(ledger), JsonObject)
assert_type(canonical_ledger_bytes(ledger), bytes)
assert_type(ledger_sha256(ledger), str)
assert_type(ReasoningReceiptValidator().is_valid(record), bool)
assert_type(JsonReceiptStore("receipt.json").load(max_bytes=1024), JsonObject)
bundle_limits = EvidenceBundleLimits(
    max_file_bytes=1024,
    max_total_bytes=4096,
)
assert_type(
    EvidenceBundleValidator(limits=bundle_limits).validate("bundle"),
    VerifiedEvidenceBundle,
)
bundle_file = EvidenceBundleFile(
    path="inputs/request.json",
    content=b"{}",
    media_type="application/json",
    role=EvidenceBundleFileRole.INPUT,
)
assert_type(
    EvidenceBundleBuilder().build(
        "bundle",
        ledger=ledger,
        title="Typed bundle",
        files=(bundle_file,),
    ),
    VerifiedEvidenceBundle,
)

proposer = Actor(
    actor_id="example-model",
    actor_type=ActorType.MODEL,
    role=ActorRole.PROPOSER,
)
record_header = RecordHeader(
    record_id="hypothesis-example",
    created_at=datetime(2026, 7, 23, 12, tzinfo=UTC),
    created_by=proposer,
)
record_scope = Scope(description="One typed API example")
assert_type(
    hypothesis_record(
        record_header,
        statement="The cache key omits a relevant input.",
        scope=record_scope,
    ),
    JsonObject,
)
assert_type(
    Authority(
        authority_type=AuthorityType.DETERMINISTIC_TOOL,
        actor_ref="example-oracle",
        basis="Exact comparison",
    ).to_json_object(),
    JsonObject,
)

endpoint = OpenAICompatibleEndpoint(
    actor_id="example-model",
    base_url="https://models.example.test/v1",
    model="example/model",
    credential=EnvironmentCredential("EXAMPLE_API_KEY"),
)
profile = StructuredOutputProfile(
    profile_id="example-profile",
    schema_name="example_output",
    system_prompt="Return one JSON object.",
    user_instruction="Process the supplied input.",
    input_label="INPUT",
    schema={"type": "object"},
)
inference_client = StructuredInferenceClient(
    endpoint=endpoint,
    artifact_sink=DirectoryArtifactSink(Path("artifacts")),
)
inference_adapter: StructuredInferenceAdapter = inference_client
assert_type(
    inference_adapter.invoke(record, profile),
    StructuredInferenceResult,
)

transition = TransitionRequest(
    subject_ref="claim-1",
    from_status=ClaimStatus.PROPOSED,
    to_status=ClaimStatus.TESTABLE,
    authorized_by="actor-1",
    actor_type=ActorType.HUMAN,
    actor_role=ActorRole.REVIEWER,
)
assert_type(validate_transition(transition), None)
