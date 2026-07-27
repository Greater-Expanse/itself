# Case 001 comparison study v2

Status: historical study version, retained because its limitations are part of the research record. It is not the current Case 001 result.

## What the experiment actually did

Imagine an engineer asks an AI assistant, "Why does this report keep coming back stale?" In this controlled incident, the source advanced from revision `A` to revision `B`, but six report-generation attempts still returned and verified stale revision `A`. The injected cause was a cache key that omitted the source revision. The model saw the failure history, system components, three plausible root causes, and the available diagnostic checks. It was not told which root cause was active.

The first model call mirrored ordinary AI-assistant use: pick the most likely root cause and explain why. A separate call asked for what an investigation team needs next: retain plausible causes, state what result each cause predicts for one selected check, and choose that check.

The harness then used the second answer in two ways. One path recorded the investigation plan without running its check. The other ran the selected check in a deterministic test environment, turned the observation into external evidence, and applied a rule outside the model to decide which cause could be marked supported or refuted. Itself created a ledger and reasoning receipt for the ordinary answer, the untested plan, and the evidence-tested plan.

This report calls those two model calls and three resulting paths one **run**.

## Outcome

We scheduled that two-call, three-path procedure 25 times—5 times for each of 5 model/provider configurations.

Of those planned runs, 17 completed both model calls and all three paths. The other 8 stopped on an explicit provider or model-output failure. Itself preserved what happened and did not retry, rewrite a response, or substitute a replacement run.

Itself retained a validated record for every scheduled run, including stopped runs, and can verify those records again later.

Study status: complete as a test of the experiment machinery; some behavior scores cannot be interpreted because model responses and the scorer represented predicted outcomes differently.

| Model | Complete runs | Why runs stopped | Ordinary answer chose known cause | Test plan chose known cause | External check identified known cause |
| --- | ---: | --- | ---: | ---: | ---: |
| Together AI / GLM 5.2 | 0/5 | 4 ordinary-answer requests: endpoint rejected the requests because its rate limit was reached (`http_rate_limit`); 1 test-plan request: response violated the requested task rules (`assertion_contract`) | 0/0 | 0/0 | 0/0 |
| Fireworks AI / GLM 5.2 | 5/5 | none | 5/5 | 5/5 | 4/5 |
| Together AI / MiniMax M3 | 3/5 | 2 test-plan requests: responses violated the requested task rules (`assertion_contract`) | 3/3 | 3/3 | 3/3 |
| Fireworks AI / MiniMax M3 | 5/5 | none | 5/5 | 5/5 | 5/5 |
| Fireworks AI / Qwen 3.7 Plus | 4/5 | 1 test-plan request: response violated the requested task rules (`assertion_contract`) | 4/4 | 4/4 | 4/4 |

## What this tells an SDK user

This experiment is evidence about the SDK's enforcement behavior, not proof of better model reasoning. Both the ordinary answer and the testable investigation plan already named the known cause in every complete run, so this result does not show that Itself made the model more accurate. It shows that a model answer can remain provisional until an external check supplies evidence; that application rules can prevent an unverified answer from being treated as confirmed; and that another developer can reconstruct how the system reached its final state from the ledger and reasoning receipt.

Across the 17 complete runs, the experiment recorded 51 ledger-backed paths: the ordinary answer, the testable investigation plan before its check, and that same plan after external evidence. Reasoning receipts recomputed from their ledgers for 51/51 paths. The incomplete runs show the other side of the boundary: a failed provider call or invalid model output remained an explicit integration result instead of being silently converted into valid-looking data.

## What happened

- The ordinary AI answer named the known cause in 17/17 complete runs.
- The testable investigation plan named the known cause in 17/17 complete runs and chose a check that could distinguish the retained alternatives in 16/17.
- The external check identified the known cause in 16/17 complete runs. The chosen check restored the expected behavior in 17/17.
- The application accepted 0 attempts to treat an unsupported model answer as confirmed.
- 8 runs stopped on provider or model-output failures. Those failures remain visible and are not counted as wrong diagnoses or replaced with new samples.

## Why some measurements cannot be used

V2 cannot support a prediction-correctness or final-verdict comparison. Its assertion contract allowed a free-text `expected_observation`, while the deterministic scorer expected the exact revision code `A` or `B`. Conforming model sentences therefore compared unequal to their intended code. That defect also propagated into evidence relations and final epistemic states.

The report retains the original scorecards for audit but marks `predictions_correct` and `ground_truth_final_status` invalid. No post-hoc text parser or manual reinterpretation was introduced. A later study requires a preregistered, schema-constrained outcome vocabulary.

## Models and weight availability

| Target | Model identifier | Weight class |
| --- | --- | --- |
| together-glm-5p2 | `zai-org/GLM-5.2` | Public weights |
| fireworks-glm-5p2 | `accounts/fireworks/models/glm-5p2` | Public weights |
| together-minimax-m3 | `MiniMaxAI/MiniMax-M3` | Public weights |
| fireworks-minimax-m3 | `accounts/fireworks/models/minimax-m3` | Public weights |
| fireworks-qwen3p7-plus | `accounts/fireworks/models/qwen3p7-plus` | Hosted only |

## Data-quality checks

1 timestamp anomaly is retained in the companion JSON report. The raw-response digests and explicit failure records for affected runs remain intact; no run was replaced.

## What this does not show

This is one visible diagnosis problem with a fixed set of possible causes. It shows that the integration and enforcement path can be exercised with live model output. It does not establish general causal-reasoning ability, behavior independent of the execution environment, or population-level model rankings. It intentionally contains no composite truth, trust, or reasoning score.

The companion JSON report contains every validated shareable scorecard, bundle identifier, failure class, and raw-response digest used for these counts. It excludes prompts, model text, credentials, artifact URIs, and private response bytes.
