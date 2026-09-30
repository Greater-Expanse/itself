# Changelog

User-visible SDK and format changes are recorded here. Protocol, receipt,
evidence-bundle, and experiment-contract versions remain independent of the
Python distribution version.

## 0.2.0

The first public release of `itself`. It implements these format
versions, each versioned independently of the package:

| Surface | Version |
| --- | --- |
| Claim and Evidence Protocol | `0.1.0-alpha.3` |
| Reasoning receipt | `0.1.0-alpha.2` |
| Evidence bundle | `0.1.0-alpha.2` |
| Schema publication line | `v0alpha2` |
| Ledger canonicalization | `rfc8785-jsonl-v1` |
| Chat Completions adapter | `0.3.1` |
| Decision-model adapter | `0.1.0` |

### Added

- A CI integration cookbook with a provider-free agent-change gate, independent
  evidence-bundle verification, reasoning-receipt publication, and thin GitHub
  Actions and GitLab CI wrappers.
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
- A decision-model adapter for endpoints that implement the `/v1/systemone`
  decision format. `ChoiceQuestion`, `NoulQuestion`, and `ScoreQuestion`
  declare typed questions; `DecisionModelEndpoint` applies the Chat Completions
  endpoint's transport rules; and `DecisionModelClient` captures each exact
  request and bounded response, validates every distribution locally, and by
  default asks each ordered question with its options as declared and reversed,
  reporting the order gap and whether the most likely option flipped. A
  failure after a request is captured raises `DecisionModelError`, which
  references the captured request and, inside `decide`, the attempts that
  completed. Probabilities stay in artifacts, so protocol records and schemas
  are unchanged.
- `DecisionModelAdapter`, a provider-neutral protocol for decision services
  with other wire formats, and a decision-model guide.
- `decode_jsonl_records` in `itself.ledger` and `decode_receipt_document` in
  `itself.receipts`, which decode ledger and receipt bytes under the same rules
  as the file loaders.
- An opt-in `trusted_authorizers` for `BundleValidator` and
  `EvidenceBundleValidator`, and a repeatable `--trusted-authorizer ID` option
  on the `ledger`, `receipt`, and `bundle` commands. Validation then refuses an
  evidence-backed transition whose `authorized_by` declares any other actor id,
  with the new `untrusted_authorizer` integrity code. The list compares
  declared ids and establishes no identity.
- `EVIDENCE_BACKED_STATUSES`, the statuses a transition reaches only with
  evidence, exported from `itself`.
- A `max_bytes` limit for `JsonReceiptStore.write`, which refuses a larger
  receipt before writing anything.
- An opt-in `deadline_seconds` on `OpenAICompatibleEndpoint`,
  `DecisionModelEndpoint`, and `HttpRequest`. The built-in transport shuts a
  request down once it passes, so a server that keeps a connection busy with
  keep-alive comments or chunked trailer lines can no longer hold a call open
  past the per-read `timeout_seconds`. The deadline runs from the moment the
  socket connects, so it also covers a proxy's `CONNECT` exchange and the TLS
  handshake, and a request with a deadline trusts the same certificates as one
  without.
- A test that pins the SHA-256 digest of every packaged schema file, so a
  published schema line cannot change in place.
- Shared JSON reader fixtures under `conformance/json/accept` and
  `conformance/json/reject` that both the Python reference and the JavaScript
  validator must accept or refuse, plus a valid record fixture with an IPv6
  artifact host, invalid record fixtures for timestamps with a space separator,
  a leap second, or a trailing newline and for URIs with a malformed port, an
  IPv6 literal written with `{,2}`, or an IPv4 octet with a leading zero inside
  an IPv6 literal, and invalid bundle fixtures for a `__proto__` scope dimension
  and a blank transition subject. The reader fixtures also cover a zero and an
  underflowing number written with exponents beyond the range of any float, and
  an escaped `NUL` with uppercase hexadecimal digits.

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
- Appending to a `Ledger` or `JsonlLedgerStore` schema-validates only the new
  records instead of the whole history, so an append no longer slows down in
  proportion to the ledger's size.
- `ProtocolValidator` validates each record against the schema branch for its
  `kind`, so errors name the failing field instead of reporting that a record
  matches none of the nine record kinds, and it compiles the packaged schema
  once per process. The set of accepted records is unchanged: a schema whose
  shape would make the per-kind branch accept a different set, such as one with
  a root `$ref` or a branch with keywords beside its `$ref`, is applied whole.
  Tests check this against every conformance fixture, systematic mutations of
  them, and three custom schemas.
- Strict JSON decoding requires UTF-8 bytes without a byte-order mark and
  rejects values nested more than 128 levels deep with `IJsonError` instead of
  an uncaught `RecursionError`. `ProtocolValidator` applies the same limit and
  refuses a value that contains itself, so a ledger never stores a record its
  own reader would refuse.
- The built-in HTTP transport never sends loopback requests through a proxy and
  accepts only `http` and `https` URLs; requests to other hosts still honor the
  standard proxy environment. Endpoints refuse a `base_url` or `resource_path`
  with whitespace, control, or non-ASCII characters, reserve the `Host`,
  `Content-Length`, and `Transfer-Encoding` headers, and limit
  `timeout_seconds` and `deadline_seconds` to `threading.TIMEOUT_MAX`. A URL or
  header value that HTTP cannot carry is a configuration failure instead of a
  retryable transport failure, and a credential value that ends in whitespace
  is refused like one with a line break. The Chat Completions adapter version
  is `0.3.1`.
- `EvidenceBundleValidator` parses the ledger and receipt from the same bytes
  it checks against the manifest. `EvidenceBundleBuilder.build` validates the
  staged bundle once and then re-hashes the published copy instead of
  validating it a second time.
- CLI commands that read ledgers or receipts (`bundle create`, `ledger
  validate`, `ledger replay`, `ledger summary`, `receipt generate`, and
  `receipt validate`) enforce the default evidence-bundle ceilings of 100,000
  records or 128 MiB per ledger and 128 MiB per receipt, and `receipt generate
  --output` refuses to write a receipt over its ceiling. Larger ledgers remain
  available through the Python API.
- Schema format checks use jsonschema's `format-nongpl` extra, so the SDK
  installs no GPL-licensed dependency, and `uri-reference` is checked with the
  SDK's own pattern for RFC 3986's URI-reference rule, whichever URI library is
  installed. The pattern follows the RFC in two cases the earlier checker
  decided the other way: an IPv4 octet with a leading zero inside an IPv6
  literal is refused, and an IPvFuture literal may begin with an uppercase
  `V`.
- The default receipt ceiling in `EvidenceBundleLimits` is 128 MiB, up from
  64 MiB, matching the ledger ceiling, because a receipt can be nearly as large
  as its ledger.
- `TransitionRequest` validates its fields when it is constructed: an unknown
  status, actor type, or role, or `None` for any of them, raises `ValueError`,
  as does an `evidence_refs` that is a single string or `None`.
- `bundle create` assigns media types from a fixed suffix table owned by the
  SDK instead of the host's `mimetypes` database, so regenerating a bundle with
  `--created-at` gives the same identifier on every host.
- The protocol specification now states that a completed test MUST cite its
  evidence, as the `v0alpha2` schema already requires; that records citing each
  other are appended together; that the reference reader limits nesting to 128
  levels, which writers must not exceed; that each record declares actor types
  rather than binding them to an identity; and that a trust list compares
  declared identifiers.

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
- Refuse a credential value that contains a line break or other character not
  allowed in an HTTP header as a configuration failure before any request.
  Previously `http.client` rejected it inside a chained, retryable transport
  error whose traceback could print the secret. Extra header values must be
  printable ASCII, and `base_url` must carry a valid port.
- `validate_transition` and `Actor` convert plain strings decoded from JSON to
  enum members, so a string-typed model actor can no longer authorize an
  evidence-backed transition through the public function.
- A bare string passed as a reference list, such as `evidence_refs="ev-1"`,
  raises `RecordConstructionError` instead of being stored as one reference per
  character.
- Report a response body that ends before its declared length, and other
  `http.client` protocol errors, as retryable transport failures.
- `InferenceError` survives pickling, so a failure raised in a worker process
  reaches the caller with its failure class, status code, and artifact.
- `DirectoryArtifactSink` calls `os.fchmod` only on POSIX; Python 3.11 and 3.12
  lack it on Windows.
- A streamed content part without text is a response-envelope failure instead
  of an uncaught `KeyError`.
- `receipt generate --output` refuses to replace its own source ledger.
- The bundle builder rejects `%`, `?`, and `#` in file paths, which URL
  resolvers would decode or truncate.
- A `size_bytes` in a bundle manifest that is not a JSON integer, such as
  `3302.0`, or an inventory path that cannot be inspected, raises
  `EvidenceBundleValidationError`.
- The strict reader reports a number whose exponent is beyond `Decimal`'s
  range, such as `1e-99999999999999999999`, as an `IJsonError` instead of
  raising `decimal.InvalidOperation`, and reads a zero with such an exponent
  as zero, as the JavaScript validator does.
- `EvidenceBundleBuilder.build` removes a published bundle whose files no
  longer match what it verified before it raises, so no unverified bundle is
  left at the destination.
- `status_transition_record` accepts plain status strings and reports an
  unknown status as `RecordConstructionError`.
- The JavaScript conformance validator parses JSON with its own strict parser.
  It rejects repeated keys even when their values are equal, keeps `__proto__`
  keys so its ledger digests match Python's, rejects invalid UTF-8 and
  byte-order marks instead of decoding them, and limits nesting to 128 levels.
  It checks `date-time` with the Python reference's grammar and
  `uri-reference` with the reference's own RFC 3986 pattern, refuses blank
  transition subjects as the reference does, and no longer accepts a lone high
  surrogate at the end of a string. `lossless-json` is no longer a dependency.
- Protocol, receipt, and bundle validation refuse a `date-time` or
  `uri-reference` value that ends in a newline, which the underlying regular
  expressions accepted.
