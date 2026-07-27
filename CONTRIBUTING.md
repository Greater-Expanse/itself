# Contributing to Itself SDK

Itself SDK is a pre-1.0 open-source assurance SDK for AI agents and AI-driven
workflows. Contributions should strengthen an executable guarantee, a
reproducible experiment, an agent integration, or the clarity of the protocol
boundary.

## Development setup

Install [uv](https://docs.astral.sh/uv/), clone the repository, and run:

```bash
uv sync --locked --extra test --extra typecheck --extra lint --extra release
uv run --frozen --extra test pytest
uv run --frozen --extra lint ruff check .
uv run --frozen --extra lint ruff format --check .
uv run --frozen --extra typecheck mypy
uv run --frozen --extra typecheck pyright
uv run --frozen --extra typecheck pyright --verifytypes itself --ignoreexternal
uv run --frozen --extra release check-manifest
uv build
uv run --frozen --extra release twine check --strict dist/*

cd conformance/javascript
npm ci --ignore-scripts
npm test
```

The test suite does not require a model provider, network service, or hosted
account.

## Evaluation runs

Run the assembled SDK exercise and observational capacity curve with fresh
output paths:

```bash
mkdir -p .itself/evaluations
uv run --frozen python -m evaluations.sdk_exercise \
  --output .itself/evaluations/sdk-exercise
uv run --frozen python -m evaluations.capacity_curve \
  --output .itself/evaluations/capacity-curve
```

Add or strengthen an evaluation when a change affects a cross-component
guarantee that is not clear from an isolated test. A mutation check should name
the boundary it challenges and the exact rejection it expects. A capacity
observation must remain distinct from a performance threshold unless a
documented benchmark method and justified regression budget are introduced.

See the [evaluation guide](docs/EVALUATIONS.md) for report semantics and
retained artifacts.

## Feedback and changes

Use the repository's issue forms for reproducible bugs and protocol or
integration feedback. Ground protocol requests in a concrete workflow and an
executable acceptance case. Do not open a public issue for a suspected
vulnerability; follow [SECURITY.md](SECURITY.md) instead.

## Change expectations

- Keep model assertions distinct from evidence, verdict authority, and task
  performance.
- Accompany externally observable behavior with tests.
- Record user-visible SDK or format changes in `CHANGELOG.md`.
- Accompany protocol changes with specification text, schema changes, and valid
  and invalid conformance fixtures where applicable.
- Version the ledger protocol, reasoning-receipt format, and diagnostician
  contract independently.
- Prefer a narrow executable result over an untested platform abstraction.
- Do not add private chain-of-thought or secrets to fixtures, logs, or receipts.

Before submitting a change, run the complete local checks above. Explain what
guarantee or capability the change addresses and what the tests establish.

## Licensing

By contributing, you agree that your contribution is licensed under the
repository's [Mozilla Public License 2.0](LICENSE).

Authored Python and JavaScript source files carry per-file license and
copyright notices. Place them immediately after a shebang, when present:

```text
# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC
```

Use the equivalent `//` comment form for JavaScript. Do not add comments to
formats such as JSON that do not support them; the repository license covers
those files.
