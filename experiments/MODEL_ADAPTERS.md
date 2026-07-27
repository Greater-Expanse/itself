# Model Adapter Design

This is the advanced implementation reference for connecting a model to an
Itself workflow. In application terms, the adapter asks a chosen model for a
structured investigation plan, records what happened during that call, and
validates the returned model assertion. It never runs the diagnostic check or
lets the model certify its own answer. Start with the
[experiment overview](README.md) for the end-to-end workflow.

Status as of July 2026: installable structured-inference adapter with a
controlled causal-diagnosis binding for contract `0.2.0`.

## Purpose

Model adapters let a hosted or local language model act as a non-authoritative
diagnostician. They translate a provider wire protocol into the existing typed
`Diagnostician` boundary; they do not make provider responses into evidence or
give a model verdict authority.

The built-in implementation targets the common OpenAI-compatible Chat
Completions wire family. It contains no provider-specific subclasses, URLs,
models, credentials, defaults, fallbacks, or behavior. Endpoint behavior is
still treated as a claim to test: an endpoint and model pair is conforming only
after it passes the adapter fixtures and an operator-authorized capability
probe.

## Boundary

```text
DiagnosisRequest
    -> diagnosis prompt renderer
    -> StructuredInferenceAdapter
       -> OpenAI-compatible Chat Completions
       -> native wire implementations supplied separately
    -> strict DiagnosisAssertion decoder
    -> adapter-owned identity, usage, timing, and artifact capture
    -> DiagnosisResult
```

The model emits only the payload defined by
`contracts/v1/diagnosis-assertion.schema.json`. The adapter, not the model,
supplies:

- actor and adapter identity;
- provider and model provenance;
- request timing and reported token usage;
- raw-response artifact location, media type, capture time, and digest.

This prevents a model from self-reporting trusted execution metadata. The final
`DiagnosisResult` remains subject to JSON Schema and request-relative semantic
validation before a selected test can execute.

## Composition

The implementation keeps three responsibilities separate:

1. A diagnosis renderer converts a provider-neutral request into messages and a
   standalone structured-output schema.
2. A structured-inference adapter sends those messages to one declared wire
   protocol and returns the untrusted response plus observed invocation
   metadata.
3. A diagnostician adapter decodes the assertion, writes or delegates raw-output
   capture to an artifact sink, constructs the result, and invokes the existing
   contract validator.

The adapter and its HTTP transport are replaceable without changing the prompt
renderer or assertion decoder. Likewise, adding a native provider protocol must
not add provider branches to the diagnosis contract.

The public implementation lives in `src/itself/inference.py`; the controlled
diagnosis binding lives in `model_adapters.py`. The default HTTP transport uses
only the Python standard library. Request construction, transport, raw-response
storage, schema validation, assertion decoding, and diagnosis-contract
validation remain separate typed boundaries.

## Endpoint configuration

Configuration describes capabilities rather than selecting hard-coded provider
classes. The public client accepts:

- base URL, resource path, query parameters, and model identifier;
- bearer, arbitrary API-key-header, or no authentication, with credentials read
  from an environment variable at request time;
- explicit `json_schema`, `json_object`, or prompted-JSON capability;
- output-token field name, accepted finish states, timeout, and explicit attempt
  policy;
- bounded OpenAI-compatible event-stream transport when a model requires
  streaming;
- optional non-secret headers and provider request options;
- a stable actor identifier used for provenance.

The SDK must never read credentials from a committed file, serialize them into
results, include authorization headers in artifacts, or expose them through
configuration representations. Live tests accept credentials only through
environment variables.

An arbitrary URL makes endpoint selection generic. It does not imply that every
provider API is compatible. A native non-Chat-Completions protocol implements
the public `StructuredInferenceAdapter` boundary while preserving the same
diagnosis and evidence interfaces.

## Structured output

The strongest portability profile uses Chat Completions `response_format` with
a named JSON Schema. The schema is also included in the prompt. Endpoints
without that capability may be configured explicitly for JSON-object or
prompted-JSON output; every mode receives the same strict local JSON parsing and
Draft 2020-12 schema validation.

The model-facing schema differs deliberately from the result schema:

- all fields are required for strict structured-output implementations;
- `expressed_confidence` is nullable rather than optional;
- provenance, usage, contract version, and artifact fields are absent;
- extra properties, including evidence and verdict fields, are forbidden.

Structured generation is a formatting control, not validation authority. Every
response is parsed without repair and checked locally. A well-formed assertion
can still fail request-relative checks, such as referencing an unavailable test
or unretained hypothesis.

An experiment may bind a stricter output profile without changing the
provider-neutral diagnosis object. Case 001 v3 constrains
`expected_observation` to its registered environment codes while leaving
explanatory prose in separate fields. The vocabulary is part of the manifest,
schema, rendered payload, and digest; it is not inferred from model prose after
execution.

## Relationship to structured-generation libraries

[Instructor][instructor] and [Outlines][outlines] address an adjacent layer of
the stack:

- Instructor turns model responses into validated application objects and can
  retry or re-ask after validation failures.
- Outlines constrains generation to schemas, types, regular expressions, or
  grammars across local and hosted model backends.

Itself currently implements the narrower primitive it needs directly: request
one strict JSON object, preserve the original response, reject malformed or
semantically invalid assertions, and make the single attempt observable. It does
not aim to become a general structured-generation library.

Either project could later back a `StructuredInferenceAdapter` or renderer
integration, but must not absorb Itself's protocol responsibilities. Itself
owns the distinction
between model assertions and evidence, causal-test references, epistemic state,
provenance, invalidation and promotion authority, and ledger-bound reasoning
receipts. Automatic repair or retry would also need an attempt-level receipt;
otherwise it would erase precisely the failure behavior this research intends
to measure.

[instructor]: https://python.useinstructor.com/
[outlines]: https://github.com/dottxt-ai/outlines

## Failures and attempts

Adapter failures must preserve where the failure occurred. The initial error
taxonomy distinguishes:

- configuration and credential resolution;
- transport timeout or connection failure;
- HTTP authentication, authorization, rate-limit, server, and other status
  failures;
- provider refusal or content filtering;
- truncated or otherwise incomplete generation;
- malformed provider response envelopes;
- missing or non-text model content;
- invalid JSON;
- assertion schema violations, reported separately as `response_schema`;
- request-relative semantic violations;
- artifact-capture failures.

There is no silent downgrade from JSON Schema to JSON object or plain text, no
JSON repair, and no prompt-changing retry. Each network attempt must be explicit
and independently observable. The first implementation performs one attempt;
retry policy will be added only with an attempt-level receipt.

## Artifact and privacy policy

Raw provider responses are provenance artifacts, never evidence. The artifact
sink receives response bytes after transport-level secret removal and returns a
URI plus digest. Operators must be able to substitute a redacting or
metadata-only sink when model inputs or outputs are sensitive.

The adapter records only sanitized response metadata in normal errors and logs.
Prompts, model output, authorization headers, and arbitrary provider error
bodies must not be placed in exception strings by default.

## Verification strategy

Offline tests use a fake transport and deterministic response envelopes. They
cover request construction, credential non-disclosure, raw-byte hashing,
provider-envelope decoding, every failure class, assertion schema validation,
and cross-reference enforcement. Unit tests never require a hosted account.

Opt-in live tests run the same frozen diagnosis request against Together AI and
Fireworks AI. Where possible, they use the same model family and pin exact model
identifiers. A conformance report records the endpoint label, base-URL origin,
model identifier, adapter version, structured-output profile, finish state,
latency, usage, raw-output digest, and validation outcome. It does not record the
credential.

The first research question is narrow: can independently hosted endpoint/model
pairs return assertions that satisfy the same provider-neutral diagnosis
contract, and which observable failure modes prevent conformance?

The case-001 live runner writes a sanitized report that binds the exact request
and assertion schema by digest, identifies the exact model, hashes the base URL,
records the adapter version and one-attempt policy, and embeds either the
validated result or a typed failure. Provider response bodies remain private
content-addressed artifacts and credentials are never serialized.

That conformance runner intentionally stops before environment execution. The
separate evidence-enforced runner consumes a versioned trial manifest and binds
the complete invocation before the network boundary. It then passes a valid
assertion through the existing case runner, where deterministic software—not the
model—executes the selected intervention, emits evidence, evaluates the causal
result, and controls promotion. The runner produces one of two outcomes:

- a completed bundle containing the manifest, diagnosis result, ledger,
  preregistered scorecard, recomputable reasoning receipt, and private raw
  response;
- a failed-attempt bundle containing the manifest, sanitized failure taxonomy,
  and any private raw response captured before failure.

Bundle manifests inventory every retained file by path, media type, visibility,
byte length, and SHA-256 digest. The bundle identifier binds that inventory and
its execution metadata. Validation also recomputes cross-document semantics,
including manifest identity, diagnosis references, usage fields, artifact
digest, ledger integrity, and receipt projection. The destination must be new;
there is no overwrite or implicit rerun.

On 2026-07-22, one live attempt with Together AI and
`openai/gpt-oss-20b` returned a conforming assertion. This is evidence that the
generic wire adapter can operate against that endpoint/model pair under the
frozen case-001 request. It is not evidence of causal correctness, repeatability,
or conformance by other providers. See the
[archived observation](results/archive/2026-07-22-together-case-001.md).

The manifest-bound evidence runner is covered offline against successful and
failed fake-provider responses. On 2026-07-22, its first registered hosted
attempt completed the full case-001 path and produced a verified trial bundle.
The model retained all alternatives and selected a discriminating intervention;
the external result—not the model assertion—then supported the true hypothesis
and refuted the alternatives. See the
[archived evidence-enforced pilot report](results/archive/2026-07-22-together-case-001-evidence-enforced-pilot.md).

The paired comparison runner extends the same assurance boundary to two model
calls and three dependent paths. One call asks for an ordinary AI answer:
choose a likely root cause and explain why. The other asks for a testable
investigation plan containing plausible causes, predictions, and a selected
check. Each provider/model target has a committed manifest that binds both
schemas and rendered payloads. Completed bundles prove that the untested and
evidence-tested paths consume the identical plan and raw-response digest.
Failure bundles preserve which request stopped execution.

The v5 matrix, registered in July 2026, uses GLM 5.2, MiniMax M3, and Qwen 3.7
Plus through one Fireworks AI endpoint family. Provider is controlled
infrastructure and retained only as provenance; it is not a behavioral metric
or grouping variable. GLM 5.2 and MiniMax M3 are public-weight targets with
their different licenses recorded. Qwen 3.7 Plus is an explicitly hosted-only
comparator and must not be counted as an open-weight result.

Together AI remains useful for opt-in adapter conformance and portability
checks. Those checks answer whether independently operated endpoints satisfy
the same wire and schema contract. They are separate from the behavioral
baseline and must not be pooled into its scorecards.

The first complete provider-controlled matrix is retained in the
[current v5 report](results/current/2026-07-24-case-001-comparison-v5.md).

## Deferred work

- OpenAI Responses and native non-OpenAI wire protocols;
- asynchronous execution and incremental public stream consumption;
- automatic capability negotiation or fallback;
- retry and backoff with attempt-level receipts;
- multi-target scheduling and cross-study comparative reporting;
- provider registries or provider-specific adapter subclasses;
- semantic grading, confidence calibration, and model-as-evaluator behavior;
- capability probes and conformance observations across more independently
  operated endpoints.
