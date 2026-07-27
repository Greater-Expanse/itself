# Model investigation contracts

These files define the JSON handoff between an application investigating a
problem and the component that suggests what to test next. They are an advanced
integration reference; start with the
[experiment overview](../README.md) if you first want to see the workflow in
application terms.

The code calls that replaceable component a `Diagnostician`. It may be a hosted
model, local model, deterministic program, human-operated tool, or another
language implementation. Whatever supplies the result remains
non-authoritative: it can make a model assertion and choose a declared check,
but it cannot claim that the check ran, turn its own output into evidence, or
certify its conclusion.

This experimental contract is the provider-neutral boundary between a causal
diagnosis runner and a replaceable diagnostician. Its canonical representation
is JSON so a model provider, local model, deterministic program, human tool, or
another language can implement it without importing the Python reference code.

Version `0.2.0` intentionally supports one narrow, closed-set workflow:

1. the runner supplies observations, candidate hypotheses, and opaque test
   options;
2. the diagnostician retains one or more hypotheses, makes predictions, selects
   one test, and returns a public explanation;
3. the runner validates all references before it executes the selected test;
4. only the environment adapter emits resulting evidence;
5. only an eligible evaluator or policy actor may authorize an evidence-backed
   state transition.

The diagnostician therefore asserts what may be true and identifies what to
test. The application remains responsible for running the check, recording the
observation, and applying the rule that determines what the evidence supports.

## Files

- [`v1/diagnosis-request.schema.json`](v1/diagnosis-request.schema.json) defines
  the information visible to the diagnostician.
- [`v1/diagnosis-assertion.schema.json`](v1/diagnosis-assertion.schema.json)
  defines only the untrusted assertion a model may emit. Adapter-observed
  identity, usage, timing, and artifact metadata are deliberately absent.
- [`v1/diagnosis-result.schema.json`](v1/diagnosis-result.schema.json) defines a
  structured assertion, adapter identity, usage, and a reference to captured raw
  output.
- [`v1/narrative-assertion.schema.json`](v1/narrative-assertion.schema.json)
  defines the deliberately minimal Condition-A model output: one selected
  hypothesis, a public explanation, and nullable confidence. Predictions,
  tests, evidence, and verdicts are forbidden.
- [`v1/narrative-result.schema.json`](v1/narrative-result.schema.json) combines
  that narrative assertion with adapter-owned identity, usage, timing, and raw
  output provenance.
- [`v1/trial-manifest.schema.json`](v1/trial-manifest.schema.json) defines a
  credential-free preregistration that binds a trial series to exact case,
  adapter, endpoint, prompt-payload, generation, attempt, artifact, and metric
  declarations.
- [`v1/comparison-manifest.schema.json`](v1/comparison-manifest.schema.json)
  defines the paired two-attempt, three-branch preregistration for the case-001
  comparative pilot. It binds both model-facing payloads and records that the
  structured-without-testing and evidence-enforced branches share one model
  sample.
- [`v1/comparison-bundle.schema.json`](v1/comparison-bundle.schema.json)
  defines the immutable inventory, attempt outcomes, and branch-to-attempt
  bindings for a completed or interrupted comparison replicate.
- [`v1/comparison-attempt-report.schema.json`](v1/comparison-attempt-report.schema.json)
  defines the sanitized record retained when either preregistered comparison
  attempt fails.
- [`v1/trial-bundle.schema.json`](v1/trial-bundle.schema.json) defines the
  digest-bound file inventory for one completed or failed evidence-enforced
  trial.
- [`v1/trial-attempt-report.schema.json`](v1/trial-attempt-report.schema.json)
  defines the sanitized failure record retained when an adapter attempt cannot
  produce a conforming assertion.
- [`v1/examples/`](v1/examples/) contains valid language-neutral fixtures.

The contract version is separate from the Claim and Evidence Protocol version.
Changing the provider boundary does not necessarily change the ledger protocol,
and vice versa.

## Semantic conformance rules

JSON Schema validates shape. A conforming runner must additionally enforce that:

- option identifiers are unique within a request;
- every retained hypothesis, prediction, and selected test resolves to an
  option in the same request;
- the primary hypothesis is retained;
- every retained hypothesis has exactly one prediction for the selected test;
- prediction pairs of hypothesis and test are unique;
- confidence, when supplied, is finite and between zero and one;
- the raw-output artifact is provenance, not evidence;
- raw private reasoning is not required or embedded in the result.

The raw-output artifact may point to a complete provider response, a redacted
response, or a normalized structured response according to operator policy. The
contract carries only its URI, media type, capture time, and digest. A runner
must not dereference or promote it automatically.

The model-facing assertion schema makes every field required for compatibility
with strict structured-output implementations. The otherwise optional
`expressed_confidence` field is represented as a number or `null`. An adapter
maps `null` to an absent confidence value when it constructs a result.

The narrative profile uses a JSON envelope only to make the selected closed-set
hypothesis and declared confidence directly scoreable. It does not add
structured alternatives, predictions, test selection, or epistemic authority;
the explanation remains a narrative assertion.

## Deliberate exclusions

This version does not define free-form hypothesis matching, prompt templates,
provider authentication, tool execution, semantic judging, confidence
calibration, or model-as-evaluator behavior. Those abstractions require evidence
from repeated trials before standardization.
