# Versioning and Compatibility

Itself SDK contains several independently versioned surfaces. A package release
does not implicitly change the protocol or artifact formats it implements.

## Version lines

### Python distribution

The `itself` distribution uses PEP 440 versions. The package version in
`pyproject.toml` and `itself.__version__` must match.

Before `1.0.0`, minor and prerelease versions may change the Python API. Such
changes must be documented and accompanied by migration guidance when an earlier
public release is affected.

### Claim and Evidence Protocol

Every protocol record declares `protocol_version`. Validators accept only
versions whose exact semantics they implement. An incompatible schema or
semantic change receives a new protocol version and schema identifier; an
existing published version must not silently acquire new meaning.

### Reasoning receipts

Every reasoning receipt declares `receipt_version`. Receipt compatibility is
independent of both the Python distribution and ledger protocol versions.
Canonicalization identifiers and digest interpretation are immutable within a
receipt version.

### Evidence bundles

Every closed bundle declares `bundle_version`. Its manifest schema, inventory
roles, identity derivation, artifact-binding rules, and required files are
immutable within that version. Bundle-format compatibility is independent of
the Python builder and validator API versions.

### Diagnostician exchange

Provider-neutral diagnosis requests and results declare `contract_version`.
Changes that would make a previously conforming message invalid, or change its
interpretation, receive a new contract version.

Contract `0.2.0` is intentionally incompatible with `0.1.0`. It renames the
model-output concept from an ambiguous commercial term to **model assertion**:
Python callers use `DiagnosisAssertion`, `NarrativeAssertion`, and
`DiagnosisResult.assertion`; JSON messages use the `assertion` property; and
schema filenames use `*-assertion.schema.json`. No compatibility aliases are
provided because this remains a pre-1.0 surface.

## Published schema identifiers

A schema publication line names one exact set of schema documents. It is
independent of the protocol, receipt, bundle, and Python distribution versions.

The bytes and `$id` values under a published line are immutable. A path such as
`/itself/schemas/v0alpha/` is not a rolling alias. Any change to a published
schema requires a new publication line and new `$id`. Existing versioned URLs
must continue to resolve to their original bytes.

The unversioned schema catalog may advertise a later publication line. Hosted
schemas must remain byte-identical to the corresponding packaged copies. Local
validation must not require access to the hosted catalog.

## Current format line

The current schemas are published under `v0alpha2` and describe:

| Surface | Current version |
| --- | --- |
| Claim and Evidence Protocol | `0.1.0-alpha.3` |
| Reasoning receipt | `0.1.0-alpha.2` |
| Evidence bundle | `0.1.0-alpha.2` |
| Ledger canonicalization | `rfc8785-jsonl-v1` |

The earlier `v0alpha` schema bytes remain packaged and exportable, but the
current validators and constructors implement `v0alpha2`.

The protocol migration requires every claim and hypothesis to begin in
`proposed`, requires evidence-backed transitions to cite a prior matching
verdict, and validates verdict scope and evidence relations. The receipt
migration replaces the earlier implementation-specific JSON serializer with
RFC 8785 canonical JSON Lines. The bundle version advances because a closed
bundle now carries the new receipt format.

## Release discipline

A release must:

1. update both Python package version declarations;
2. document user-visible changes and migrations;
3. pass tests, strict type checks, lint, formatting, and package-build checks;
4. include the schemas, `py.typed` marker, and license in built artifacts;
5. verify that any published schema line matches the packaged bytes;
6. use the Git tag `v` followed by the normalized distribution version, such
   as `v0.2.0`.

Publishing a GitHub release for that tag runs the release workflow. It refuses
a tag that does not name the package version, builds and checks the
distributions, and uploads them to PyPI through trusted publishing once a
maintainer approves the protected `pypi` environment.

No compatibility claim extends beyond the explicit surface and version named in
that claim. In particular, structural validation does not establish factual
truth, domain fitness, or compatibility with an undeclared model or harness.
