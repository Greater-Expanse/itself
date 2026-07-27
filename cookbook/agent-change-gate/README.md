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
repository, add a pinned `itself-sdk` dependency to the CI environment, and
choose one platform wrapper:

- [GitHub Actions](github-actions.yml)
- [GitLab CI](gitlab-ci.yml)

Then replace two example inputs:

- `cookbook/fixtures/model-assertion.json` with the structured output produced
  by the coding or review agent;
- `python -m pytest` with the repository-owned test, reproduction, policy, or
  analysis command that is actually authoritative for the assertion.

Do not interpolate a model-supplied command into the workflow. If an agent
recommends a test, route that recommendation through a reviewed allowlist or
adapter before execution.

## Outcomes

| Observation | Claim state | Decision | Runner status |
| --- | --- | --- | ---: |
| Exit code matches the model's expectation | `supported` | `approved` | `0` |
| Exit code differs | `refuted` | `rejected` | `1` |
| The check cannot produce an exit code | `inconclusive` | `deferred` | `2` |

Invalid input or an inability to record the result also returns `2`, but does
not fabricate a deferred bundle. In every case, a nonzero status keeps the CI
gate closed.

An approved Itself decision does not merge a change. It supplies a validated
decision record and successful job status that the code host's branch or merge
policy may use as one required condition.

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
