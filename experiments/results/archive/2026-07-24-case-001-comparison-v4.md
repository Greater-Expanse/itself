# Case 001 comparison study v4

Status: historical study version, retained because its limitations are part of the research record. It is not the current Case 001 result.

## What the experiment actually did

Imagine an engineer asks an AI assistant, "Why does this report keep coming back stale?" In this controlled incident, the source advanced from revision `A` to revision `B`, but six report-generation attempts still returned and verified stale revision `A`. The injected cause was a cache key that omitted the source revision. The model saw the failure history, system components, three plausible root causes, and the available diagnostic checks. It was not told which root cause was active.

The first model call mirrored ordinary AI-assistant use: pick the most likely root cause and explain why. A separate call asked for what an investigation team needs next: retain plausible causes, state what result each cause predicts for one selected check, and choose that check.

The harness then used the second answer in two ways. One path recorded the investigation plan without running its check. The other ran the selected check in a deterministic test environment, turned the observation into external evidence, and applied a rule outside the model to decide which cause could be marked supported or refuted. Itself created a ledger and reasoning receipt for the ordinary answer, the untested plan, and the evidence-tested plan.

This report calls those two model calls and three resulting paths one **run**.

## Outcome

We scheduled that two-call, three-path procedure 20 times—5 times for each of 4 model/provider configurations.

Of those planned runs, 14 completed both model calls and all three paths. The other 6 stopped on an explicit provider or model-output failure. Itself preserved what happened and did not retry, rewrite a response, or substitute a replacement run.

Itself retained a validated record for every scheduled run, including stopped runs, and can verify those records again later.

Study status: complete; the predefined scoring rules could be applied as written.

| Model | Complete runs | Why runs stopped | Ordinary answer chose known cause | Test plan chose known cause | External check identified known cause |
| --- | ---: | --- | ---: | ---: | ---: |
| Fireworks AI / GLM 5.2 | 5/5 | none | 5/5 | 5/5 | 5/5 |
| Together AI / MiniMax M3 | 1/5 | 4 test-plan requests: responses violated the requested task rules (`assertion_contract`) | 1/1 | 1/1 | 1/1 |
| Fireworks AI / MiniMax M3 | 4/5 | 1 test-plan request: response violated the requested task rules (`assertion_contract`) | 4/4 | 4/4 | 4/4 |
| Fireworks AI / Qwen 3.7 Plus | 4/5 | 1 ordinary-answer request: response did not match the required JSON shape (`response_schema`) | 4/4 | 4/4 | 4/4 |

## What this tells an SDK user

This experiment is evidence about the SDK's enforcement behavior, not proof of better model reasoning. Both the ordinary answer and the testable investigation plan already named the known cause in every complete run, so this result does not show that Itself made the model more accurate. It shows that a model answer can remain provisional until an external check supplies evidence; that application rules can prevent an unverified answer from being treated as confirmed; and that another developer can reconstruct how the system reached its final state from the ledger and reasoning receipt.

Across the 14 complete runs, the experiment recorded 42 ledger-backed paths: the ordinary answer, the testable investigation plan before its check, and that same plan after external evidence. Reasoning receipts recomputed from their ledgers for 42/42 paths. The incomplete runs show the other side of the boundary: a failed provider call or invalid model output remained an explicit integration result instead of being silently converted into valid-looking data.

## What happened

- The ordinary AI answer named the known cause in 14/14 complete runs.
- The testable investigation plan named the known cause in 14/14 complete runs and chose a check that could distinguish the retained alternatives in 14/14.
- The external check identified the known cause in 14/14 complete runs. The chosen check restored the expected behavior in 14/14.
- The application accepted 0 attempts to treat an unsupported model answer as confirmed.
- 6 runs stopped on provider or model-output failures. Those failures remain visible and are not counted as wrong diagnoses or replaced with new samples.
- The investigation plans declared 40 test-outcome predictions across complete runs; 36/40 matched the case's known outcome table.
- Final recorded states for the known cause after the external check: 14 supported.

## How the measurement was corrected

V4 binds `expected_observation` to the exact revision codes `A` and `B` in every manifest, output schema, and rendered payload. The deterministic scorer therefore compares like-for-like categorical values without a text parser or post-hoc reinterpretation.

V3 stopped after ten registered replicates when failed-attempt receipts were found not to bind the manifest's structured profile. The binding validator was corrected before v4 registration, and no v3 output was reused.

## Models and weight availability

| Target | Model identifier | Weight class |
| --- | --- | --- |
| fireworks-glm-5p2 | `accounts/fireworks/models/glm-5p2` | Public weights |
| together-minimax-m3 | `MiniMaxAI/MiniMax-M3` | Public weights |
| fireworks-minimax-m3 | `accounts/fireworks/models/minimax-m3` | Public weights |
| fireworks-qwen3p7-plus | `accounts/fireworks/models/qwen3p7-plus` | Hosted only |

## Data-quality checks

No reportable metadata anomalies were detected.

## What this does not show

This is one visible diagnosis problem with a fixed set of possible causes. It shows that the integration and enforcement path can be exercised with live model output. It does not establish general causal-reasoning ability, behavior independent of the execution environment, or population-level model rankings. It intentionally contains no composite truth, trust, or reasoning score.

The companion JSON report contains every validated shareable scorecard, bundle identifier, failure class, and raw-response digest used for these counts. It excludes prompts, model text, credentials, artifact URIs, and private response bytes.
