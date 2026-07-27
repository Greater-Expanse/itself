# Controlled experiments

Itself does not make a model know when it is right. These experiments ask a
more practical question: can an application keep a plausible model answer
provisional, turn it into a testable investigation plan, require evidence from
outside the model before treating the answer as confirmed, and preserve how the
decision was reached?

This is the same assurance boundary needed when an AI agent reviews code,
diagnoses CI failures, proposes a repair, opens a merge request, or recommends
an incident response. Case 001 isolates that boundary in one controlled
engineering incident.

## Start here

1. Read [Case 001](001-cache-key-omission/README.md) for the stale-report
   incident in real-world terms.
2. Read the [current v5 result](results/current/2026-07-24-case-001-comparison-v5.md)
   for what happened and what it does not establish.
3. Run the credential-free reference workflow below to inspect the SDK's
   enforcement mechanics yourself.

The [results index](results/README.md) separates the current result from earlier
engineering pilots and superseded study versions.

## What the current experiment does

Case 001 models a familiar engineering incident: a report should use source
revision `B`, but repeated attempts keep returning stale revision `A`. The model
sees the failure history, three plausible root causes, and the diagnostic checks
available to an engineer. It is not told which cause is active.

Each comparison run contains two model calls and three application paths:

| Path | What the application does |
| --- | --- |
| Ordinary AI answer | Ask which root cause is most likely and why. Do not run a check. |
| Testable investigation plan | Ask the model to retain plausible causes, predict what a selected check would show under each one, and choose that check. Record the plan without running it. |
| Evidence-tested plan | Reuse the exact same plan, run its selected check outside the model, and let the resulting evidence control what may be marked supported or refuted. |

The untested and evidence-tested paths share one model response. This isolates
the effect of external testing from the variability of asking the model again.

## Current result: v5, July 2026

V5 scheduled five runs for each of three models through one endpoint provider:
GLM 5.2, MiniMax M3, and Qwen 3.7 Plus.

- 12 of 15 runs completed both model calls and all three paths.
- Every completed run kept the model answer provisional until the external
  check ran.
- The application accepted no unsupported answer as confirmed.
- All 36 completed paths produced reasoning receipts that recomputed from their
  ledgers.
- Three runs stopped on explicit model-output failures that remained visible;
  they were not silently repaired or replaced.

This demonstrates Itself's enforcement, failure-preservation, and
reconstructability on one transparent incident. It does not demonstrate better
model accuracy: the ordinary answer and investigation plan already selected the
known cause in every completed run. It also does not establish general causal
reasoning, cross-domain transfer, or model rankings.

See the [human-readable report](results/current/2026-07-24-case-001-comparison-v5.md)
and [machine-readable report](results/current/2026-07-24-case-001-comparison-v5.json).

## Exercise the evidence path without a model provider

The deterministic reference workflow executes the case, applies the external
check and evidence policy, and writes a ledger and reasoning receipt. It uses no
hosted model, network service, or provider credential.

From the repository root:

```bash
mkdir -p .itself/experiments/case-001

uv run --frozen python -m experiments.cases.cache_key_scripted_run \
  --output .itself/experiments/case-001/ledger.jsonl

uv run --frozen itself ledger summary \
  .itself/experiments/case-001/ledger.jsonl

uv run --frozen itself receipt generate \
  .itself/experiments/case-001/ledger.jsonl \
  --output .itself/experiments/case-001/receipt.json

uv run --frozen itself receipt validate \
  .itself/experiments/case-001/receipt.json \
  --ledger .itself/experiments/case-001/ledger.jsonl
```

This is an SDK integration exercise, not a language-model evaluation. Its
reviewed deterministic response makes the enforcement behavior reproducible.

## Connect your own model

The experiment boundary is provider-neutral. A hosted model, local model,
deterministic program, human tool, or another language can supply the same
request and result files.

Use:

- [Model adapter design](MODEL_ADAPTERS.md) for the generic
  OpenAI-compatible endpoint boundary and failure behavior;
- [Experiment contracts](contracts/README.md) for the language-neutral JSON
  interfaces;
- `python -m experiments.cases.cache_key_exchange --help` for offline file
  exchange;
- `python -m experiments.cases.cache_key_model_trial --help` for one
  output-contract trial; and
- `python -m experiments.cases.cache_key_comparison_run --help` for a complete
  two-call comparison run.

Provider credentials are read from an operator-selected environment variable.
They are never part of the experiment manifest, report, ledger, or committed
artifact.

## Reproduce or extend the study

The [comparative methodology](001-cache-key-omission/COMPARATIVE_STUDY.md)
defines the treatments, controls, measurements, and limitations. The exact v5
model and generation configurations are retained under
[`001-cache-key-omission/manifests/v5/`](001-cache-key-omission/manifests/v5/).

The registered manifest set can be checked without a network call:

```bash
uv run --frozen python \
  -m experiments.cases.cache_key_comparison_study check \
  --study-version 5 \
  --directory experiments/001-cache-key-omission/manifests/v5
```

Re-executing the hosted study requires access to the registered endpoint and
models, incurs provider cost, and may produce different samples. The public
manifests make the treatment inspectable; they do not make hosted inference
deterministic.

## Public artifact boundary

The repository publishes:

- deterministic environment and runner code;
- language-neutral schemas and examples;
- preregistered manifests;
- human-readable result reports;
- bounded machine-readable scorecards, failure classes, model provenance, and
  response digests.

Raw hosted-model responses are not published because they may contain sensitive
input or output content. The exact historical provider responses therefore
cannot be replayed from a fresh clone. The public report remains inspectable,
and the public runners can produce new locally verifiable bundles.

Earlier pilots, superseded study versions, and known harness defects remain
available under [`results/archive/`](results/archive/). They are preserved as
research history, not presented as current product guidance.
