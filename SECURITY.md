# Security Policy

## Supported versions

Itself SDK is a pre-1.0 SDK. Security fixes are made on the
current `main` branch; no released version is presently covered by a long-term
support commitment.

## Reporting a vulnerability

Do not disclose a suspected vulnerability in a public issue. Use GitHub's
private vulnerability reporting flow from the repository's **Security** tab.
Include:

- the affected version or commit;
- the security or assurance property that can be violated;
- a minimal reproduction when safe to provide;
- the expected and observed behavior;
- any known mitigations.

Reports may cover conventional software vulnerabilities or protocol flaws that
could let an untrusted actor fabricate evidence, bypass authority rules, alter
ledger meaning, or produce a misleading reasoning receipt.

The protocol threat model and privacy boundary are documented in
the [protocol specification](docs/SPECIFICATION.md). A structurally valid record
or receipt is not, by itself, proof that an underlying real-world claim is true.
