# Experiment results

This directory separates the current research results from earlier engineering
observations and superseded study versions.

## Current results

### Case 001 comparison study v5 — July 2026

The provider-controlled v5 study applied the same stale-report investigation to
GLM 5.2, MiniMax M3, and Qwen 3.7 Plus through one endpoint provider. It
scheduled five runs per model.

- [Human-readable report](current/2026-07-24-case-001-comparison-v5.md)
- [Machine-readable report](current/2026-07-24-case-001-comparison-v5.json)

The report is evidence about Itself's enforcement behavior, not a model
leaderboard or proof of general causal reasoning. Its opening section explains
the incident, the two model calls, and the three application paths before
presenting any measurements.

### Case 001 comparison study v6 — November 2026

V6 applied the same investigation to GLM 5.2, MiniMax M3.1 Flash, and Qwen 3.7
Plus through one Together AI endpoint, under study contract v2. It scheduled
five runs per model. The five GLM 5.2 runs completed. The provider refused the
test-plan request for the other two models with HTTP 400, so their ten runs
stopped there and are retained as failed runs.

- [Human-readable report](current/2026-11-26-case-001-comparison-v6.md)
- [Machine-readable report](current/2026-11-26-case-001-comparison-v6.json)

V6 differs from v5 in provider, in one model, and in the study contract. The
two results are not pooled or compared.

### Case 001 comparison study v7 — November 2026

V7 repeated v6 with one change: its test-plan schema omits the `uniqueItems`
keyword, which the provider refused for two of v6's three models, and its
system prompt states that requirement instead. It scheduled five runs per
model. Fourteen of 15 runs completed: five for GLM 5.2, four for MiniMax M3.1
Flash, and five for Qwen 3.7 Plus. One MiniMax M3.1 Flash run stopped on a test
plan that broke the diagnosis contract and is retained as a failed run.

- [Human-readable report](current/2026-11-26-case-001-comparison-v7.md)
- [Machine-readable report](current/2026-11-26-case-001-comparison-v7.json)

Like the v5 report, it is evidence about Itself's enforcement behavior, not a
model leaderboard. V7 differs from v6 in its test-plan output profile, so v5,
v6, and v7 are not pooled or compared.

## What is publicly verifiable

Each checked-in JSON report contains the validated scorecards, run identifiers,
failure classes, model provenance, metric-validity declarations, and response
digests used for its counts. Each Markdown report is deterministically rendered
from that data.

Private model-response content is not published. Consequently, a fresh clone
can inspect the bounded public report and verify that its Markdown matches its
JSON, but cannot replay the exact historical provider responses. The registered
manifests and runners are public so researchers can execute new trials under the
same declared treatment.

## Historical results

These artifacts remain public because unsuccessful runs and instrumentation
defects are part of the research record:

- [v4 comparison report](archive/2026-07-24-case-001-comparison-v4.md) and its
  [JSON data](archive/2026-07-24-case-001-comparison-v4.json) — corrected
  categorical scoring, but a mixed-provider execution matrix;
- [v2 comparison report](archive/2026-07-24-case-001-comparison-v2.md) and its
  [JSON data](archive/2026-07-24-case-001-comparison-v2.json) — retained as a
  harness-validation result because a representation mismatch invalidated some
  behavioral measurements;
- [first evidence-enforced hosted pilot](archive/2026-07-22-together-case-001-evidence-enforced-pilot.md)
  — one complete hosted execution before repeated comparisons;
- [first adapter-conformance observation](archive/2026-07-22-together-case-001.md)
  — one early endpoint integration smoke test.

Archived results are not pooled into the v5, v6, or v7 measurements or
presented as current product guidance.
