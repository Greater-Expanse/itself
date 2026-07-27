# JavaScript conformance check

This directory contains a small implementation-independent check of the
language-neutral Claim and Evidence Protocol fixtures. It does not import,
execute, or shell out to the Python SDK.

The runner:

- compiles the canonical Draft 2020-12 JSON Schema with Ajv;
- accepts every individual record fixture under `conformance/valid`;
- rejects every record fixture under `conformance/invalid`;
- rejects duplicate object keys and non-interoperable JSON inputs;
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
