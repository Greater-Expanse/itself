# Case 001 Comparative Study Design

## The experiment in real-world terms

Imagine an engineer asks an AI assistant, “Why does this report keep coming
back stale?” The first call asks for the kind of answer most AI products return
today: choose the most likely root cause and explain why. A separate call asks
for an actionable investigation plan: retain plausible causes, state what each
would predict, and choose the next diagnostic check.

The harness records the investigation plan without testing it, then separately
runs the chosen check in a deterministic environment and lets external evidence
control which cause can be marked supported or refuted. One complete repetition
therefore contains two model calls and three paths: the ordinary answer, the
untested plan, and the evidence-tested plan. Generated reports call this a
**run**; the methodology below calls the same unit a **comparison replicate**.

## Status

V5 completed on 2026-07-24. It registered five runs for each of three model
configurations through one endpoint provider. Twelve runs completed both model
requests and all three application paths; three retained explicit model-output
failures. Every registered slot produced an immutable bundle that passed
Itself's validators.

The [current v5 report](../results/current/2026-07-24-case-001-comparison-v5.md)
is the public result. It demonstrates enforcement, failure preservation, and
reconstructability for this incident. It is not evidence that the workflow
improves model accuracy or transfers to other tasks. Earlier registrations and
superseded results are explained under [Study history](#study-history).

## Question

For the same visible failure history and candidate explanations, what changes
when a model's diagnosis is represented as:

1. an ordinary AI answer that picks a likely cause and explains why;
2. a testable investigation plan whose predictions have not been checked;
3. the same plan after its chosen check runs and external evidence governs what
   the application may conclude?

The immediate objective is to validate a fair, inspectable comparison unit. A
single pilot cannot estimate an effect or establish model reasoning ability.

## Formal experimental unit

Formally, one comparison replicate contains two model calls and three dependent
paths:

```text
same case facts
├── ordinary-answer request ──────────> Condition A
└── test-plan request ────────────────> Condition B
                                  └───> Condition C + external check
```

Conditions B and C must consume the exact same validated investigation plan,
including the same raw-response digest. They are paired paths, not independent
model trials. This isolates the consequence of running the external check and
applying promotion policy from model-sampling variance.

Condition A requires its own model call because removing competing
hypotheses, predictions, and test selection from the output task is the
treatment being studied. It receives the same factual request content and
available option statements as the test-plan call. The order of the two
provider calls must be declared before execution.

## Shared controls

Every model call in a replicate must use the same:

- case version and visible observations;
- workflow-component descriptions;
- candidate hypothesis statements and identifiers;
- test-option descriptions and identifiers;
- provider, endpoint, model, adapter version, temperature, seed policy, token
  ceiling, timeout, and attempt policy;
- credential and raw-artifact handling policy.

The output profile and its corresponding instructions differ by condition and
must have separate schema and rendered-payload digests. Equal token ceilings do
not imply equal token consumption; output length is a reported cost, not a
quantity to normalize after execution.

## Condition A: Ordinary AI answer

The model answers the everyday engineering question, “What is the most likely
cause, and why?” It returns a minimal machine-readable envelope containing:

- one selected hypothesis identifier;
- a public narrative explanation;
- optional expressed confidence.

The JSON envelope exists only to avoid an unregistered parser or a
model-as-grader. The prompt does not request retained alternatives,
predictions, test selection, evidence, or a verdict. The harness may project the
selection into a ledger as one `proposed` hypothesis linked to the captured
output artifact. It must not reinterpret fluent language as evidence or a
supported state.

No intervention is executed. The evaluator may compare the selected identifier
with hidden ground truth for measurement, but that evaluator-only comparison
does not become evidence available to the condition.

## Condition B: Testable investigation plan without running the check

The model returns the existing provider-neutral diagnosis contract as an
actionable investigation plan:

- one or more retained hypothesis identifiers;
- one primary hypothesis;
- one selected declared test;
- exactly one falsifiable prediction for that test for every retained
  hypothesis;
- a public explanation and optional confidence.

The harness records the hypotheses, predictions, and planned test. A retained
hypothesis with a conforming prediction may reach `testable`; none may reach
`under_test`, `supported`, or `refuted`. No intervention is executed and no
test-result evidence or causal verdict exists.

The deterministic evaluator may grade the hidden correctness and
discrimination of the assertion for the scorecard. These evaluator-only
measurements must not be injected into the branch ledger.

## Condition C: Investigation plan checked against external evidence

Condition C begins from the exact validated investigation plan used by
Condition B. The runner executes only the selected declared check. The
environment emits the resulting observation, and the deterministic causal
oracle compares it with predictions recorded before execution.

Only this external evidence and an eligible non-model policy actor may authorize
`supported`, `refuted`, `inconclusive`, or `mixed_evidence`. Model confidence and
explanation fluency play no role in promotion.

## Common scorecard

The comparison reports components, never a composite truth or trust score.
Every branch records:

- whether the model's primary hypothesis is ground truth;
- whether ground truth was retained by the model;
- number of retained hypotheses;
- expressed confidence, when supplied;
- whether a test was selected;
- whether the selected test discriminates the primary from retained
  alternatives;
- number of predictions declared and number correct against the evaluator-only
  matrix;
- number of external tests executed;
- whether external evidence identifies ground truth;
- final epistemic status of the ground-truth hypothesis;
- unsupported promotion attempts and acceptances;
- model input and output tokens, wall time, and ledger size;
- whether the reasoning receipt recomputes.

Fields that do not apply must be represented as `null`, not silently converted
to zero or false. In particular, absence of an external test is different from
a failed external test, and absence of structured predictions is different from
incorrect predictions.

## Registered model matrix

V5 was registered in July 2026 using model families selected as
community-relevant, near-frontier baselines at that time. Fireworks AI is fixed
as controlled infrastructure across the entire cohort.

| Family | Checkpoint status | Registered model identifier |
| --- | --- | --- |
| [GLM 5.2](https://huggingface.co/zai-org/GLM-5.2) | Public weights, MIT | `accounts/fireworks/models/glm-5p2` |
| [MiniMax M3](https://huggingface.co/MiniMaxAI/MiniMax-M3) | Public weights, MiniMax Community License | `accounts/fireworks/models/minimax-m3` |
| [Qwen 3.7 Plus](https://fireworks.ai/blog/qwen-3p7-plus) | Hosted licensed weights; weight export unavailable | `accounts/fireworks/models/qwen3p7-plus` |

Qwen 3.7 Plus served as the hosted frontier comparator at registration time,
but it must not be described or counted as an open-weight result. The
verifiable open-weight subset consists of GLM 5.2 and MiniMax M3. MiniMax M3 is
open-weight, but its community license is not equivalent to the MIT license
used by GLM 5.2.

Provider identity is retained in manifests, bundles, and report provenance so
the execution environment remains auditable. It is not a registered
behavioral metric: the primary report contains no provider aggregate,
cross-provider comparison, or provider-effect estimate. HTTP, capacity, and
transport failures remain typed attrition; they are not converted into model
scores.

Cross-provider portability belongs to a separate adapter-conformance study. It
may test whether two endpoints satisfy the same wire and schema contract, but
its observations must not be pooled into this behavioral baseline.

Every v5 target has five registered replicates: 15 paired replicates, 30 model
attempts, and 45 projected condition branches in total. Execution is
round-robin by replicate index in the registered target order—GLM 5.2,
MiniMax M3, then Qwen 3.7 Plus—to distribute time-dependent serving variation
across model families. All three manifests were frozen under
[`manifests/v5`](manifests/v5) before execution.

## Registered generation profile

All served targets use the same harness-level controls:

- `temperature=1.0`;
- `top_p=0.95`;
- no requested seed;
- no provider-specific reasoning-effort parameter;
- a 32,768-token output ceiling;
- a 300-second timeout;
- strict JSON Schema output;
- bounded OpenAI-compatible event streaming;
- one attempt per output profile, with no retry, repair, or fallback.

The sampling profile matches the published recommendations for
[GLM 5.2](https://huggingface.co/zai-org/GLM-5.2) and
[MiniMax M3](https://huggingface.co/MiniMaxAI/MiniMax-M3). It is a standardized
study profile, not a claim that every target is at its individually optimized
setting. Hosted inference may remain nondeterministic even without an explicit
seed.

Strict structured output is deliberately part of the applied SDK scenario.
It also changes the model's operating context. In particular, Fireworks
[documents](https://docs.fireworks.ai/structured-responses/structured-response-formatting)
that JSON Schema response formatting can suppress provider-exposed reasoning
output. The study therefore observes model behavior under a structured
assertion contract; it does not measure unconstrained reasoning or private
chain-of-thought quality.

For v3 onward, `expected_observation` is additionally constrained to the exact
preregistered revision codes `A` and `B`. Explanatory language remains
available in `falsified_when` and `public_explanation`. This makes the
prediction representation identical to the deterministic environment's
observation vocabulary and removes any need for post-hoc text interpretation.

## Execution binding

Before either hosted call in a replicate, its versioned comparison manifest
must bind:

- both output schemas and both rendered request payloads;
- the common case request;
- the two-attempt call order;
- the fact that Conditions B and C share one structured attempt;
- all generation, timeout, failure, privacy, metric, and retention policies.

Each provider attempt is single-shot: no automatic retry, JSON repair, prompt
change, fallback, or structured-output downgrade. If either attempt fails, the
failure remains part of the comparison bundle and the replicate is incomplete.
It must not be silently replaced.

## Bundle requirements

One verified comparison bundle must retain:

- the frozen comparison manifest;
- the narrative result or sanitized attempt failure;
- the structured result or sanitized attempt failure;
- separate ledgers, scorecards, and reasoning receipts for A, B, and C when the
  required attempt succeeded;
- private raw provider responses;
- a digest-bound inventory declaring each file's visibility;
- explicit bindings showing that B and C use the same structured result and raw
  artifact.

Finalization requires complete schema, digest, reference, ledger, scorecard,
and receipt validation. The destination is immutable and must never be
overwritten.

## Analysis boundary

Report target-level counts, distributions, individual branch outcomes, and
paired branch differences. Do not calculate a general truth or trust score.
Five samples per served target are observational and too small to support
population-level significance claims.

V2 exposed a specific analysis defect: the model contract allowed a free-text
`expected_observation`, while the case scorer expected the exact revision code
`A` or `B`. V2 prediction-correctness values, evidence-relation labels, and
resulting final epistemic states must therefore not be interpreted as model
behavior. The original bundles and scorecards remain immutable. The report
marks those metrics invalid instead of adding an unregistered text parser or
manually regrading responses.

V4 repaired the representation prospectively, and v5 preserves that treatment:
the allowed codes and their order are bound into each manifest, output schema,
prompt payload, and digest before execution. Prediction correctness, evidence
relations, and final epistemic states are therefore defined for conforming v5
bundles. This correction does not retroactively validate the corresponding v2
fields. V3 bundles are not analyzed because that study stopped on an
instrumentation-integrity defect. V5 also does not reuse v4 outputs.

Most importantly, repeatedly sampling this one transparent case measures
within-prompt behavior, not cross-task causal-reasoning ability. A later study
requires hidden mechanism and case variation before making transfer claims.

## Study history

The earlier registrations remain public because the defects they exposed are
part of the research record:

- V1 used non-streaming transport and included two Qwen 3.7 Max endpoint cells
  that were unavailable.
- V2 introduced streaming and Qwen 3.7 Plus. All 25 registered slots produced
  validated bundles, but a free-text-versus-code mismatch made prediction
  correctness and resulting verdict-state measurements invalid. The
  [v2 report](../results/archive/2026-07-24-case-001-comparison-v2.md) marks
  those fields invalid rather than regrading them after the fact.
- V3 registered a categorical observation vocabulary to correct that mismatch,
  then stopped after ten of 20 runs when failed-attempt receipts were found not
  to bind the manifest's structured profile. Its bundles were not reused.
- V4 corrected that validator defect and completed all 20 registered runs
  across a mixed-provider matrix. Its
  [report](../results/archive/2026-07-24-case-001-comparison-v4.md) remains a
  historical observation set, not a provider comparison.
- V5 used a fresh, provider-controlled three-model matrix. It did not filter or
  reuse v4 model outputs or scorecards.

The immutable registrations are retained under [`manifests/`](manifests/).

## Threats and limitations

- Candidate hypotheses and tests are visible, making this a closed-set task.
- Descriptive option text may cue a correct selection.
- The narrative condition still uses a minimal JSON envelope for reliable
  scoring.
- The structured schema itself may scaffold a better assertion; that is part of
  the A-to-B treatment, not proof of internal reasoning.
- The v5 categorical observation vocabulary is an additional harness treatment
  and may improve formatting compliance independently of causal diagnosis.
- B and C are deliberately dependent branches and must never be counted as two
  independent model samples.
- The case-specific deterministic oracle is custom verification infrastructure;
  its cost and lack of transfer remain central research measurements.
- A fixed seed and temperature do not guarantee deterministic hosted inference.
- A single provider controls between-provider variation but cannot guarantee
  identical model-specific quantization, templates, or serving configuration.
- A provider may change the model behind an unchanged identifier.
- One case, a small provider/model cohort, and five replicates per cell cannot
  support a general claim.

## Acceptance criteria

- both attempts receive byte-identical case facts and option content;
- profile-specific prompt and schema differences are explicit and hashed;
- exactly two model attempts create exactly three condition branches;
- Conditions B and C reference the same structured result and artifact digest;
- A records no prediction, test plan, test result, evidence-backed verdict, or
  promotion;
- B records a plan but executes no test and performs no evidence-backed
  promotion;
- C alone executes the selected test and may promote only after external
  evidence;
- evaluator-only grading never enters A or B as evidence;
- every successful branch has a valid ledger and recomputable receipt;
- every provider failure is retained without retry;
- no private provider response or credential is committed to the repository;
- published conclusions remain bounded to the observed replicate.
