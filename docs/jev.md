# Jev: what Typesafe documents, and what fastbrowse assumes

Every Jev assumption in fastbrowse, checked against Typesafe's own documentation (September 2026, Jev
1.13). Anything the documentation does not state is marked **ours**: a fastbrowse choice, tuned on the
evals rather than taken from Typesafe.

## The contract

| | Documented | Where fastbrowse relies on it |
|:--|:--|:--|
| Direct API | `POST https://api.typesafe.ai/v1/systemone`, bearer key, body `{model, state, questions}`; response `{model, answers, usage}` ([API](https://docs.typesafe.ai/api), [OpenAPI](https://api.typesafe.ai/openapi.json)) | `clients/typesafe.py`, used when `TYPESAFE_API_KEY` is set |
| Gateway | Model `typesafe-ai/jev` via `https://ai-gateway.vercel.sh/v4/ai/evaluation-model`, body `{state, questions}` ([Gateway](https://vercel.com/docs/ai-gateway/modalities/evaluation), [transport source](https://github.com/vercel/ai/blob/main/packages/gateway/src/gateway-evaluation-model.ts)) | `clients/vercel.py`, used with `AI_GATEWAY_API_KEY` |
| Yes/no (Noul) | Direct returns `{type: "noul", noul: P(yes)}`; the gateway returns `probability`. Optional `true`/`false` criteria define the boundary ([Noul](https://docs.typesafe.ai/primitives/noul), [v1 migration](https://docs.typesafe.ai/migrating-to-v1)) | `clients/validation.py` decodes each shape separately |
| Choice | The top `choice`, every option's probability, and a `confidence` ([Choice](https://docs.typesafe.ai/primitives/choice)) | `policy.py` (operation and target), `retrieval.py` (field and short-fact reads) |
| Score | Ordered levels, returning a probability-weighted index ([Score](https://docs.typesafe.ai/primitives/score)) | Modelled in `jev.py`, not yet called |
| Options | At most 255 per Choice ([Choice](https://docs.typesafe.ai/primitives/choice)) | `MAX_CHOICE_OPTIONS = 255`; the 240 cap and group-then-element selection are **ours** |
| Tokens | 32k for state plus the largest question, 64k in total ([Models](https://docs.typesafe.ai/models)) | `TokenBudget` targets 24k and 48k; the headroom and `chars_per_token = 3.0` are **ours**, because tokens are estimated locally |
| Rate limits | 1,200 requests a minute and 250k tokens a second on the direct API, subject to change ([Models](https://docs.typesafe.ai/models)); no extra gateway limit on paid tiers ([Gateway limits](https://vercel.com/docs/ai-gateway/rate-limits)) | A run makes a few requests a step, far below either |
| Errors | 400/401/403/404/422/429/5xx, with 529 for overload; retry with backoff, honouring server retry headers ([Exceptions](https://docs.typesafe.ai/sdk/python/api/exceptions)) | `post_with_retry` retries these and honours `retry-after-ms` and `retry-after`, capped at 10s |
| Price | $0.042 per million input tokens; output is free ([Models](https://docs.typesafe.ai/models), [gateway catalog](https://ai-gateway.vercel.sh/v1/models)) | `CostComponent.JEV` on the ledger |
| Latency | 70 to 500ms end to end, as advertised ([launch post](https://typesafe.ai/blog/introducing-system-one-models-and-jev)); no SLA is published | Measured through the gateway: 0.28s median and 0.62s worst over 25 policy-sized calls. The 3s hedge in `clients/validation.py` is **ours** |
| Streaming | Gateway evaluation does not stream ([AI SDK evaluation](https://ai-sdk.dev/docs/ai-sdk-core/evaluation)) | Not needed: answers are a few numbers |

## Confidence is not correctness

Typesafe says Jev's probabilities are calibrated: across many answers, frequencies match the stated
probability ([primer](https://docs.typesafe.ai/introduction/machine-learning-primer)). A Choice's `confidence`
summarises how concentrated the distribution is; it is not the chosen option's probability
([Confidence](https://docs.typesafe.ai/confidence)). Noul has no separate confidence: its probability is
the answer.

Every threshold in `config.py` (`recover_below` 0.55, `done_accept_from` 0.85, `claim_problem_above` 0.70,
the 0.90 read cut in `retrieval.py`) is **ours**. None is a Typesafe recommendation. Each is set per
question on the evals, and the gates behind them (VERIFY, the claim check, the LLM fallback) mean a
miscalibrated threshold costs seconds, not a wrong answer.

## How the questions are written

Typesafe's guidance ([Primitives](https://docs.typesafe.ai/primitives), [Noul](https://docs.typesafe.ai/primitives/noul),
[Choice](https://docs.typesafe.ai/primitives/choice), [Jev 1.13 jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13)),
and where fastbrowse follows it:

- **Narrow, explicit judgments, with true/false descriptions that match the instruction.** Every Noul
  question in `verification.py` and `safety.py` carries both descriptions.
- **Concrete option boundaries, plus an escape option when coverage is incomplete.** The short-fact read
  offers "none of these", and the policy offers `escalate`.
- **Question ids are invisible to the model.** Everything the model needs is in the instruction text.
- **Batch independent questions that share one state; answers cannot see each other.** Each step is one
  request: operation, target, login and irreversibility together. The done check asks completion, unmet
  actions and "does the draft need rewriting" in one call.
- **Remove irrelevant state and keep arithmetic in code.** State is the redacted viewport and the notes;
  counts and comparisons go to the LLM reader.

## Not yet used

- **Structured instructions and criteria** ([Structure](https://docs.typesafe.ai/primitives/advanced)): `jev.py`
  types them as strings.
- **Score** for graded judgments such as relevance.
- **Full Choice distributions** for trying a second-best target, rather than only the top pick
  ([hierarchical classification](https://docs.typesafe.ai/cookbooks/hierarchical_classification)).

No faster Jev tier, cross-request cache or logprob access is documented.
