# Experiment results

This directory separates the current research result from earlier engineering
observations and superseded study versions.

## Current result

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

## What is publicly verifiable

The checked-in JSON report contains the validated scorecards, run identifiers,
failure classes, model provenance, metric-validity declarations, and response
digests used for its counts. The Markdown report is deterministically rendered
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

Archived results are not pooled into the v5 measurements or presented as
current product guidance.
