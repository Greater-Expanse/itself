# Case 001: Why does this report keep coming back stale?

Case 001 is a controlled software-incident investigation. It gives Itself a
small, deterministic environment in which a plausible model explanation can be
kept separate from a cause that has actually been tested.

## The incident

A report-generation system first processes source revision `A`. The source then
advances to revision `B`, but six later attempts still produce and verify stale
revision `A`.

The environment contains one known cause: the caching proxy keys responses by
report identifier but omits the source revision. The model is not told that
answer. It receives the observed failures and three plausible root causes:

1. the cache key omits the source revision;
2. the report worker reads stale source data;
3. the verifier reads an older artifact.

The model may also choose from declared diagnostic checks such as bypassing the
cache or inspecting the worker response directly.

## What the experiment compares

| Application path | What happens |
| --- | --- |
| Ordinary AI answer | The model picks the most likely cause and explains why. Nothing checks the answer. |
| Testable investigation plan | The model keeps plausible causes, predicts what a selected check would show under each one, and chooses that check. The application records the plan but does not run it. |
| Evidence-tested plan | The application runs the same selected check outside the model. The resulting observation, evaluated by a declared rule, controls which cause may be marked supported or refuted. |

One comparison run contains two model calls and all three application paths. The
untested and evidence-tested paths reuse the exact same investigation plan, so
their difference comes from external testing rather than another model sample.

## Current result

The July 2026 v5 study scheduled five runs for each of three models. Twelve of
15 runs completed both model calls and all three paths. In every completed run,
the application kept the model answer provisional until the external check ran,
accepted no unsupported answer as confirmed, and produced reasoning receipts
that recomputed from their ledgers.

This demonstrates the SDK's enforcement and recordkeeping behavior on one
transparent case. It does not show that Itself improved model accuracy: both
model calls already selected the known cause in every completed run.

Read the [current result report](../results/current/2026-07-24-case-001-comparison-v5.md)
for the complete observations and limitations.

## Explore further

- [Deterministic case design](DESIGN.md) explains the workflow, competing
  causes, diagnostic checks, and evaluator behavior.
- [Comparative study methodology](COMPARATIVE_STUDY.md) defines the model
  treatments, controls, measurements, and limitations.
- [`manifests/v5/`](manifests/v5/) contains the exact preregistered July 2026
  model and generation configurations.
- [`../cases/`](../cases/) contains the executable environment, runners, and
  report generator.
