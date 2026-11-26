# Gate an agent change

Use this recipe when an AI agent opens or updates a pull or merge request and
software must decide whether a repository-owned check supports the agent's
assertion.

The reference runner:

1. strictly parses the model assertion;
2. runs a command chosen in reviewed CI configuration;
3. records the command outcome as external evidence;
4. applies an explicit exit-code policy;
5. creates a verdict and authorized decision;
6. emits a closed evidence bundle and reasoning receipt;
7. returns a status that branch or merge policy can require.

It never asks a model to grade its own assertion.

## Adapt the recipe

Copy [`run.py`](run.py) into a stable tools directory in the consuming
repository, add a pinned `itself` dependency to the CI environment, and
choose one platform wrapper:

- [GitHub Actions](github-actions.yml)
- [GitLab CI](gitlab-ci.yml)

The checked-in wrappers use `ITSELF_INSTALL_SPEC: "."` to run against an
Itself source checkout. In a consuming repository, change that value to the
immutable Itself version or locked VCS revision used by the project.

Then replace two example inputs:

- `cookbook/fixtures/model-assertion.json` with the structured output produced
  by the coding or review agent;
- `python -m pytest` with the repository-owned test, reproduction, policy, or
  analysis command that is actually authoritative for the assertion.

Do not interpolate a model-supplied command into the workflow. If an agent
recommends a test, route that recommendation through a reviewed allowlist or
adapter before execution.

## Outcomes

The runner answers two separate questions. The model's expected exit code
decides whether its assertion is supported. The repository's passing exit code,
set with `--passing-exit-code` and `0` by default, decides whether the action
may proceed. A model that correctly predicts a failing check has made a true
assertion about a change that still fails.

| Observation | Claim state | Decision | Runner status |
| --- | --- | --- | ---: |
| Exit code matches the model's expectation and is the passing exit code | `supported` | `approved` | `0` |
| Exit code matches the model's expectation but is not the passing exit code | `supported` | `rejected` | `1` |
| Exit code differs from the model's expectation | `refuted` | `rejected` | `1` |
| The check cannot produce an exit code | `inconclusive` | `deferred` | `2` |

A check that times out, cannot start, or is ended by a signal has no exit code.
Invalid input or an inability to record the result also returns `2`, but does
not fabricate a deferred bundle. In every case, a nonzero status keeps the CI
gate closed.

The check runs without the GitHub Actions workflow-command files
(`GITHUB_ENV`, `GITHUB_OUTPUT`, `GITHUB_PATH`, `GITHUB_STATE`, and
`GITHUB_STEP_SUMMARY`) in its environment, and the GitHub wrapper fails the
step that runs the gate with the gate's own status rather than passing it to a
later step.

An approved Itself decision does not merge a change. It supplies a validated
decision record and successful job status that the code host's branch or merge
policy may use as one required condition.

## Run the gate from code the change cannot edit

The checked-in wrappers demonstrate the recipe; they do not protect it. Under
`pull_request`, the workflow file, `run.py`, the assertion fixture, the test
suite, and the `.` install of Itself all come from the change being gated, so
that change can rewrite any of them and approve itself. Removing the
workflow-command variables from the check's environment does not change this:
the checked code runs as the same runner user.

A real deployment must:

- run the gate from a protected ref, such as a required workflow or a reusable
  workflow pinned to the base branch, so the gate's workflow and `run.py` are
  not taken from the change;
- install a pinned `itself` version rather than the checked-out source; and
- take the assertion from the agent's output, not from a file the change can
  edit.

## What the example establishes

The recipe establishes that:

- the checked source revision, assertion, configured command, observation, and
  decision are explicit;
- the model cannot promote its own claim to `supported`;
- a rejected or unavailable check remains inspectable instead of disappearing
  behind an aborted job;
- another process can verify the resulting bundle and replay the claim state.

It does not establish that the command tests every relevant property, that the
runner was uncompromised, or that one successful check makes a change safe.
Those remain policy and system-design responsibilities.
