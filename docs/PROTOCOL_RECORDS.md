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

## Restrict who may authorize evidence-backed transitions

Every record declares its own actors, and the protocol binds no identifier to
an identity, so a validator cannot tell whether an identifier that calls itself
`software` belongs to a policy engine or to a model. When a deployment knows
which actor ids may settle claims, it can list them in a `ValidationPolicy`.
The policy is a check of the reference SDK, outside `v0alpha2` conformance, and
without one the validators accept exactly what the protocol accepts:

```python
from itself import (
    BundleValidator,
    EvidenceBundleValidator,
    Ledger,
    ValidationPolicy,
)

policy = ValidationPolicy(trusted_authorizers={"release-policy", "reviewer-alex"})
trusted_ledger = Ledger(validator=BundleValidator(policy=policy))
bundle_validator = EvidenceBundleValidator(policy=policy)
```

`trusted_authorizers=` on either validator is shorthand for a policy with only
that list, and passing it together with `policy=` raises `ValueError`.

With a list, the validators refuse a transition whose `authorized_by` declares
any other id when it moves a subject into or out of an evidence-backed status,
one of `EVIDENCE_BACKED_STATUSES`: `supported`, `refuted`, `inconclusive`,
`mixed_evidence`, and `corroborated_within_scope`. The refusal is reported as
`untrusted_authorizer`, and the transition does not apply. So an unlisted actor
can neither promote a claim nor demote a supported one to `stale` or
`invalidated`. Transitions between the other states, such as `testable` to
`under_test`, are not restricted, and an empty list refuses every transition
into or out of an evidence-backed status.

A list also refuses an actor id declared with more than one `actor_type`, in
any record's `created_by` or any decision's or transition's `authorized_by`.
The refusal is reported once per id as `actor_type_conflict`, on the first
record where the second type appears. Otherwise, a model could
claim a listed software id in its own records and the trust list would not
notice.

The list compares the ids that records declare, and it establishes no
identity. Any writer that can append records can declare a trusted id, so the
list protects a ledger only when the write path controls which writers emit
records under those ids, for example a service that appends every
evidence-backed transition itself. On the command line, pass
`--trusted-authorizer ID` once per id to the `ledger`, `receipt`, and `bundle`
commands.

## Require supported reliance and unexpired support

A policy has two more opt-in checks, which also apply to every validator that
accepts one:

```python
from datetime import UTC, datetime

policy = ValidationPolicy(
    require_supported_reliance=True,
    as_of=datetime(2026, 8, 1, tzinfo=UTC),
)
```

`require_supported_reliance` refuses an `approved` or `executed` decision whose
`relied_on_claim_refs` names a claim whose replayed status, at the decision's
position in the history, is anything other than `supported` or
`corroborated_within_scope`. The refusal is reported as `unsupported_reliance`.
A later promotion does not repair an earlier decision, and references declared
external are skipped, because their status is not in the history.

`as_of`, a timezone-aware datetime, checks each subject that is `supported` or
`corroborated_within_scope` at the end of the history. It reports
`expired_support` when the subject's own scope `valid_until` is at or before
`as_of`. It does the same for any evidence cited by the transition that gave
the subject its current status, when that evidence's `expires_at` or scope
`valid_until` is at or before `as_of`. Timestamps are compared as instants,
whatever their offsets. A naive datetime raises `ValueError`. On the command line, the same checks are
`--require-supported-reliance` and `--as-of TIMESTAMP`, an RFC 3339 date-time
with an offset, on every command that accepts `--trusted-authorizer`.

## Record a test and its evidence

A planned test and its execution are separate immutable records. The completed
record names its plan in `plan_ref` and must cite the evidence it produced in
`evidence_refs`. The schema rejects a `completed` test without evidence, and
the constructor reports `$: 'evidence_refs' is a required property`.

The evidence names the completed test in `test_ref`, so the two records
reference each other. References resolve against the complete candidate
history, so appending either record alone fails with `unresolved_reference`.
Records that reference each other must enter in one `Ledger.extend` or
`JsonlLedgerStore.extend` batch, in either order.

Continuing the hypothesis example:

```python
from itself import (
    Authority,
    AuthorityType,
    EvidenceRelation,
    EvidenceRelationType,
    EvidenceType,
    Oracle,
    TestDesign,
    TestStatus,
    VerdictOutcome,
    evidence_record,
    protocol_test_record,
    verdict_record,
)

operator = Actor("ci-runner", ActorType.SOFTWARE, ActorRole.OPERATOR)
evaluator = Actor("bypass-checker", ActorType.SOFTWARE, ActorRole.EVALUATOR)
oracle = Oracle("revision-equality", "1", AuthorityType.DETERMINISTIC_TOOL)
authority = Authority(
    AuthorityType.DETERMINISTIC_TOOL,
    actor_ref="bypass-checker",
    basis="Exact comparison of the returned and current revisions",
)


def header(record_id: str, second: int, actor: Actor) -> RecordHeader:
    return RecordHeader(
        record_id=record_id,
        created_at=datetime(2026, 7, 23, 12, 0, second, tzinfo=UTC),
        created_by=actor,
    )


plan = protocol_test_record(
    header("test-cache-bypass-plan", 3, operator),
    question="Does bypassing the proxy return the current revision?",
    design=TestDesign.DETERMINISTIC_CHECK,
    status=TestStatus.PLANNED,
    oracle=oracle,
    scope=scope,
    subject_refs=("hypothesis-cache-key",),
    prediction_refs=("prediction-cache-bypass",),
)
under_test = status_transition_record(
    header("transition-cache-under-test", 4, operator),
    subject_ref="hypothesis-cache-key",
    from_status=ClaimStatus.TESTABLE,
    to_status=ClaimStatus.UNDER_TEST,
    authorized_by=operator,
    reason="The planned bypass check is running.",
)
ledger.extend((plan, under_test))

run = protocol_test_record(
    header("test-cache-bypass-run", 5, operator),
    question="Does bypassing the proxy return the current revision?",
    design=TestDesign.DETERMINISTIC_CHECK,
    status=TestStatus.COMPLETED,
    oracle=oracle,
    scope=scope,
    subject_refs=("hypothesis-cache-key",),
    prediction_refs=("prediction-cache-bypass",),
    plan_ref="test-cache-bypass-plan",
    evidence_refs=("evidence-cache-bypass",),
)
evidence = evidence_record(
    header("evidence-cache-bypass", 6, evaluator),
    evidence_type=EvidenceType.DETERMINISTIC_TEST,
    relations=(
        EvidenceRelation("hypothesis-cache-key", EvidenceRelationType.SUPPORTS),
    ),
    authority=authority,
    scope=scope,
    test_ref="test-cache-bypass-run",
    result={"returned_current_revision": True},
)
ledger.extend((run, evidence))

verdict = verdict_record(
    header("verdict-cache-key", 7, evaluator),
    subject_ref="hypothesis-cache-key",
    outcome=VerdictOutcome.SUPPORTED,
    evidence_refs=("evidence-cache-bypass",),
    authority=authority,
    scope=scope,
    public_rationale="Bypassing the proxy returned the current revision.",
)
supported = status_transition_record(
    header("transition-cache-supported", 8, evaluator),
    subject_ref="hypothesis-cache-key",
    from_status=ClaimStatus.UNDER_TEST,
    to_status=ClaimStatus.SUPPORTED,
    authorized_by=evaluator,
    reason="The deterministic check supported the scoped hypothesis.",
    evidence_refs=("evidence-cache-bypass",),
    verdict_ref="verdict-cache-key",
)
ledger.extend((verdict, supported))
```

The hypothesis replays to `ClaimStatus.SUPPORTED`. A verdict and its
transition may also arrive in separate appends, because each only cites
records that precede it.

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
