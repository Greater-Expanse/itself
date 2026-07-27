# Structured inference adapters

Use the inference boundary when an agent or application needs a structured
model assertion that can enter an external-check workflow. Inference is
optional: protocol validation, ledgers, evidence evaluation, and reasoning
receipts do not require a model endpoint.

Itself exposes one provider-neutral `StructuredInferenceAdapter` protocol and
one built-in implementation, `StructuredInferenceClient`, for the widely
implemented OpenAI-compatible Chat Completions wire format.

There are no hosted-provider subclasses. A provider is selected by runtime
endpoint, model, credential, and capability configuration. A native API with a
different wire format can implement the same adapter protocol without changing
Itself records, ledgers, or evidence rules.

## Minimal configuration

```python
from pathlib import Path

from itself import (
    DirectoryArtifactSink,
    EnvironmentCredential,
    OpenAICompatibleEndpoint,
    StructuredInferenceClient,
    StructuredOutputMode,
    StructuredOutputProfile,
)

endpoint = OpenAICompatibleEndpoint(
    actor_id="model-diagnostician",
    base_url="https://inference.example.com/v1",
    resource_path="chat/completions",
    model="organization/model-name",
    credential=EnvironmentCredential("INFERENCE_API_KEY"),
)

profile = StructuredOutputProfile(
    profile_id="triage-v1",
    schema_name="triage",
    system_prompt="Return only the requested JSON object.",
    user_instruction="Triage the supplied incident.",
    input_label="INCIDENT",
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["assertion", "recommended_test"],
        "properties": {
            "assertion": {"type": "string"},
            "recommended_test": {"type": "string"},
        },
    },
    mode=StructuredOutputMode.JSON_SCHEMA,
)

client = StructuredInferenceClient(
    endpoint=endpoint,
    artifact_sink=DirectoryArtifactSink(Path(".itself/responses")),
)
result = client.invoke({"symptom": "stale report output"}, profile)
print(result.value)
print(result.raw_response.sha256)
```

The credential is read from the environment only while a request is rendered.
It is absent from endpoint representations, request bodies, response artifacts,
results, and sanitized exceptions.

`DirectoryArtifactSink` refuses symbolic-link roots. On POSIX systems it also
requires an owner-only artifact directory, and stored responses are readable
and writable only by the owner.

## Authentication

Bearer tokens are the default:

```python
credential=EnvironmentCredential("INFERENCE_API_KEY")
```

An endpoint using another header, including an Azure-style API key, uses the
same class:

```python
credential=EnvironmentCredential(
    "INFERENCE_API_KEY",
    header="api-key",
    prefix="",
)
```

For an unauthenticated local server:

```python
endpoint = OpenAICompatibleEndpoint(
    actor_id="local-model",
    base_url="http://127.0.0.1:8000/v1",
    model="local/model",
    credential=None,
    allow_insecure_http=True,
)
```

Static secrets should not be placed in `extra_headers`, URLs, query parameters,
or request bodies. Credential-bearing endpoints must use HTTPS. Unencrypted
HTTP is disabled by default and can only be enabled for a credential-free
loopback host.

## Endpoint and request compatibility

`OpenAICompatibleEndpoint` supports:

- any absolute HTTPS base URL;
- explicit credential-free HTTP for loopback development servers;
- a configurable or empty resource path;
- non-secret query parameters;
- configurable `max_tokens` or `max_completion_tokens` field names;
- an omitted output-token limit when the endpoint selects its own;
- accepted finish-state declarations for compatible local servers;
- bounded Server-Sent Event streaming for endpoints that require it;
- non-secret extra headers and JSON request fields;
- a replaceable synchronous HTTP transport with no implicit retry.

The built-in transport does not follow redirects. It returns a 3xx response to
the client as an HTTP-status failure, so authentication headers are never
forwarded to a redirect target. A custom transport owns the same obligation.

For example, an endpoint whose supplied URL is already complete can use:

```python
endpoint = OpenAICompatibleEndpoint(
    actor_id="diagnostician-model",
    base_url="https://gateway.example.com/models/diagnose",
    resource_path="",
    query_parameters={"api-version": "2026-07-01"},
    model="diagnostician",
    credential=EnvironmentCredential(
        "INFERENCE_API_KEY",
        header="X-Api-Key",
        prefix="",
    ),
    max_output_tokens_field="max_completion_tokens",
)
```

Extension fields cannot override adapter-owned fields such as `model`,
`messages`, `stream`, `response_format`, authentication, or content type.

Set `stream=True` when the selected endpoint or model requires the
OpenAI-compatible Server-Sent Events form:

```python
endpoint = OpenAICompatibleEndpoint(
    actor_id="diagnostician-model",
    base_url="https://inference.example.com/v1",
    model="organization/model-name",
    credential=EnvironmentCredential("INFERENCE_API_KEY"),
    stream=True,
)
```

Streaming does not weaken the evidence boundary. The transport reads at most
its configured response-byte limit, the artifact sink captures the exact event
stream before interpretation, and the client then assembles its text locally.
It requires one terminal `[DONE]` marker, one completion choice, a stable model
identity and finish reason, and internally consistent token usage when usage is
reported. It does not expose partial output as a successful result.

## Structured-output modes

The caller declares endpoint capability explicitly:

| Mode | Wire request | Local enforcement |
|---|---|---|
| `JSON_SCHEMA` | Strict named `json_schema` response format | Strict JSON parse and Draft 2020-12 validation |
| `JSON_OBJECT` | `json_object` response format | Strict JSON parse and Draft 2020-12 validation |
| `PROMPTED_JSON` | Schema in the prompt; no response-format field | Strict JSON parse and Draft 2020-12 validation |

The schema is included in the user message in every mode. The client never
silently downgrades modes. It does not strip Markdown fences, repair JSON,
change prompts, retry, or switch models after a failure.

Local parsing rejects duplicate object keys and non-standard numeric constants.
Local schema validation is authoritative for output shape even when the
endpoint claims strict generation.

## Observable results and failures

Before interpreting a provider response, the client captures its exact bounded
bytes through the configured `ArtifactSink`. A successful
`StructuredInferenceResult` contains:

- a defensive copy of the validated JSON value;
- requested and provider-reported model identity;
- adapter identity and version;
- the raw-response artifact URI, digest, media type, and capture time;
- provider-reported token counts when available;
- locally measured wall time;
- the structured-output profile identifier.

`InferenceError` classifies configuration, transport, response-size, HTTP,
refusal, incomplete-generation, envelope, JSON, schema, usage, and artifact
failures. Only transport, rate-limit, and server failures are marked retryable;
the client itself never retries.

Response bodies and low-level transport details are kept out of exception text.
If a response exists, its artifact reference remains attached to the error for
authorized inspection.

## Native provider protocols

The built-in client deliberately targets one interoperable wire family. A
provider whose native API is not Chat Completions-compatible should implement:

```python
class MyAdapter:
    def invoke(
        self,
        input_value: JsonValue,
        profile: StructuredOutputProfile,
    ) -> StructuredInferenceResult:
        ...
```

The class then satisfies the `StructuredInferenceAdapter` protocol. It should
preserve the same observable guarantees: explicit configuration, bounded raw
response capture, strict local JSON and schema validation, sanitized failures,
and no silent repair or fallback.

## Assurance boundary

A schema-valid model response is still a model assertion. It is not evidence, a
verdict, or authorization. Applications should add the assertion to an Itself
ledger, execute declared tests through external tools or reviewers, record the
resulting evidence, and allow only an authorized non-model actor to promote the
claim or hypothesis.

The [materialized reference bundle](../examples/cache-key-diagnosis/README.md)
shows that full path.
