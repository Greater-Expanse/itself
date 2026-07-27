# Case 001: Omitted Revision in a Cache Key

## Status

The deterministic environment, separate behavioral and causal oracles,
protocol-instrumented reference run, provider-neutral model boundary, and
manifest-bound comparison runner are implemented and tested. The first
provider-controlled repeated study completed on 2026-07-24: 15 registered v5
runs produced 12 complete comparisons and three preserved model-output
failures. See the
[current result](../results/current/2026-07-24-case-001-comparison-v5.md).

This remains one transparent reference incident. Repeated execution validates
the experiment and enforcement machinery; it does not establish cross-case
causal-reasoning ability or improved model accuracy.

## Purpose

This case tests whether the protocol and enforcement layer preserve the
difference between:

1. a plausible causal diagnosis;
2. a structured but untested causal diagnosis;
3. a diagnosis supported by a discriminating external intervention.

It is intentionally small and deterministic. Its first implementation is meant
to validate the experimental machinery, not establish a general result about
language-model reasoning.

## Workflow under diagnosis

A report-generation workflow has five components:

```text
source store
    -> report worker
    -> caching proxy
    -> artifact sink
    -> verifier
```

The workflow is warmed using source revision `A`. The source then advances to
revision `B`, after which six ordinary report-generation attempts continue to
produce and verify revision `A`.

The externally visible failure is:

```text
expected source revision: B
verified report revision: A
task result: failure
```

The diagnostician receives component descriptions, the six attempt records, and
the available intervention catalog. It does not receive the configured failure
mechanism through the run interface.

## Injected mechanism

The caching proxy keys report responses by `report_id` but omits
`source_revision`. Once revision `A` is cached, a request for the same report at
revision `B` receives the stale response.

Ground-truth identifier:

```text
cache_key_omits_source_revision
```

The ground truth belongs to the environment and causal oracle. It appears in the
closed-set request only as one of three symmetric candidate hypotheses; the
request must not identify which candidate is configured as ground truth.

## Competing hypotheses

The case has three deliberately plausible explanations.

### H1: Cache-key omission

The proxy reuses a cached response because its key omits `source_revision`.

### H2: Worker reads stale source

The report worker's source-store view remains at revision `A`, so even a fresh
worker execution produces stale content.

### H3: Verifier reads stale artifact

The worker and proxy produce revision `B`, but the verifier reads a previous
artifact path containing revision `A`.

The initial observation is consistent with all three. Frequency, fluency, or
confidence cannot distinguish them.

## Intervention matrix

`A` and `B` below denote the revision visible in the final observation.

| Intervention | H1 prediction | H2 prediction | H3 prediction |
|---|---:|---:|---:|
| unchanged replay | A | A | A |
| bypass caching proxy | B | A | A |
| invoke worker and inspect response directly | B | A | B |
| write and verify a fresh artifact path | A | A | B |

The prediction vectors are therefore:

```text
H1 = [A, B, B, A]
H2 = [A, A, A, A]
H3 = [A, A, B, B]
```

Every pair differs. `bypass caching proxy` is the cheapest single intervention
that distinguishes H1 from both alternatives when it yields `B`. Other outcomes
retain multiple possibilities and should trigger another test rather than a
forced verdict.

## Behavioral and causal oracles

The case keeps two evaluators separate.

### Behavioral oracle

The report passes when its verified revision equals the current source revision.
This oracle can establish whether an intervention restored task behavior.

### Causal oracle

The causal oracle compares intervention outcomes with the registered mechanism
and the predeclared prediction matrix. It evaluates whether the selected
hypothesis predicts the observations and whether the intervention isolates that
mechanism from the alternatives.

A passing report is not automatically a correct causal diagnosis. For example,
directly inserting revision `B` into the output would satisfy the behavioral
oracle while providing no evidence that the cache key caused the original
failure.

## Experimental conditions

The first study compares three conditions using the same observation budget and
case description.

The implementation-level comparison unit, shared controls, paired branching,
scorecard semantics, and pilot acceptance criteria are frozen separately in the
[comparative study design](COMPARATIVE_STUDY.md). In particular, Conditions B
and C share one test-plan model call so that running the external check is not
confounded with a different model sample.

### Condition A: Ordinary AI answer

The AI assistant receives the six failed attempts and answers the familiar
engineering question, “What is the most likely cause, and why?” No diagnostic
check is executed.

Record:

- selected mechanism;
- whether it matches ground truth;
- expressed confidence if supplied;
- cost and latency.

The explanation is never labeled verified.

### Condition B: Testable investigation plan without running the check

The AI assistant emits an investigation plan containing plausible causes,
predicted observations, and a selected diagnostic check, but the application
does not execute that check.

Record:

- whether all material alternatives were retained;
- whether predictions discriminate among them;
- selected primary hypothesis;
- any attempted evidence-backed promotion;
- cost and latency.

The enforcement layer must leave causal hypotheses at `proposed`, `testable`, or
`under_test`. Merely completing the schema cannot promote them.

### Condition C: Investigation plan checked against external evidence

The AI assistant emits alternatives and predictions and selects a diagnostic
check. The application runs the check outside the model and records the
resulting observation. A deterministic policy may promote a cause only when:

- the test and prediction were recorded before execution;
- the result is emitted as evidence by the environment adapter;
- the evidence explicitly relates to the hypothesis;
- the observed outcome matches the recorded prediction;
- the intervention distinguishes the hypothesis from remaining alternatives;
- an eligible non-model policy actor authorizes the transition.

If the result remains compatible with multiple hypotheses, the valid outcome is
`inconclusive` or another test—not forced support.

## Primary measurements

The first implementation reports a scorecard rather than a composite score.

1. **Ground-truth identification** — whether the selected mechanism is H1.
2. **Ground-truth retention** — whether H1 remains among viable alternatives.
3. **False promotion** — unsupported transitions to an evidence-backed state.
4. **Test discrimination** — whether the selected intervention separates the
   leading hypothesis from live alternatives.
5. **Prediction correctness** — whether predeclared predictions match results.
6. **Causal evidence validity** — whether cited evidence is relevant, available
   before promotion, and emitted by the declared deterministic adapter.
7. **Behavioral recovery** — whether the workflow passes after intervention or
   patching, scored separately from causal identification.
8. **Cost** — interventions, environment executions, wall time, model tokens,
   and human review time where applicable.
9. **Reconstructability** — whether a reviewer can derive why the final state
   was permitted from public records without private chain-of-thought.

## Reference implementation phases

### Phase 1: Deterministic environment

**Status:** implemented.

- implement source store, worker, proxy, sink, and verifier components;
- inject exactly one of the three mechanisms;
- expose the four declared interventions;
- implement separate behavioral and causal oracles;
- prove the prediction matrix with parameterized tests.

### Phase 2: Protocol-instrumented scripted run

**Status:** implemented.

- encode all observations, hypotheses, predictions, tests, and evidence;
- demonstrate rejection of promotion before evidence;
- execute the discriminating intervention;
- produce a valid JSON Lines ledger ending in a scoped verdict;
- show a behavior-restoring but causally irrelevant patch as a negative control.

The scripted run proves wiring and policy behavior only. It is not a model
evaluation.

The implementation produced four concrete engineering findings:

- planned tests and completed executions need distinct immutable records linked
  by `plan_ref`, so an execution cannot silently rewrite its preregistered plan;
- a completed test and its mutually referencing evidence must be admitted as
  one atomic ledger batch;
- valid structured hypotheses still cannot enter an evidence-backed state until
  earlier, relevant evidence and an eligible authorizer are present;
- behavioral recovery and causal repair require separate measurements: the
  negative-control output patch passes the task oracle without addressing the
  injected mechanism.

### Phase 3: Replaceable diagnostician interface

**Status:** implemented for the closed-set reference case.

- define a provider-neutral input and output contract;
- add a deterministic fixture diagnostician for conformance;
- add model adapters only after the contract is stable;
- preserve raw provider output as an artifact reference, not as authoritative
  causal evidence.

The canonical JSON request supplies observations, candidate hypotheses, and
opaque test options. The result may retain candidates, make predictions, select
one listed test, and provide a public explanation. It cannot request arbitrary
tool execution, emit evidence, or assign verdicts. A typed Python `Protocol`
mirrors this language-neutral boundary.

The implementation established five additional findings:

- requests assembled under all three hidden mechanism configurations are
  byte-identical, so the evaluator configuration does not leak through this API;
- cross-reference checks are semantic, not merely structural: retained,
  primary, prediction, and selected-test identifiers must resolve coherently;
- captured diagnostician output enters the ledger as an artifact dependency of
  proposed hypotheses, never as authorizing evidence;
- a wrong primary diagnosis can be overturned by the deterministic external
  result and evaluator;
- a conforming diagnostician that selects a non-discriminating replay leaves all
  compatible hypotheses `inconclusive` rather than forcing support.

The installable generic OpenAI-compatible inference adapter now implements this
boundary. Early single-attempt observations established that the adapter could
capture a conforming assertion and carry one hosted response through the
evidence path. The deterministic fixture continues to validate authority
separation without simulating model behavior. The comparison runner now applies
the same boundary to repeated model calls and preserves both completed and
failed attempts.

### Phase 4: Repeated model trials

**Status:** implemented for the first transparent reference case. V2 exposed a
measurement defect, v3 stopped on an instrumentation-integrity defect, v4
completed after that defect was corrected, and the provider-controlled v5
study completed with 15 registered runs. Superseded results remain preserved
under [`../results/archive/`](../results/archive/); v5 is the current result.

- preregister prompts, budgets, sampling settings, and primary metrics;
- run repeated trials rather than one illustrative transcript;
- separate mechanism selection, test selection, updating, and patch success;
- publish negative and inconclusive results;
- avoid using this transparent case as a held-out generalization claim.

Supporting infrastructure includes:

- an offline JSON exchange can emit the exact diagnostician request, ingest a
  schema-valid external result, enforce request-relative references, and execute
  only the selected declared test;
- a minimized reasoning receipt deterministically projects the validated ledger,
  binds it by a canonical SHA-256 digest, and can be verified by recomputing the
  complete projection against the source ledger;
- a generic OpenAI-compatible Chat Completions adapter performs one observable
  attempt, captures the provider response as provenance, and validates the
  model's assertion without repair or retry;
- a versioned machine-readable manifest preregisters and cryptographically
  binds the exact request, schema, adapter, endpoint, rendered payload,
  generation settings, attempt policy, artifact policy, and metrics;
- a verified trial bundle retains a successful evidence history or a sanitized
  adapter failure, with a digest-bound inventory and private raw response;
- the case-specific model runner validates the manifest before network access,
  executes exactly one attempt, feeds a valid assertion into the external test
  and evaluator path, and independently validates the final bundle.

These facilities support provider-neutral, manifest-bound repeated execution
across the three experimental conditions. The remaining research gaps are new
cases and domains, hidden-mechanism variation, model and case holdouts,
purpose-built adapter-conformance studies, reusable verification adapters, and
measurement of effects on human decisions.

## Leakage controls

The executable package should separate public case material from evaluator-only
configuration at the API boundary. Because this repository is open, that
separation prevents accidental runtime leakage but does not make the answer
secret from a model trained on or given the repository.

Future benchmark claims require mechanisms or parameterizations unavailable to
the proposer, plus explicit checks that prompts and artifacts exclude evaluator
configuration.

## Acceptance criteria for phases 1 through 3

- no network or model provider is required;
- the environment is deterministic and resettable;
- all three mechanisms reproduce the common initial failure;
- the tested intervention vectors exactly match the declared matrix;
- the behavioral oracle and causal oracle can disagree in a negative control;
- evaluator-only configuration is absent from the diagnostician observation;
- tests fail if an intervention leaks the configured mechanism directly;
- the same request is produced for every hidden mechanism compatible with the
  baseline observations;
- only a test identifier declared by the runner can be selected;
- raw diagnostician output is represented as provenance rather than evidence;
- a non-discriminating result produces an inconclusive state;
- the implementation introduces no generalized plugin system before this case
  demonstrates what such an interface actually needs.

All criteria above are covered by the deterministic environment, contract, and
runner tests. This is an implementation result, not empirical evidence about
language models.

## What this case cannot establish

Even correct enforcement and successful repeated execution on this case do not
show that:

- language models diagnose causes more accurately under the protocol;
- the abstractions transfer to another software failure or knowledge-work task;
- the deterministic causal oracle is representative of real organizations;
- a structured ledger improves human decisions;
- the protocol resists gaming or fabricated evidence;
- the approach is economical at enterprise scale.

Those are later empirical questions. The immediate goal is smaller: establish a
clean, inspectable experimental unit in which plausible explanation, behavioral
recovery, and causally discriminating evidence are demonstrably different.
