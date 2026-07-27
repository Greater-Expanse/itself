# Publish a reasoning receipt

Use this recipe when a workflow already has a validated JSON Lines ledger and
wants to expose a compact, deterministic account of its subjects, tests,
evidence, verdicts, transitions, and decisions.

```bash
itself receipt generate ledger.jsonl --output reasoning-receipt.json
itself receipt validate reasoning-receipt.json --ledger ledger.jsonl
```

The platform examples also generate a human-readable ledger summary and retain
both projections:

- [GitHub Actions](github-actions.yml)
- [GitLab CI](gitlab-ci.yml)

Replace `ASSURANCE_LEDGER` with the producing job's ledger path. If the workflow
already emits a closed evidence bundle, use the receipt inside that bundle
instead of generating another copy.

A receipt includes the canonical ledger digest and enough projection data for
review or indexing. Validating its exact binding still requires the source
ledger. Publishing a receipt without the ledger can be useful when the ledger
must remain in a controlled store, but consumers can then validate only the
receipt's schema—not independently recompute its binding.

A receipt is not a model chain-of-thought transcript and should not contain
one. It records declared protocol objects and public rationales.
