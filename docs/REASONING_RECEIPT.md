# Reasoning Receipt `v0alpha2`

## Purpose

A reasoning receipt is a minimized, deterministic public projection of a valid
Claim and Evidence Protocol ledger. It lets a reviewer answer:

- what an agent or person asserted before the application acted;
- what claims or hypotheses were considered;
- how their epistemic states changed;
- which tests, evidence, verdicts, policies, and actors authorized those changes;
- which source artifacts are referenced;
- which exact ledger the projection came from.

It does not contain private chain-of-thought. It omits artifact contents and the
potentially large or sensitive `result` payloads inside evidence records.

The canonical machine-readable format is
[`reasoning-receipt.schema.json`](https://greaterexpanse.com/itself/schemas/v0alpha2/reasoning-receipt.schema.json).
The repository includes the same
[schema source](../src/itself/schemas/v0alpha2/reasoning-receipt.schema.json) for
offline use.
Receipt version `0.1.0-alpha.2` is independent of the ledger protocol version.

## Deterministic ledger binding

The generator serializes every record in authoritative ledger order using
`rfc8785-jsonl-v1`:

1. the [RFC 8785 JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785);
2. strict interoperable JSON values with duplicate keys, lone surrogates,
   negative zero, non-finite numbers, and lossy numeric inputs rejected;
3. no insignificant whitespace;
4. one object per line, including a final LF byte.

SHA-256 is computed over those exact bytes. The digest appears in
`source_ledger.digest`, and `receipt_id` is `urn:sha256:<ledger-digest>`.
Consequently, the receipt identifier names the canonical source ledger—not the
receipt document itself.

Two byte-different JSON Lines files that decode to the same ordered records have
the same canonical ledger digest. Changing record content or order changes the
digest.

## Contents

The receipt contains:

- protocol versions and record counts;
- claim and hypothesis text, scope, initial state, current state, and transition
  identifiers;
- artifact URIs, media types, titles, and declared digests;
- planned and completed tests, plan lineage, oracles, evidence links, and costs;
- evidence relations and declared authority;
- verdicts and their public rationales;
- every state transition, its authorizer, cited evidence, matching verdict,
  policy, and public reason;
- decisions and their relied-on or unresolved claims;
- explicit assurance bases and limitations.

Ordering follows the source ledger within each projected record kind. The
receipt is a review surface, not a replacement for the ledger.

## Assurance boundary

Generation establishes that the source ledger passed:

- protocol schema conformance;
- cross-record reference integrity;
- deterministic state replay;
- canonical digest construction.

It does not establish factual truth, artifact authenticity, digest correctness
for external artifacts, or the correctness of an oracle, evaluator, reviewer,
or policy.

An artifact reference is provenance. Its presence in a receipt never converts
the artifact—or a model response—into evidence.

## Validation modes

Schema validation checks only receipt shape:

```bash
itself receipt validate receipt.json
```

Ledger-bound validation recomputes the entire receipt and requires exact
equality, detecting schema-valid tampering as well as a mismatched digest:

```bash
itself receipt validate receipt.json --ledger assurance.jsonl
```

Generate a receipt atomically with:

```bash
itself receipt generate assurance.jsonl --output receipt.json
```

`--output` atomically replaces an existing receipt. The command exits with
status 1 without writing when the output path is the source ledger itself,
including a hard link or symbolic link to it. Both commands read ledgers of at
most 128 MiB and 100,000 records, and `receipt validate` reads receipts of at
most 128 MiB; these are the default evidence-bundle ceilings. `receipt generate
--output` refuses to write a receipt larger than that ceiling.

When a source ledger declares external references, pass the same repeated
`--external-ref ID` arguments during generation and ledger-bound validation.
Both commands also accept repeated `--trusted-authorizer ID` arguments, which
refuse evidence-backed transitions authorized by any other declared actor id;
see the [protocol-record guide](PROTOCOL_RECORDS.md#restrict-who-may-authorize-evidence-backed-transitions).
`receipt validate` checks them while it replays the ledger, so it accepts the
option only together with `--ledger`.
