# Changelog

User-visible SDK and format changes are recorded here. Protocol, receipt,
evidence-bundle, and experiment-contract versions remain independent of the
Python distribution version.

## Unreleased

### Added

- A versioned, content-identified evaluation-report contract with canonical
  JSON output and derived Markdown rendering.
- A deterministic offline SDK exercise covering the complete
  inference-to-evidence lifecycle, exact invalid-fixture outcomes, bundle
  reproduction, and adversarial bundle mutations.
- An observational capacity curve for record construction, ledger storage,
  receipt generation, and evidence-bundle validation at increasing sizes.
- CI automation that publishes evaluation summaries and retains their complete
  reports and evidence bundles for inspection.
- Typed, schema-validating constructors for every Claim and Evidence Protocol
  record kind, with immutable value objects for shared provenance, scope,
  authority, oracle, relation, digest, and cost fields.
- A provider-neutral structured-inference protocol and configurable
  OpenAI-compatible Chat Completions client.
- A typed `EvidenceBundleBuilder` and immutable `EvidenceBundleFile` input
  model for materializing closed evidence bundles.
- `itself bundle create` for packaging a ledger, its deterministic receipt,
  declared inputs, supplemental files, and every referenced artifact.
- Atomic bundle creation, default assurance limitations, strict artifact
  mapping, and deterministic creation-time overrides.
- A complete, independently verifiable cache-key diagnosis evidence bundle.
- A compact, deterministic inference-to-evidence example exercising the public
  compatible-endpoint adapter, private response capture, typed lifecycle
  records, external verification, and closed evidence-bundle construction.
- An independent JavaScript conformance runner and CI job covering all
  language-neutral record fixtures, cross-record integrity codes, authorization
  rules, and deterministic bundle replay.
- Deterministic `itself schema export` tooling with a public catalog, schema
  content digests, and canonical hosted URLs under
  `https://greaterexpanse.com/itself/schemas/`.
- A top-level `itself --version` command and an automated package-version
  consistency check.
- Schema guidance defining offline validation, export APIs, checksum
  boundaries, and immutable versioned publication lines.
- Reproducible source-manifest and distribution-metadata checks, with the
  documentation assets, controlled experiments, typing fixtures, and lockfile
  included in source archives.
- An immutable `v0alpha2` schema publication line with RFC 8785 canonical JSON
  Lines, I-JSON conformance vectors, and independent JavaScript verification.
- Configurable evidence-bundle limits for manifest size, inventory size,
  individual and aggregate file bytes, directory traversal, receipts, and
  ledger records.
- Deterministic publication-race tests for schema exports, evidence bundles,
  and content-addressed inference artifacts.

### Changed

- Public positioning now leads with agent assurance and AI-driven engineering
  workflows, while retaining evidence as the formal result of an external check
  in the provider-neutral protocol.
- Project licensing changed from the MIT License to the Mozilla Public License
  2.0.
- Authored Python and JavaScript sources now carry per-file SPDX license and
  Greater Expanse LLC copyright notices.
- Public reference material is grouped behind an audience-oriented
  documentation index instead of competing with the README at repository root.
- The README, evaluation guides, and generated comparison reports now explain
  Itself's assurance boundary and experiment outcomes in plain developer-facing
  language before presenting reproducibility details.
- The public Python schema-export surface uses `export_schemas`, `SchemaExport`,
  and `SchemaExportError`.
- Experimental contract identifiers now use self-contained `urn:itself:...`
  references instead of URLs for schemas that are not part of the public hosted
  catalog.
- Local examples and inference artifacts now stay under the ignored `.itself/`
  workspace, and CI checkouts no longer persist Git credentials.
- Evidence-backed state transitions now require a preceding verdict whose
  subject, scope, relation, evidence, and authorized transition match exactly.
- Current-line ledger and receipt identifiers now use RFC 8785 rather than a
  Python-specific JSON serialization profile.
- Experiment bundle formats now share one validation and filesystem policy;
  repeated schema-diagnostic and runtime-narrowing helpers are centralized.

### Fixed

- Compose conditional `test` and `verdict` rules inside their schema `allOf`
  blocks so independent Draft 2020-12 validators agree on evaluated properties.
- Reject symlinked or permissively accessible inference-artifact destinations
  before writing raw provider responses.
- Closed-bundle artifact records without a digest now produce a stable
  validation error instead of an internal key lookup failure.
- Reject symbolic-link paths at ledger, receipt, and schema-export storage
  boundaries without altering their targets.
- Reject duplicate keys, non-finite values, unsafe integers, negative zero,
  Unicode surrogates, and ambiguous external references at untrusted JSON
  boundaries.
- Refuse credential-bearing plaintext endpoints, redirects, unsafe resource
  paths, conflicting transport overrides, oversized responses, malformed
  streaming events, and incomplete model output.
- Bound evidence-bundle validation before expensive traversal, allocation, or
  hashing, and stream artifact digests instead of loading entire files.
- Publish completed directories with an atomic no-replace operation so a
  destination created concurrently is preserved.
- Keep private experiment-directory creation inside its declared boundary
  without changing permissions on caller-owned ancestors.
