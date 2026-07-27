# Canonical Schemas

Itself distributes its canonical JSON Schemas inside the Python package and
publishes the same bytes at
[`greaterexpanse.com/itself/schemas/`](https://greaterexpanse.com/itself/schemas/).
The package remains sufficient for local validation. The hosted copies provide
a stable discovery and exchange surface for other languages and tools.

## Runtime boundary

`ProtocolValidator`, `ReasoningReceiptValidator`, and
`EvidenceBundleValidator` load schemas from the installed package. They do not
make HTTP requests or require the public catalog.

Applications can therefore validate records, receipts, and bundles without
network access. A non-Python implementation can vendor the published files or
retrieve them during its own installation process.

## Published resources

The current publication line is `v0alpha2`. The original `v0alpha` line remains
available at its immutable URLs for consumers of the earlier formats.

| Resource | URL |
| --- | --- |
| Discovery catalog | <https://greaterexpanse.com/itself/schemas/> |
| `v0alpha2` catalog | <https://greaterexpanse.com/itself/schemas/v0alpha2/> |
| Claim and Evidence Protocol | <https://greaterexpanse.com/itself/schemas/v0alpha2/protocol.schema.json> |
| Reasoning receipt | <https://greaterexpanse.com/itself/schemas/v0alpha2/reasoning-receipt.schema.json> |
| Evidence bundle | <https://greaterexpanse.com/itself/schemas/v0alpha2/evidence-bundle.schema.json> |
| SHA-256 inventory | <https://greaterexpanse.com/itself/schemas/v0alpha2/SHA256SUMS> |
| Earlier `v0alpha` catalog | <https://greaterexpanse.com/itself/schemas/v0alpha/> |

Each schema declares its public URL as its JSON Schema `$id`. Each catalog entry
also records the schema title, relative path, and SHA-256 digest.

The server returns schema documents as `application/schema+json`. It permits
cross-origin reads so browser-based tools can load the public files.

## Publication-line policy

A publication line names one exact set of schema documents. It is independent
of `protocol_version`, `receipt_version`, and `bundle_version`.

The bytes and `$id` values under a published line are immutable. Neither
`v0alpha` nor `v0alpha2` is a rolling alias. A change to any published schema
requires a new publication line and new `$id`.

The unversioned discovery catalog can advertise a later publication line.
Existing versioned URLs must continue to resolve to their original bytes.

## Export from the CLI

The installed SDK can materialize the complete static tree:

```bash
itself schema export ./schema-export
```

The destination must not exist. The command publishes the complete export with
an atomic no-replace operation, including when another process creates the
destination during export.

```text
schema-export/
└── itself/
    └── schemas/
        ├── index.json
        ├── v0alpha/
        │   ├── SHA256SUMS
        │   ├── evidence-bundle.schema.json
        │   ├── index.json
        │   ├── protocol.schema.json
        │   └── reasoning-receipt.schema.json
        └── v0alpha2/
            ├── SHA256SUMS
            ├── evidence-bundle.schema.json
            ├── index.json
            ├── protocol.schema.json
            └── reasoning-receipt.schema.json
```

Use the exported files when an integration needs a local or vendored schema
copy.

## Export from Python

```python
from itself import export_schemas

snapshot = export_schemas("./schema-export")
for schema in snapshot.schemas:
    print(schema.schema_id, schema.sha256)
```

`SchemaExport` reports the destination and the immutable
`PublishedSchema` metadata for each exported schema.

## Checksum boundary

The catalog and `SHA256SUMS` let consumers confirm that two schema files contain
the same bytes. A matching digest does not authenticate the publisher, establish
factual truth, or prove that a schema is suitable for a specific domain.
