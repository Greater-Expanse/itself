# Typed Protocol Record Construction

Itself's wire format remains ordinary JSON. The typed construction layer helps
Python applications create that JSON without hand-authoring nested dictionaries
or duplicating the protocol vocabulary.

Each constructor:

- accepts enums and typed value objects for protocol-controlled fields;
- normalizes timezone-aware timestamps to UTC;
- rejects values that cannot be represented as strict JSON;
- validates the completed record against the canonical protocol schema;
- returns a defensive `JsonObject` ready for `Ledger`, `JsonlLedgerStore`, or
  another language-neutral consumer.

## Build a hypothesis

```python
from datetime import UTC, datetime

from itself import (
    Actor,
    ActorRole,
    ActorType,
    ClaimStatus,
    Ledger,
    PredictionCondition,
    RecordHeader,
    Scope,
    hypothesis_record,
    prediction_record,
    status_transition_record,
)

created_by = Actor(
    actor_id="incident-diagnostician",
    actor_type=ActorType.MODEL,
    role=ActorRole.PROPOSER,
    implementation_ref="organization/model@2026-07",
)
scope = Scope(
    description="Incident 184 in the staging environment",
    dimensions={"incident_id": "184", "environment": "staging"},
)

hypothesis = hypothesis_record(
    RecordHeader(
        record_id="hypothesis-cache-key",
        created_at=datetime(2026, 7, 23, 12, tzinfo=UTC),
        created_by=created_by,
    ),
    statement="The proxy cache key omits the source revision.",
    scope=scope,
    prediction_refs=("prediction-cache-bypass",),
)
testable = status_transition_record(
    RecordHeader(
        record_id="transition-cache-testable",
        created_at=datetime(2026, 7, 23, 12, 0, 1, tzinfo=UTC),
        created_by=created_by,
    ),
    subject_ref="hypothesis-cache-key",
    from_status=ClaimStatus.PROPOSED,
    to_status=ClaimStatus.TESTABLE,
    authorized_by=created_by,
    reason="The hypothesis has a falsifiable prediction.",
)
prediction = prediction_record(
    RecordHeader(
        record_id="prediction-cache-bypass",
        created_at=datetime(2026, 7, 23, 12, 0, 2, tzinfo=UTC),
        created_by=created_by,
    ),
    hypothesis_ref="hypothesis-cache-key",
    condition=PredictionCondition.HYPOTHESIS_TRUE,
    expected_observation="Bypassing the proxy returns the current revision.",
    falsified_when="The bypass still returns the stale revision.",
    scope=scope,
)

ledger = Ledger((hypothesis, testable, prediction))
```

The constructor validates each record independently. `Ledger` then validates
cross-record references, kinds, ordering constraints, and replayed state.

## Typed value objects

The repeated nested structures are represented by immutable typed values:

- `Actor` and `RecordHeader` for provenance;
- `Scope` for interpretation boundaries;
- `Digest` for artifact identity;
- `Authority` for the issuer and basis of evidence or verdicts;
- `Oracle` for test-evaluator declarations;
- `EvidenceRelation` for evidence-to-subject semantics;
- `TestCost` for observable resource use.

Protocol vocabularies use `StrEnum` classes such as `ClaimType`,
`EvidenceType`, `TestDesign`, `VerdictOutcome`, and `DecisionDisposition`.
Existing `ActorType`, `ActorRole`, and `ClaimStatus` enums are shared with the
state-transition engine.

## Record constructors

The public constructor set mirrors every protocol record kind:

| Wire kind | Python constructor |
|---|---|
| `artifact_reference` | `artifact_reference_record` |
| `claim` | `claim_record` |
| `hypothesis` | `hypothesis_record` |
| `prediction` | `prediction_record` |
| `test` | `protocol_test_record` |
| `evidence` | `evidence_record` |
| `verdict` | `verdict_record` |
| `decision` | `decision_record` |
| `status_transition` | `status_transition_record` |

`status_transition_record` also applies the reference SDK's transition policy.
It rejects disallowed state edges, evidence-backed promotion without evidence
or a matching verdict, unauthorized roles, model-authorized promotion, and
corroboration without a policy reference before a ledger append is attempted.

## Deliberate boundaries

The construction layer does not:

- generate identifiers or timestamps;
- infer scope, authority, or evidence relations;
- turn model output into evidence;
- establish that an assertion or evidence source is correct;
- resolve cross-record references before records enter a ledger;
- replace the canonical JSON Schema.

Explicit identifiers and time are intentional provenance inputs. Applications
can place their own clock and identifier strategy above these helpers without
changing the protocol representation.
