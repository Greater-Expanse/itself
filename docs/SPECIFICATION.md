# Claim and Evidence Protocol `v0alpha2`

## 1. Purpose

The Claim and Evidence Protocol defines a minimal interoperable representation
for the externally inspectable epistemic state of AI agents and AI-driven
workflows.

It enables different models, agents, tools, evaluators, and human-review systems to exchange records without assuming that generated explanations are verified, that all evidence has equal authority, or that private model reasoning is available.

The canonical machine-readable definition is the published
[`protocol.schema.json`](https://greaterexpanse.com/itself/schemas/v0alpha2/protocol.schema.json).
The repository includes the same
[schema source](../src/itself/schemas/v0alpha2/protocol.schema.json) for offline
use.

## 2. Normative language

The terms **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** describe protocol requirements.

This is a pre-1.0 specification. Compatibility may break before a stable `v1`
release, and every incompatible format change receives a new version.

## 3. Core records

### Artifact reference

A content-addressable pointer to an external source, trace, report, test artifact, or other material. Implementations SHOULD store references and digests rather than duplicating sensitive content throughout the ledger.

### Claim

A proposition that may be used in an analysis or decision. A claim MUST state its type, current epistemic status, and applicable scope.

### Hypothesis

A candidate explanation for observations or failures. A hypothesis SHOULD identify alternatives and predictions capable of distinguishing it from those alternatives.

### Prediction

An expected observable result under a stated condition. Predictions make hypotheses testable without treating generated explanations as evidence.

### Test

A planned or completed intervention, query, replay, calculation, review, or other evidence-producing operation. A test MUST declare its design and oracle or evaluator.

In an append-only history, a completed execution MUST NOT overwrite its planned
test record. It SHOULD be emitted as a new test record whose `plan_ref` points to
the immutable plan and whose `evidence_refs` identify the resulting evidence.

### Evidence

An externally inspectable result related to one or more claims. Evidence MUST declare its type, relation to its subjects, source authority, and scope.

### Verdict

A scoped assessment issued from specified evidence under a stated policy. `Verdict` is provisional terminology and does not imply final or universal truth.

### Decision

An accountable disposition or action. Authorization to act is not evidence that the decision's factual premises are true.

### Status transition

An append-only record changing the epistemic state of a claim or hypothesis.
Evidence-backed transitions MUST cite evidence, the verdict that assessed it,
an authorizing actor, and the applicable policy when required.

## 4. Evidence types

`v0alpha2` defines a deliberately small vocabulary:

- `observation` — a directly recorded event or measurement;
- `deterministic_test` — an executable check with deterministic semantics;
- `benchmark_result` — aggregate or per-case task-performance evidence;
- `calculation` — a reproducible computation;
- `database_query` — a result from a declared data source and query;
- `simulation` — output from a declared model of an environment;
- `source_record` — evidence that a source contains or published material;
- `model_evaluation` — a model-applied rubric or judgment;
- `human_review` — an accountable human assessment;
- `policy_check` — an evaluation against a defined organizational policy;
- `other` — an extension whose semantics MUST be described.

Evidence type limits what a record can establish. For example, a benchmark result may support a claim about observed task performance but does not automatically establish a causal mechanism.

## 5. Authority

Every evidence record and verdict declares an authority type:

- `deterministic_tool`;
- `domain_evaluator`;
- `model_evaluator`;
- `human_reviewer`;
- `policy_engine`;
- `external_source`.

Authority describes the issuer and basis of a record. It is not a universal ranking of trustworthiness. Implementations MUST NOT silently convert authority, organizational permission, or model confidence into factual correctness.

## 6. Epistemic states

```text
proposed
    ├──→ testable
    ├──→ blocked
    └──→ withdrawn

testable
    ├──→ under_test
    ├──→ blocked
    └──→ withdrawn

under_test
    ├──→ supported
    ├──→ refuted
    ├──→ inconclusive
    └──→ mixed_evidence

supported
    ├──→ corroborated_within_scope
    ├──→ under_test
    ├──→ mixed_evidence
    ├──→ stale
    └──→ invalidated

refuted, inconclusive, mixed_evidence,
corroborated_within_scope, blocked, or stale
    └──→ may be reopened only through an allowed transition
```

`corroborated_within_scope` is intentionally scoped. The protocol contains no universal `true` status.

Every claim or hypothesis MUST enter an ordered append-only bundle with
`status: proposed`. Consumers derive current state by replaying subsequent
status-transition records; they MUST NOT seed a base record with an already
promoted state or rewrite it to conceal the transition history.

The reference SDK requires evidence and a matching verdict for transitions to
`supported`, `refuted`, `inconclusive`, `mixed_evidence`, or
`corroborated_within_scope`. The verdict MUST assess the same subject, exact
evidence set, scope, outcome, and policy represented by the transition.
Supporting and refuting verdicts require correspondingly related evidence;
`mixed_evidence` requires both relations. The transition also requires an
evaluator, reviewer, or authority role.

A model MAY issue `model_evaluation` evidence, but a model actor MUST NOT authorize an evidence-backed transition. Authorization must come from a human, organization, or software actor applying an explicit evaluation or policy. This does not make the authorizing actor correct; it prevents model output from silently promoting itself. A proposer cannot promote its own claim merely by generating additional text.

## 7. Provenance

Records MUST have stable identifiers, timestamps, and creating actors. Evidence SHOULD cite immutable artifact references with content digests when possible.

Transformations such as extraction, summarization, redaction, or format conversion SHOULD produce new artifact references connected to their inputs rather than overwriting the source record.

The protocol is append-oriented. Historical evidence and transitions SHOULD remain available even after a claim is refuted, invalidated, or reopened.

## 8. Invalidation

Claims and evidence MAY declare dependencies and invalidation conditions. Implementations SHOULD reopen or mark affected claims stale when:

- an evidence source expires or is superseded;
- a referenced model, harness, or environment version changes;
- a required evaluator is found to be defective;
- contradictory evidence appears;
- the original scope no longer applies.

Invalidation is not deletion. It records that prior support can no longer be relied upon under the current conditions.

## 9. Privacy and data minimization

The protocol MUST NOT require private chain-of-thought. Implementations SHOULD capture externally meaningful artifacts such as claims, predictions, tool events, tests, public rationales, and source references.

Implementations SHOULD support:

- content-addressed references instead of embedded sensitive content;
- redaction before external model or evaluator access;
- access control independent of epistemic status;
- configurable retention and deletion of source artifacts;
- minimized receipts that reveal only decision-relevant evidence;
- separation between public rationales and private operational traces.

## 10. Threat model

The first implementation assumes that any participant may be incomplete or wrong, including the proposer, evidence source, adapter, evaluator, policy, and human reviewer.

Primary threats include:

- a proposer promoting its own conjecture;
- weak model judgments laundered as verification;
- tampered or stale source artifacts;
- an adapter claiming authority outside its declared scope;
- benchmark optimization mistaken for causal understanding;
- hidden conflicts between evaluator and proposer;
- fabricated provenance;
- sensitive trace disclosure;
- deletion of contradictory or negative evidence;
- policy changes applied retroactively without an audit record.

`v0alpha2` provides representation and basic transition enforcement. It does
not provide cryptographic identity, secure execution, or a universally correct
oracle.

## 11. Conformance

An implementation conforms to `v0alpha2` when it:

1. accepts every fixture under `conformance/valid`;
2. rejects every fixture under `conformance/invalid`;
3. enforces the documented status-transition graph;
4. preserves unknown extension data only in fields explicitly permitting it;
5. does not present a provider-specific model judgment as a stronger evidence type;
6. does not require raw chain-of-thought to produce a valid record.

Conformance establishes protocol behavior, not the truth of any claim or correctness of any evaluator.

The repository continuously exercises these fixtures through both the Python
reference implementation and an independent JavaScript validator. The
JavaScript check compiles this canonical schema directly and independently
implements ordered bundle integrity and replay; it does not call the Python
package.

### 11.1 Ordered bundle integrity

The reference SDK also defines a deterministic integrity profile
for ordered record bundles. A conforming bundle validator MUST:

1. validate every record against the canonical schema;
2. reject duplicate record identifiers;
3. resolve local references and enforce their expected record kinds;
4. require a verdict's subject, evidence, and policy to appear before the
   verdict;
5. require verdict and evidence scope to exactly match subject scope;
6. enforce evidence-relation semantics for each verdict outcome;
7. require a transition subject to appear before its transition;
8. require cited evidence, verdict, and policy artifacts to exist before the
   transition they authorize;
9. replay transitions from each claim or hypothesis's `proposed` state;
10. reject a transition whose declared `from_status` differs from replayed
    state;
11. require the authorizing verdict to match the transition's subject, outcome,
    exact evidence set, and policy.

An unresolved identifier is permitted only when the caller explicitly declares
it external to the bundle. A transition's subject, cited evidence, verdict, and
policy artifact are exceptions: they MUST be locally inspectable in the ordered
bundle. Closed-bundle conformance permits no unresolved references.

Bundle integrity establishes that a history is internally coherent and
replayable. It does not establish that evidence is genuine, an oracle is
correct, or a claim is true.

### 11.2 Local JSON Lines profile

The reference local ledger stores one protocol record per UTF-8 JSON line. File
order is authoritative replay order. Blank lines and non-object JSON values are
invalid. Readers reject duplicate object keys, lone Unicode surrogates, negative
zero, numeric overflow or underflow, unsafe integer literals, `NaN`, and
infinities. Protocol numbers remain inside the interoperable IEEE 754 range
defined by the schema.

Writers emit each record using
[RFC 8785 JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785)
followed by one LF byte. Reasoning-receipt ledger digests are computed over
those exact JSON Lines bytes.

An append or batch append MUST validate the complete candidate history before
mutating in-memory or persisted state. The reference store writes a replacement
file in the destination directory and atomically replaces the previous file only
after validation and file synchronization succeed.

This profile provides deterministic local persistence and failure atomicity for
a single writer. It does not define concurrent-write coordination,
cryptographic tamper evidence, remote durability, or database transactions.

## 12. Non-goals for `v0alpha2`

- a universal ontology for every evidence domain;
- a scalar truth, trust, or reliability score;
- a universal verifier;
- a replacement for domain experts;
- storage or transport standardization beyond JSON records;
- capture of private model reasoning;
- permanent standardization before benchmark use.
