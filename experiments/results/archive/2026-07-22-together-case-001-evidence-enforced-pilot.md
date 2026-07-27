# Together Case-001 Evidence-Enforced Pilot

Date: 2026-07-22 (UTC)

Status: one successful preregistered engineering pilot, not a benchmark result.

This is a historical engineering pilot, retained because it was the first
hosted response carried through the complete evidence path. It is not the
current research baseline. See the [results index](../README.md) for the
current Case 001 comparison.

## Research boundary

This was the first hosted-model attempt through the complete case-001 path:
model assertion, controlled intervention, environment-emitted evidence,
deterministic causal evaluation, policy-controlled promotion, ledger,
scorecard, and recomputable reasoning receipt.

It is distinct from the earlier
[assertion-conformance observation](2026-07-22-together-case-001.md), which
stopped after validating the model's structured output. This pilot used one
transparent case with visible candidate hypotheses and one model-provider pair.
It cannot establish repeatability, provider or model transfer, benchmark
resistance, or general causal-reasoning ability.

## Registered configuration

The versioned
[trial manifest](../../contracts/v1/examples/trial-manifest.json) was frozen
before execution. It declared:

- trial series: `case-001-together-gpt-oss-20b-pilot-v1`;
- study phase: `engineering_pilot`;
- condition: `evidence_enforced`;
- adapter: `openai-chat-completions` `0.1.0`;
- diagnosis contract: `0.1.0`;
- provider endpoint: Together AI's OpenAI-compatible Chat Completions API;
- model: `openai/gpt-oss-20b`;
- temperature: `0.0`;
- seed: `20260722`;
- maximum output tokens: `2048`;
- timeout: `90` seconds;
- trials and maximum attempts: one;
- automatic retry, JSON repair, and fallback: disabled;
- raw provider response: retained privately;
- credentials: environment only;
- private reasoning: not requested.

The manifest registration timestamp was `2026-07-22T04:16:48Z`; the bundle was
created at `2026-07-22T04:40:40.647368Z`.

The bearer credential was injected at runtime and was not serialized into the
manifest, request, result, ledger, receipt, scorecard, or repository.

## Cryptographic bindings

- diagnosis request SHA-256:
  `d11d486902622a1bd271559f9fde36900b311ae479ccfc263fad50becf8b29c6`;
- model assertion schema SHA-256 (legacy contract `0.1.0`):
  `cf8e705e40b9c3467485072ea8b0c82b66ea94ed4c5518964f615381d7cb3e34`;
- fully rendered prompt payload SHA-256:
  `d5542a82ab1c23598803b6b347866921dbe1e09528aa3abf3e7b77830efda3af`.

The raw response is not included in this repository.

## Model assertion

The model:

- retained all three registered hypotheses;
- selected `cache_key_omits_source_revision` as primary;
- selected the discriminating `bypass_caching_proxy` intervention;
- predicted observed revision `B` under the cache-key hypothesis;
- predicted observed revision `A` under each alternative;
- reported confidence `0.6`.

These values were model assertions, not evidence or verdicts. The adapter
validated their structure and exact request-relative references before the
runner was allowed to execute the selected test. Expressed confidence played no
role in the external evidence evaluation or promotion decision.

## External test and epistemic outcome

The deterministic environment observed revision `B` when the caching proxy was
bypassed. That observation matched only the predeclared cache-key prediction.
The deterministic causal oracle therefore authorized:

- `cache_key_omits_source_revision`: `supported`;
- `worker_reads_stale_source`: `refuted`;
- `verifier_reads_stale_artifact`: `refuted`.

The model did not emit the evidence or authorize these transitions. One
deliberate premature-promotion attempt was rejected, and zero false promotions
were accepted.

## Preregistered scorecard

| Metric | Result |
|---|---:|
| Ground truth identified | `true` |
| Ground truth retained | `true` |
| Evidence identifies ground truth | `true` |
| False promotion attempts | `1` |
| False promotions accepted | `0` |
| Predictions evaluated | `3` |
| Predictions correct | `3` |
| Tests executed | `1` |
| Discriminating tests | `1` |
| Behavioral recovery | `true` |
| Negative-control behavioral recovery | `true` |
| Negative control addresses cause | `false` |
| Ledger records | `23` |
| Model input tokens | `1,556` |
| Model output tokens | `725` |
| Observed model-call wall time | `6,989 ms` |
| Reasoning receipt recomputes | `true` |

The negative control is part of the deterministic case rather than a second
model attempt. It demonstrates that directly restoring output behavior can pass
the task oracle without addressing the injected causal mechanism.

## Verification performed

The independent validator verified:

- the manifest and bundle schemas;
- the exact file inventory, byte lengths, visibility declarations, and digests;
- the bundle identifier;
- manifest-to-bundle identity and condition;
- diagnosis references and raw-artifact binding;
- scorecard fields and model usage;
- the 23-record ledger and its final epistemic states;
- exact reasoning-receipt recomputation against that ledger.

## What this observation supports

This attempt supports a narrow engineering claim: a generic
OpenAI-compatible model adapter can be bound by a frozen manifest, produce a
valid non-authoritative assertion, and participate in a complete workflow where
external evidence and deterministic policy—not model fluency or confidence—
control epistemic promotion. The resulting evidence history can be retained and
independently checked without publishing the private provider response.

This single pilot does not show that the architecture improves outcomes over
ordinary-answer or untested-plan conditions, works across providers or models,
transfers to a second failure mechanism or domain, or remains economical at
scale. Later repeated case-001 results are indexed in
[`../README.md`](../README.md); held-out transfer and human-outcome studies
remain future work.
