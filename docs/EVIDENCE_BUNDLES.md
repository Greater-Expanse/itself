# Evidence Bundles

An Itself evidence bundle is a portable, closed directory that binds a
validated evidence ledger to its deterministic reasoning receipt and every
artifact referenced by that ledger. The bundle manifest inventories the exact
bytes, media type, role, and SHA-256 digest of each file.

This is the exchange boundary for handing an evidence history to another
person, service, repository, or CI job without asking that consumer to trust the
process that assembled it.

In an AI-driven engineering workflow, a bundle can retain the agent assertion,
declared checks, CI observations, evaluation, merge or release decision, and
reasoning receipt as one validated artifact.

## Create a bundle from the CLI

Start with a valid JSON Lines ledger. Every `artifact_reference` record in a
closed bundle must:

- use a normalized relative `uri`, such as `artifacts/model-assertion.json`;
- declare a media type and SHA-256 digest;
- have exactly one source-file mapping supplied to the builder.

The CLI maps each record identifier to its source file:

```bash
mkdir -p .itself/examples

uv run --frozen itself bundle create \
  examples/cache-key-diagnosis/bundle/ledger.jsonl \
  .itself/examples/cache-key-bundle \
  --title "Cache-key diagnosis" \
  --created-at 2026-07-23T12:00:00Z \
  --artifact artifact-diagnostician-output=examples/cache-key-diagnosis/bundle/artifacts/model-assertion.json \
  --input examples/cache-key-diagnosis/bundle/inputs/diagnosis-request.json \
  --limitation "The causal oracle is authoritative only within this controlled case."
```

`--artifact` must be repeated once for every artifact-reference record in the
ledger. Input files are placed under `inputs/`, and supplemental files are
placed under `supplemental/`. Their media types are inferred from their
filenames, with `application/octet-stream` as the fallback.

The creation time defaults to the current UTC time. Supplying `--created-at`
makes deterministic regeneration possible. The builder includes two baseline
limitations and appends each `--limitation` value.

The destination must not exist. A successful command prints the record count,
file count, and content-derived bundle identifier.

## Create a bundle from Python

The Python API accepts immutable file contents and explicit bundle paths:

```python
from datetime import UTC, datetime

from itself import (
    EvidenceBundleBuilder,
    EvidenceBundleFile,
    EvidenceBundleFileRole,
    JsonlLedgerStore,
)

ledger = JsonlLedgerStore("run/ledger.jsonl").load()
assertion = EvidenceBundleFile.from_path(
    "run/model-assertion.json",
    path="artifacts/model-assertion.json",
    media_type="application/json",
    role=EvidenceBundleFileRole.ARTIFACT,
    record_ref="artifact-model-assertion",
)
request = EvidenceBundleFile.from_path(
    "run/request.json",
    path="inputs/request.json",
    media_type="application/json",
    role=EvidenceBundleFileRole.INPUT,
)

verified = EvidenceBundleBuilder().build(
    "run/evidence-bundle",
    ledger=ledger,
    title="Incident diagnosis",
    created_at=datetime(2026, 7, 23, 12, tzinfo=UTC),
    files=(request, assertion),
    limitations=(
        "The evaluator is authoritative only for the declared test environment.",
    ),
)
print(verified.manifest["bundle_id"])
```

`EvidenceBundleFile.from_path` snapshots a regular, non-symlink source file.
Callers that already hold artifact bytes can instantiate `EvidenceBundleFile`
directly.

## Bundle integrity

Before making a completed bundle available, the builder verifies:

- the manifest against the versioned
  [evidence-bundle schema](https://greaterexpanse.com/itself/schemas/v0alpha2/evidence-bundle.schema.json);
- the complete file inventory, byte lengths, and SHA-256 digests;
- every protocol record and cross-record reference in the ledger;
- deterministic epistemic-state replay;
- the reasoning receipt by exact recomputation from the ledger;
- a one-to-one binding between artifact-reference records and materialized
  artifact files.

Only a fully verified bundle becomes available at the destination. If validation
or writing fails, no partial bundle is left there. Final publication is atomic
and refuses replacement, including when another process creates the destination
during bundle construction.

## Resource limits

Bundle verification treats the directory and its manifest as untrusted input.
It checks declared and observed sizes before parsing large documents, streams
file digests in bounded chunks, limits ledger decoding, and stops directory
traversal after a configured number of entries.

The default `EvidenceBundleLimits` are:

| Resource | Default maximum |
| --- | ---: |
| Manifest | 4 MiB |
| Inventoried files | 1,024 |
| Files and directories traversed | 4,096 |
| One general file | 256 MiB |
| All inventoried files | 512 MiB |
| Ledger | 128 MiB |
| Reasoning receipt | 64 MiB |
| Ledger records | 100,000 |

Applications should lower these ceilings when their expected artifacts are
smaller. A deliberate larger deployment can raise them explicitly:

```python
from itself import EvidenceBundleLimits, EvidenceBundleValidator

validator = EvidenceBundleValidator(
    limits=EvidenceBundleLimits(
        max_file_bytes=16 * 1024 * 1024,
        max_total_bytes=64 * 1024 * 1024,
        max_ledger_records=10_000,
    )
)
verified = validator.validate("run/evidence-bundle")
```

`EvidenceBundleBuilder` accepts a configured validator through its `validator`
field and applies the same limits before creating its staging directory.
`EvidenceBundleFile.from_path` also accepts `max_bytes` and defaults to the
general 256 MiB file ceiling.

The independent verification command uses the same public format but does not
trust builder state:

```bash
uv run --frozen itself bundle validate run/evidence-bundle
```

## Assurance and privacy boundary

A valid bundle proves that the supplied bytes, records, receipt, references, and
digests agree under the declared Itself format. It does not prove that an
assertion is true, that evidence was honestly produced, or that an evaluator or
authority was correct.

The builder performs no secret scanning, redaction, encryption, or signing.
Anything supplied as an input, artifact, or supplemental file becomes part of
the closed bundle. Raw provider responses captured for private inspection
should be omitted unless they have been explicitly reviewed for publication.

The generic inference adapter may initially capture a response at a local file
URI. Before including that response in a closed bundle, the application should
create an `artifact_reference` record with a bundle-relative URI and the same
media type and digest. This makes the publication decision explicit rather than
silently copying every private inference artifact.
