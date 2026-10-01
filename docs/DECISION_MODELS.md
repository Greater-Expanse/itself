# System One models

A System One model, also called a decision model, answers a closed question
about a state with a typed decision: a probability distribution over declared
options, and no generated text. Itself's decision adapter sends such questions
to an endpoint that implements the `/v1/systemone` request format, captures
each request and response, and returns validated distributions that an
application can record as predictions.

A decision model's answer is a model assertion like any other. It can decide
which external check runs first; it is never evidence, a verdict, or
authorization.

## Minimal configuration

```python
from pathlib import Path

from itself import (
    ChoiceQuestion,
    DecisionModelClient,
    DecisionModelEndpoint,
    DirectoryArtifactSink,
    NoulQuestion,
    ScoreQuestion,
)

endpoint = DecisionModelEndpoint(
    actor_id="support-router",
    base_url="http://127.0.0.1:8000/v1",
    model="local-decider",
    allow_insecure_http=True,
)
client = DecisionModelClient(
    endpoint=endpoint,
    artifact_sink=DirectoryArtifactSink(Path(".itself/decisions")),
)
result = client.decide(
    "Customer: my invoice was charged twice and nobody answers the phone!",
    {
        "department": ChoiceQuestion(
            "Which team should handle this?",
            {
                "billing": "Charges, invoices, refunds",
                "technical": "Bugs and outages",
            },
        ),
        "urgent": NoulQuestion("Is this urgent?"),
        "frustration": ScoreQuestion(
            "How frustrated is the customer?",
            ["Calm", "Frustrated", "Very angry"],
        ),
    },
)

department = result.estimates["department"]
print(department.most_likely, department.probabilities["billing"])
print(department.order_gap, department.order_flip)
```

`DecisionModelEndpoint` applies the same transport rules as the Chat
Completions endpoint. A credential requires HTTPS; unencrypted HTTP is limited
to credential-free loopback hosts and requires `allow_insecure_http=True`.
`EnvironmentCredential` reads the secret only while a request is rendered, and
it never appears in request bodies, artifacts, results, or the SDK's error
messages; the [inference guide](INFERENCE.md) describes what a custom
transport's exceptions and traceback-recording tools can still expose. Extra
headers and request fields cannot override adapter-owned values: the `model`,
`state`, and `questions` fields, the content type, the `Host`,
`Content-Length`, and `Transfer-Encoding` headers, and authentication. The
default resource path is `systemone`, so a base URL ending in `/v1` reaches
`/v1/systemone`. `timeout_seconds` bounds each read, and the optional
`deadline_seconds` bounds each request as a whole.

## Questions

| Question | Declares | Answer |
| --- | --- | --- |
| `ChoiceQuestion(instructions, criteria)` | 2 to 255 options, each an identifier key with an optional description | A distribution over the option keys |
| `NoulQuestion(instructions, true_description=None, false_description=None)` | A yes-or-no question | The probability that it is true, as `{"true": p, "false": 1 - p}` |
| `ScoreQuestion(instructions, levels)` | 2 to 255 ordered levels, lowest first | A distribution over the level indexes `"0"`, `"1"`, and so on |

The state is JSON text, an object, or an array. An object or array must be
I-JSON nested at most 127 levels deep, so the request that carries it stays
within the 128 levels a strict reader accepts: every key is a string, integers
stay within ±(2^53 − 1), and every number is finite, so the request means the
same thing to every JSON reader and its captured copy can be read back. The
keys of the question mapping identify each question in every request, answer,
and estimate, and they must be identifiers.

Questions are immutable values that compare, hash, and pickle. A choice
question's option order is part of its value, so two questions that list the
same options in different orders are different questions.

## Option order

Some decision models favor an option because of where it appears. By default,
`decide` therefore asks each question twice, once with its options as declared
and once reversed, and reports how far the answers moved.

- A choice question is sent with its options reversed. Its answers are keyed
  by option, so they need no mapping.
- A score question is sent with its levels reversed, and the adapter maps
  each answer back to the declared level indexes before combining them.
- A noul question's two answers have no order on the wire, so reversing it
  would change nothing a server sees. `decide` asks it once, in the first
  request, and does not send a later request that has nothing left to ask. To
  measure order effects on a yes-or-no judgment, ask it as a two-option
  `ChoiceQuestion`.

Counterbalancing a choice question relies on the endpoint reading `criteria`
in the order the request lists them. JSON leaves the order of an object's
members unspecified, so a server or gateway that parses them into an unordered
map shows both requests the same order, and the order gap then measures
nothing. Score levels travel as an array, whose order JSON keeps. When a score
answer's `legend` maps every level index to its text, the adapter requires it
to match the levels the request showed, which catches a server that keyed the
distribution by another order.

Each `DecisionModelEstimate` holds the mean distribution over the requests
that asked its question, `attempt_count`, `order_gap`, and `order_flip`.
`order_gap` is the largest difference between requests in the probability of
any option, and `order_flip` records whether the most likely option changed.
Both are `None` for a question asked once.

Pass `orders=(OptionOrder.DECLARED,)` to send one request per decision, or
call `ask` to send exactly one request in a chosen order. The client sends
requests one at a time and never retries one.

## Results

A `DecisionModelResult` contains:

- `identity`: the actor, adapter identity and version, requested model, and
  the model the endpoint reported;
- `attempts`: one `DecisionModelAttempt` per request, with its option order,
  the captured request and raw response as artifact references, token counts,
  wall time, and one validated `DecisionModelAnswer` per question in declared
  terms;
- `estimates`: one `DecisionModelEstimate` per question;
- `usage`: token counts and wall time summed over the attempts.

`to_json_object()` on a result, attempt, answer, or estimate returns a JSON
representation that can be stored and cited as a derived artifact. Canonical
JSON sorts object keys, so answers and estimates also carry `option_keys` in
declared order and the `most_likely` option, whose ties that order breaks.
Results and their parts are immutable and pickle, so a worker process can
return them.

The client captures the exact request body before sending it, and the exact
bounded response bytes before interpreting them, through the configured
`ArtifactSink`. A captured request contains the state and questions but no
credential, so keep it in a private sink whenever the state is private.

## Validation and failures

Each answer is checked against the question that was asked:

- the response answers exactly the asked questions, each with the matching
  type;
- every probability is a finite JSON number from 0 to 1, and each
  distribution covers exactly the declared options;
- each distribution sums to 1 within the endpoint's `probability_tolerance`
  (default `1e-6`, at most `0.01`);
- a choice answer's `choice` is its most probable option, within the
  tolerance;
- a score answer's `score` lies within the tolerance times the number of
  levels minus one of its distribution's expected level, because the expected
  level weights each probability by an index up to that number;
- a score answer's `legend`, when it maps every level index to text, matches
  the levels the request showed;
- a reported confidence, for any question type, lies between 0 and 1;
- a reported `model` is a string, and every request in one decision reports
  the same model.

Fields the adapter does not interpret, such as a legend of any other shape,
remain in the captured response. `reported_confidence` is the endpoint's own figure, and
implementations define it differently, so compare models by their
probabilities.

Failures raise `InferenceError` with the failure classes of the Chat
Completions client, and the captured response stays attached when one exists.
A failure after the request was captured raises `DecisionModelError`, an
`InferenceError` whose `request` references the captured request. When a later
request of a `decide` call fails, the error's `attempts` holds the requests
that completed, so every captured artifact of the decision stays reachable.
Only transport, rate-limit, and server failures are marked retryable. A server
that answers "busy" with a 5xx status is therefore retryable, and the client
still leaves any retry to the caller.

## Recording a decision in a ledger

Schema line `v0alpha2` has no probability field on predictions, so the
probabilities stay in artifacts that ledger records cite by digest:

```python
from datetime import datetime

from itself import (
    Actor,
    ActorRole,
    ActorType,
    Digest,
    DigestAlgorithm,
    RecordHeader,
    artifact_reference_record,
)

identity = result.identity
model = Actor(
    actor_id=identity.actor_id,
    actor_type=ActorType.MODEL,
    role=ActorRole.PROPOSER,
    implementation_ref=(
        f"{identity.adapter_id}@{identity.adapter_version}:"
        f"{identity.resolved_model or identity.requested_model}"
    ),
)
records = [
    artifact_reference_record(
        RecordHeader(
            f"decision-{index}-{role}",
            datetime.fromisoformat(reference.captured_at),
            model,
        ),
        uri=reference.uri,
        media_type=reference.media_type,
        title=f"Decision-model {role}, options {attempt.order.value}",
        digest=Digest(DigestAlgorithm.SHA256, reference.sha256),
        captured_at=datetime.fromisoformat(reference.captured_at),
    )
    for index, attempt in enumerate(result.attempts)
    for role, reference in (
        ("request", attempt.request),
        ("response", attempt.raw_response),
    )
]
```

A prediction record by the same model actor then states the observation that
the estimate's most likely option implies, and the external test, evidence,
verdict, and evidence-backed transition come from non-model actors, as the
[protocol-record guide](PROTOCOL_RECORDS.md) describes.

## Native adapters

A decision service with a different wire format can implement
`DecisionModelAdapter`:

```python
from collections.abc import Mapping, Sequence

from itself import (
    COUNTERBALANCED_ORDERS,
    DecisionModelResult,
    DecisionQuestion,
    JsonValue,
    OptionOrder,
)


class MyDecisionAdapter:
    def decide(
        self,
        state: JsonValue,
        questions: Mapping[str, DecisionQuestion],
        *,
        orders: Sequence[OptionOrder] = COUNTERBALANCED_ORDERS,
    ) -> DecisionModelResult:
        ...
```

It should keep the same guarantees: explicit configuration, capture of each
exact request and bounded response before interpretation, local validation of
every distribution, answers reported in declared terms whatever order a
request showed, sanitized failures, and no silent repair, retry, or fallback.

## Wire format

A request lists each question's options in the order the attempt shows them,
so the client serializes it without sorting keys:

```json
{
  "model": "local-decider",
  "state": "Customer: my invoice was charged twice and nobody answers the phone!",
  "questions": {
    "department": {
      "type": "choice",
      "instructions": "Which team should handle this?",
      "criteria": {
        "billing": "Charges, invoices, refunds",
        "technical": "Bugs and outages"
      }
    },
    "urgent": {"type": "noul", "instructions": "Is this urgent?"},
    "frustration": {
      "type": "score",
      "instructions": "How frustrated is the customer?",
      "criteria": ["Calm", "Frustrated", "Very angry"]
    }
  }
}
```

A response answers every question:

```json
{
  "model": "decider-2b",
  "answers": {
    "department": {
      "type": "choice",
      "choice": "billing",
      "probabilities": {"billing": 0.9, "technical": 0.1},
      "confidence": 0.8
    },
    "urgent": {"type": "noul", "noul": 0.7},
    "frustration": {
      "type": "score",
      "score": 1.5,
      "probabilities": {"0": 0.1, "1": 0.3, "2": 0.6},
      "confidence": 0.4
    }
  },
  "usage": {"input_tokens": 212, "output_tokens": 0}
}
```

## Assurance boundary

A validated distribution is still a model assertion. Applications should
record it as a prediction, run the declared checks through external tools or
reviewers, record the resulting evidence, and let only an authorized
non-model actor promote the claim or hypothesis. Itself enforces that last
rule for every model actor, whichever adapter produced the prediction.
