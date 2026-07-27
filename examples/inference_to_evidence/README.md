# From agent assertion to checked decision

This compact example models a failed-build investigation. An AI agent returns a
structured causal assertion and states which result it expects from the next
check. The application runs that check outside the model, evaluates the
observation under a declared policy, and records whether a decision is
authorized.

It is the smallest executable example of the boundary Itself adds between model
output and consequential action:

```text
incident
  -> structured agent assertion
  -> expected observation and declared check
  -> external verifier
  -> observed result (evidence)
  -> policy evaluation and authorized decision
  -> replayable ledger, reasoning receipt, and artifact inventory
```

The example is offline and deterministic. `FixtureTransport` returns a reviewed
Chat Completions response, but the response still passes through the public
`StructuredInferenceClient`, local JSON Schema validation, and private
`DirectoryArtifactSink` used by a live compatible endpoint. Replacing the
fixture transport with the default HTTP transport changes the source of the
model assertion, not the evidence workflow.

Run it from the repository root:

```bash
mkdir -p .itself/examples/inference-to-evidence

uv run --frozen python -m examples.inference_to_evidence.run \
  --output .itself/examples/inference-to-evidence/bundle \
  --private-artifacts .itself/examples/inference-to-evidence/private-artifacts

uv run --frozen itself bundle validate \
  .itself/examples/inference-to-evidence/bundle
uv run --frozen itself ledger summary \
  .itself/examples/inference-to-evidence/bundle/ledger.jsonl \
  --format json
```

The bundle contains the incident input, reviewed provider envelope, structured
model assertion, verifier observation, complete ledger, and deterministic
reasoning receipt. The provider envelope is included only because this fixture
is known not to contain secrets or private data. Production provider responses
may contain prompts, outputs, identifiers, or metadata and should remain in the
private artifact sink unless explicitly reviewed for release.

The decisive boundary is visible in the ledger: the model can move its
hypothesis from `proposed` to `testable`, but only the external software
evaluator can relate observed evidence to that hypothesis and authorize its
promotion to `supported` or `refuted`. In application terms, the agent can
assert and recommend; it cannot certify its own answer or authorize the
resulting action.
