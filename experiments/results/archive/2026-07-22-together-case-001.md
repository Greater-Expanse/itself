# Together Case-001 Conformance Observation

Date: 2026-07-22 (UTC)

Status: one successful live engineering observation, not a benchmark result.

This is a historical adapter smoke test, retained to show the first live
contract-conformance observation and its limitations. It is not the current
research baseline. See the [results index](../README.md) for the current Case
001 comparison.

## Configuration

- adapter: `openai-chat-completions` `0.1.0`;
- diagnosis contract: `0.1.0`;
- provider endpoint: Together AI's OpenAI-compatible Chat Completions API;
- model: `openai/gpt-oss-20b`;
- attempts: one;
- automatic retries, repair, and fallback: disabled.

The bearer credential was injected at runtime and was not serialized into the
request, report, artifact, or repository.

## Bound inputs

- diagnosis request SHA-256:
  `d11d486902622a1bd271559f9fde36900b311ae479ccfc263fad50becf8b29c6`;
- model assertion schema SHA-256 (legacy contract `0.1.0`):
  `cf8e705e40b9c3467485072ea8b0c82b66ea94ed4c5518964f615381d7cb3e34`.

## Observed result

- adapter outcome: `conformant`;
- process exit status: `0`;
- retained hypotheses: one;
- selected test: `bypass_caching_proxy`;
- reported model input tokens: 1,556;
- reported model output tokens: 943;
- observed wall time: 8,984 milliseconds;
- raw provider response: not retained or published.

`Conformant` means that the endpoint returned an assertion satisfying the frozen
JSON Schema and the exact request's cross-reference constraints. It does not
mean that the retained hypothesis was correct, that the selected test was
maximally discriminating, or that external evidence supported a causal verdict.

Because the raw response and complete report were not retained, this note is an
observation rather than a replayable research package.
