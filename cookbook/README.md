# Itself integration cookbook

This cookbook shows where Itself fits in ordinary engineering automation. Start
with the agent-change gate: it takes a model assertion, runs a check chosen by
the repository owner, and leaves a replayable record of why software was or was
not allowed to proceed.

The recipes use the same Itself CLI and bundle formats on GitHub Actions,
GitLab CI, and a local shell. The platform files are thin wrappers. The
assurance logic remains portable.

The checked-in wrappers install `.` so they can be exercised from this source
checkout. When copying one into another repository, set
`ITSELF_INSTALL_SPEC` to an immutable package version or locked VCS revision
available to that runner. Do not install assurance code from a moving branch.

## Choose a recipe

| Goal | Recipe | Result |
| --- | --- | --- |
| Check an agent-authored change before acting | [Gate an agent change](agent-change-gate/README.md) | A CI status, evidence bundle, reasoning receipt, and human-readable summary |
| Inspect a bundle produced elsewhere | [Verify an evidence bundle](verify-evidence-bundle/README.md) | Independent validation of file digests, records, references, replay, and receipt binding |
| Expose a compact account of a ledger | [Publish a reasoning receipt](publish-reasoning-receipt/README.md) | A deterministic receipt and ledger summary for people or downstream automation |

The first recipe is the best starting point for most teams. The other two show
how to move and inspect its outputs across job, service, and review boundaries.

## Run the agent-change example

From an Itself source checkout:

```bash
output=".itself/cookbook-pass-$(date +%s)"

uv run --frozen python cookbook/agent-change-gate/run.py \
  --assertion cookbook/fixtures/model-assertion.json \
  --output "$output" \
  --source-revision example-revision \
  --check-name "example process check" \
  --model-actor-id example-agent \
  --model-implementation example-agent@1 \
  -- python -c "raise SystemExit(0)"

uv run --frozen itself bundle validate "$output/bundle"
uv run --frozen itself receipt validate \
  "$output/bundle/reasoning-receipt.json" \
  --ledger "$output/bundle/ledger.jsonl"
```

Change the final `0` to `7` to observe a rejected decision. The runner returns a
failing CI status but still records the contradictory observation, refuted
claim, rejected decision, and valid bundle.

## The model-owned input

The model assertion has a deliberately narrow contract:

```json
{
  "assertion_id": "agent-change-passes-required-checks",
  "expected_exit_code": 0,
  "text": "The proposed source revision passes the repository's required checks."
}
```

The model does not supply the command. A repository owner chooses that command
in reviewed CI configuration. The runner rejects unknown assertion fields
before starting a process, executes the configured argument vector without a
shell, and compares the observed exit code with the declared expectation.

This separation is the point of the recipe:

```text
model owns                       repository policy owns
assertion + expected result  →   command + runner + evaluator + action gate
```

## What the runner writes

Each invocation uses a new output path and writes:

```text
OUTPUT/
├── bundle/
│   ├── artifacts/
│   │   ├── check-result.json
│   │   └── model-assertion.json
│   ├── bundle.json
│   ├── ledger.jsonl
│   └── reasoning-receipt.json
├── outcome.json
└── summary.md
```

`outcome.json` is a small CI-facing projection. `summary.md` is for job
summaries and reviewers. The bundle is the authoritative portable artifact.

The built-in exit-code evaluator is intentionally modest. It demonstrates the
integration boundary without pretending that every code review, security
finding, or incident diagnosis can be judged by one generic oracle. A real
integration can replace it with domain checks while retaining the same claim,
test, evidence, verdict, decision, ledger, and receipt boundaries.

## CI security and privacy

The GitHub examples use read-only token permissions, immutable action commit
pins, and the `pull_request` event. They do not use `pull_request_target` or
send secrets to code from an untrusted change. See GitHub's
[secure-use guidance](https://docs.github.com/en/actions/reference/security/secure-use)
before adapting a workflow with elevated permissions or self-hosted runners.

Both platforms retain assurance output even when a gate rejects the change.
GitHub describes this use of
[workflow artifacts](https://docs.github.com/en/actions/concepts/workflows-and-actions/workflow-artifacts);
GitLab provides equivalent
[job artifacts](https://docs.gitlab.com/ci/jobs/job_artifacts/). Artifact
visibility and retention remain properties of the host configuration.

An evidence bundle can contain model output, source identifiers, check names,
and observations. Review those contents under the same data-handling policy as
CI logs and test reports. Itself validates the bytes and declared relationships;
it does not redact, encrypt, or prove that an external check is sufficient.
