# Reproducible evaluations

Itself includes repository-level evaluation suites that exercise complete SDK
workflows and preserve what happened in a versioned, machine-readable report.
They complement unit and conformance tests: a unit test isolates behavior,
whereas an evaluation run assembles records, ledgers, receipts, artifacts, and
evidence bundles into an inspectable result.

Put simply, these suites ask whether a developer can rely on the SDK to
preserve an evidence trail, reject known-invalid data and tampering, and
reconstruct how a recorded decision was reached. They test Itself's behavior,
not whether a model or evidence source is telling the truth.

The current suites are offline and deterministic apart from measured durations
and runtime metadata. They require no model endpoint, provider credential, or
network service.

## Current suites

| Suite | Purpose | Result semantics |
|---|---|---|
| `sdk-exercise` | Exercise the reference lifecycle and challenge its validation boundaries with known-invalid records, ledgers, and bundle mutations. | Every check is an executable guarantee. A failed check makes the run fail. |
| `capacity-curve` | Observe construction, validation, storage, receipt, and bundle behavior at increasing ledger sizes. | Successful samples are observations, not performance passes. Incorrect behavior still makes the run fail. |

### SDK exercise

The SDK exercise performs 31 checks:

- exports and identifies all six schemas across the current and retained
  immutable publication lines;
- runs a complete inference-to-evidence lifecycle containing all nine protocol
  record kinds;
- recomputes the reasoning receipt from its ledger;
- independently validates every inventoried evidence-bundle byte;
- reproduces the deterministic lifecycle bundle byte for byte;
- rejects eleven invalid record fixtures;
- rejects eleven invalid ledgers with their exact integrity codes; and
- rejects artifact tampering, a re-digested but ledger-divergent receipt, an
  uninventoried file, and a symbolic-link artifact.

The retained lifecycle uses a reviewed deterministic model-response fixture.
This makes SDK behavior reproducible; it does not measure the factual quality
of a hosted model.

### Capacity curve

The capacity curve uses independent unverified claims at 10, 100, and 1,000
records by default. For each size it records:

- typed record construction and ledger validation;
- batch JSON Lines write and load;
- one marginal append to an existing ledger;
- reasoning-receipt construction and validation;
- evidence-bundle construction and independent validation; and
- ledger and receipt byte sizes.

These measurements describe one run in its recorded environment. They are not
portable performance claims or regression thresholds. Repeat `--size` to
select a different curve.

## Run the evaluations

From a repository checkout:

```bash
uv sync --locked
mkdir -p .itself/evaluations

uv run --frozen python -m evaluations.sdk_exercise \
  --output .itself/evaluations/sdk-exercise

uv run --frozen python -m evaluations.capacity_curve \
  --output .itself/evaluations/capacity-curve
```

Each output path must be new. Pass `--source-revision REVISION` to bind the
report to a commit or other source identifier. In GitHub Actions, the workflow
uses the triggering commit SHA.

Each suite atomically publishes this result shape:

```text
suite-output/
├── report.json
├── report.md
└── artifacts/
    └── retained evidence bundle(s)
```

`report.json` is the canonical result. It conforms to
[`evaluations/contracts/v1/evaluation-report.schema.json`](../evaluations/contracts/v1/evaluation-report.schema.json)
and has a `run_id` derived from every other report field. `report.md` is
rendered from the same typed report object for human inspection.

## Interpret a report

Individual checks use four states:

- `passed`: an asserted guarantee held;
- `failed`: an asserted guarantee or required operation did not hold;
- `observed`: a measurement was recorded without a pass threshold; and
- `skipped`: the environment could not exercise an optional capability.

An aggregate `failed` outcome means at least one check failed. An aggregate
`passed` outcome means the run contained a passing guarantee and no failures.
An aggregate `observational` outcome means the run contained observations but
no guarantee passes or failures.

Always read the report's limitations alongside its checks. Protocol validity,
digest integrity, deterministic replay, and successful mutation rejection do
not establish that a real-world assertion is true or that an evidence source
is trustworthy.

## Continuous integration

CI runs both suites from a clean checkout. Their Markdown reports appear in the
job summary, and the complete result tree is retained as a workflow artifact
for 14 days. This preserves the evidence bundles needed to inspect or
independently revalidate a run instead of retaining only a green status.

Live model behavior is a separate evaluation concern. Provider-backed trials
must record the model identity, endpoint configuration, sampling controls,
inputs, raw response artifacts, repetitions, scoring method, and limitations.
Review those artifacts for sensitive content before sharing them.
