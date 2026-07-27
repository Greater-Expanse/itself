# Itself documentation

If this is your first visit, start with the repository
[README](../README.md). It explains how Itself adds external checks and
decision controls to AI agent workflows, shows where it fits, and includes a
provider-free quickstart.

Choose the path that matches what you are trying to do:

## Follow an agent workflow

- [From inference to evidence](../examples/inference_to_evidence/README.md) —
  follow a failed-build assertion through an external check, policy evaluation,
  authorized decision, ledger, and reasoning receipt.
- [Inspect the reference bundle](../examples/cache-key-diagnosis/README.md) —
  verify and explore the complete record of a controlled incident diagnosis.

## Integrate the SDK

- [Add assurance records](PROTOCOL_RECORDS.md) — capture agent assertions,
  expected outcomes, external checks, observed evidence, evaluations, and
  decisions with typed protocol constructors.
- [Connect a model endpoint](INFERENCE.md) — use the generic structured
  inference boundary to capture an assertion without giving the model evidence
  or decision authority.
- [Create evidence bundles](EVIDENCE_BUNDLES.md) — package a ledger, receipt,
  inputs, and referenced artifacts into a closed, independently verifiable
  directory.

## Understand the formats

- [Protocol specification](SPECIFICATION.md) — normative record semantics,
  authority rules, state transitions, privacy boundary, and threat model.
- [Canonical schemas](SCHEMAS.md) — packaged and hosted JSON Schemas, exports,
  checksums, and publication policy.
- [Reasoning receipts](REASONING_RECEIPT.md) — deterministic projections of a
  validated ledger and the limits of what a receipt proves.
- [Versioning and compatibility](VERSIONING.md) — independent version lines for
  the Python package, protocol, receipts, bundles, and model contracts.

## Evaluate and inspect

- [Reproducible evaluations](EVALUATIONS.md) — provider-free lifecycle,
  mutation, and capacity exercises.
- [Controlled experiments](../experiments/README.md) — study design, current
  results, historical defects, and live-model limitations.
- [JavaScript conformance](../conformance/javascript/README.md) — an independent
  implementation of schema validation, integrity checks, and deterministic
  replay.

Project changes are recorded in the [changelog](../CHANGELOG.md). Contribution
and vulnerability-reporting guidance remain in
[CONTRIBUTING.md](../CONTRIBUTING.md) and [SECURITY.md](../SECURITY.md).
