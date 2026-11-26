# Model Adapter Design

This is the advanced implementation reference for connecting a model to an
Itself workflow. In application terms, the adapter asks a chosen model for a
structured investigation plan, records what happened during that call, and
validates the returned model assertion. It never runs the diagnostic check or
lets the model certify its own answer. Start with the
[experiment overview](README.md) for the end-to-end workflow.

Status as of November 2026: installable structured-inference adapter with a
controlled causal-diagnosis binding for diagnosis contract `0.2.0`, used by
studies registered under study contracts v1 and v2.

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

The public client is `itself.inference`. Its endpoint checks, HTTP transport,
raw-response capture, and sanitized errors live in private sibling modules
(`_endpoints.py`, `_http_transport.py`, `_artifact_sinks.py`, and
`_inference_errors.py` under `src/itself/`) that `itself.inference` re-exports.
The controlled diagnosis binding lives in `model_adapters.py`. The default HTTP
transport uses only the Python standard library. Request construction,
transport, raw-response storage, schema validation, assertion decoding, and
diagnosis-contract validation remain separate typed boundaries.

Manifests and results registered under study contract v1 record the binding's
identity, `openai-chat-completions` version `0.2.0`, not the shipped
`openai-compatible-chat-completions` client that sends the requests, and no v1
result names the shipped client's version. Binding `0.2.0` has been
re-verified offline against shipped adapter `0.4.0`: every registered study
manifest still validates and reproduces its prompt-payload digests, the
committed reports re-render byte for byte, and the request body is the one
shipped `0.3.1` sent, while the `User-Agent` header names the shipped version.
A test pins that pairing, so a change to the shipped client fails until the
pairing is re-verified and the binding version kept or deliberately bumped.
Under study contract v1 the binding also sends requests with no total
deadline: a registered manifest bounds each read with
`execution.timeout_seconds` and registers no deadline, so the shipped client's
default deadline would change how a registered request runs. A v2 manifest
declares its deadline; see [Study contract v2](#study-contract-v2).

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
configuration representations. The live runners accept credentials only
through an environment variable the operator names.

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

A comparison manifest names its test-plan profile in
`profiles.structured.profile_id`, and the harness rebuilds that profile from
it: `diagnosis-assertion-v0.2.0` registers no expected observations,
`diagnosis-assertion-v0.3.0` constrains them, and `diagnosis-assertion-v0.4.0`
constrains them and sends v0.3.0's schema without the `uniqueItems` keyword on
`retained_hypothesis_ids`, stating that requirement in its system prompt
instead. Case 001 v7 binds v0.4.0 because a provider's structured output
refused the keyword. The local check does not change with the profile: the
returned assertion is decoded against the unchanged contract, so a repeated
identifier fails as `assertion_contract`. A manifest that names an unknown
profile, or one that does not match whether it registers expected
observations, is refused.

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
- a response over the byte limit;
- HTTP authentication, authorization, rate-limit, server, and other status
  failures;
- provider refusal or content filtering;
- truncated or otherwise incomplete generation;
- malformed provider response envelopes;
- missing or non-text model content;
- invalid JSON;
- assertion schema violations, reported separately as `response_schema`;
- missing, malformed, or inconsistent token usage;
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

No test calls a provider. Live calls are made only by the opt-in runners,
`experiments.cases.cache_key_model_trial` for one conformance trial and
`experiments.cases.cache_key_comparison_run` for a registered comparison
replicate, and each sends the same frozen diagnosis request to an endpoint the
operator names.

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

The runner checks the manifest's metrics against the scorecard fields and
refuses an existing destination, including a dangling symbolic link, before it
sends a request. After a response is captured, no exception deletes it. Under
study contract v1, a failure that is not an adapter failure, such as a
reasoning receipt that does not recompute, leaves no published bundle; the
staging directory is renamed to `<destination>.quarantined` and the error names
that path. An HTTP status outside 100–599, which v1 attempt reports cannot
hold, is recorded as a null `status_code` under the `http_status` failure kind,
and the raw response is kept. A v1 attempt report holds a status only for an
HTTP-status failure, so any other failure, such as a response rejected for its
size, is recorded with a null status. Study contract v2 instead publishes the
runner failure and keeps each of these statuses; see
[Study contract v2](#study-contract-v2).

Bundle manifests inventory every retained file by path, media type, visibility,
byte length, and SHA-256 digest. The bundle identifier binds that inventory and
its execution metadata. Validation reads only regular files, without following
symbolic links and within the shipped evidence-bundle size limits, and rejects
any symbolic link or non-regular entry in the bundle. It then replays the
recorded diagnosis through the case: the stored ledger and scorecard must equal
the replay, the receipt must recompute from the ledger, and the manifest must
match the request, the recorded diagnostician, and the prompt payload it
renders. The destination must be new; there is no overwrite or implicit rerun,
and a quarantined attempt also keeps its slot.

This runner and the paired comparison runner below exit 0 for a completed
bundle and 1 only for a published or verified failed-attempt bundle. Any other
failure exits 2 with a one-line `ERROR:` message and no traceback.

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
Failure bundles preserve which request stopped execution. Comparison bundles
follow the same quarantine, status-code, file-reading, and exit-code rules. Each
branch's `receipt_recomputes` is measured. Under study contract v1, a replicate
with a branch whose receipt does not recompute from its ledger is quarantined,
not published; under [study contract v2](#study-contract-v2) it is published
as a runner failure.

The v5 matrix, registered in July 2026, uses GLM 5.2, MiniMax M3, and Qwen 3.7
Plus through one Fireworks AI endpoint family. Provider is controlled
infrastructure and retained only as provenance; it is not a behavioral metric
or grouping variable. GLM 5.2 and MiniMax M3 are public-weight targets with
their different licenses recorded. Qwen 3.7 Plus is an explicitly hosted-only
comparator and must not be counted as an open-weight result.

The v6 and v7 matrices, registered in November 2026 under study contract v2,
use GLM 5.2, MiniMax M3.1 Flash, and Qwen 3.7 Plus through one Together AI
endpoint, again as controlled infrastructure. V5, v6, and v7 are not pooled or
compared.

The first complete provider-controlled matrix is retained in the
[v5 report](results/current/2026-07-24-case-001-comparison-v5.md). The
[results index](results/README.md) lists all three current reports.

## Study contract v2

Study contract v2 ([`contracts/v2/`](contracts/v2/), document version `0.3.0`)
records five things that v1 cannot. Each manifest, bundle manifest, and attempt
report declares its version, and loaders and validators use the schemas of that
version and refuse any other. Builders and runners follow the manifest they are
given, so a v1 manifest still binds, runs, and validates exactly as before, and
no study registered under v1 changes. The binding identity is
`openai-chat-completions` `0.2.0` under both versions, because the same case
and settings render the same payload bytes; the one difference is
`stream_options` when a v2 manifest asks for usage on a stream.
`build_trial_manifest`, and `build_comparison_manifest` with
`contract=V2_CONTRACT`, produce v2 manifests, and a `ComparisonStudyTarget`
with `contract=V2_CONTRACT` carries the v2 settings. Case 001 comparison study
v6 is the first study registered under v2: its manifests pin the shipped client
`openai-compatible-chat-completions` `0.4.0` as a literal, ask every stream for
usage, and give every request a 600-second deadline. V7 keeps those settings
and registers the `diagnosis-assertion-v0.4.0` test-plan profile.

1. **Observed HTTP status.** A v2 attempt report keeps the status the
   transport returned, unclamped, including one outside 100–599, and `null`
   only when no response arrived. The runner reads it from a wrapper around the
   transport, because the shipped client names a status only for a non-2xx
   response; a malformed 200 response is therefore recorded with status 200.
   When the shipped transport rejects a response after its headers
   arrived, such as a body over its byte limit or one that ends early, its
   error names that status, and the wrapper keeps it the same way. The status
   the adapter reports with a failure must equal the one the wrapper kept.
2. **Request deadline.** `execution.deadline_seconds` is a positive number of
   seconds, or `null` for none, beside the per-read `timeout_seconds`. The
   binding passes it to the shipped endpoint, and binding a manifest to an
   execution checks it like the other execution fields. A v1 binding refuses
   any deadline.
3. **Shipped adapter identity.** `diagnostician.shipped_adapter` pins the id
   and version of the shipped `itself.inference` client. The runner compares
   the pin with the running client before any request and refuses a mismatch.
   Every v2 bundle manifest and attempt report records `shipped_adapter`, read
   from `itself.inference` when the record is written, and validation requires
   it to equal the pin. Validation does not consult the running client, so a
   recorded study still verifies after the shipped client changes.
4. **Runner failure.** A failure after a request was sent that is not an
   adapter failure, such as a scorer exception or a disk error while the bundle
   is written, publishes a valid, inventory-bound bundle with outcome
   `runner_failed`. Its attempt report has failure kind `runner_error`, the
   observed status, the captured response, and the exception type with a
   one-line message of at most 500 characters from which the credential is
   redacted. Files that an unfinished completion wrote are removed first; the
   captured response never is. An interrupt is published the same way with
   kind `interrupted` and then raised again, so it still stops the caller.
   Quarantine remains only for an attempt whose failure bundle cannot itself be
   published; an interrupt is still raised then, with that failure added as a
   note. A failure before any request propagates and leaves nothing behind, as
   under v1. A comparison runner failure belongs to the attempt whose result
   was not yet recorded: the narrative attempt while it runs, and otherwise the
   structured attempt, with the narrative result kept. The comparison report
   (version `0.4.0`) counts runner failures in `runner_failed_replicates` and
   `runner_failures`, keeps `failed_replicates` and `failures` for adapter
   failures, reports only a runner failure's exception type, and labels it
   `harness:` in the Markdown table.
5. **Token usage on streams.** `generation.stream` and
   `generation.stream_include_usage` are required, and usage may be requested
   only on a stream. When it is, the binding sends
   `stream_options: {"include_usage": true}`, which is part of the bound prompt
   payload and its digest. A provider may report token usage on a stream only
   when the request asks for it. No v1 manifest can send `stream_options`, so
   a streamed v1 comparison against such a provider stops with a
   `response_usage` adapter failure rather than completing.

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
