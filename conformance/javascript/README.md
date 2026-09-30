# JavaScript conformance check

This directory contains a small implementation-independent check of the
language-neutral Claim and Evidence Protocol fixtures. It does not import,
execute, or shell out to the Python SDK.

The runner:

- compiles the canonical Draft 2020-12 JSON Schema with Ajv;
- accepts every individual record fixture under `conformance/valid`;
- rejects every record fixture under `conformance/invalid`;
- reads JSON with its own strict parser: bytes must be UTF-8 without a
  byte-order mark, every repeated object key is rejected even when the values
  are equal, a `__proto__` key stays an ordinary member, nesting is limited to
  128 levels, and numbers must be interoperable I-JSON;
- accepts every document under `conformance/json/accept` and rejects every
  document under `conformance/json/reject`, the reader rules the Python
  reference also checks;
- checks the schema's `date-time` and `uri-reference` formats with the same
  grammars as the Python reference (rfc3339-validator after upper-casing, and
  rfc3987's `URI_reference` rule, vendored in `uri-reference.mjs`), so both
  runners accept exactly the same records;
- independently resolves typed cross-record references;
- enforces verdict scope, evidence-relation, and transition-authorization
  policy;
- replays ordered epistemic state deterministically;
- reproduces the RFC 8785 JSON Lines digest used by the reference receipt;
- accepts every valid bundle fixture; and
- rejects each invalid bundle fixture with the expected integrity code.

Run it from this directory with Node.js 20 or later:

```bash
npm ci
npm test
```

This is a cross-language conformance canary, not a second supported SDK. It
demonstrates that the protocol and fixtures are consumable without Python and
guards against accidental coupling between the schema and the reference
implementation.
