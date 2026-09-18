<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/wordmark-dark.svg">
  <img src="assets/wordmark.svg" alt="fastbrowse" width="360">
</picture>

**A browser agent that picks instead of generating.**

Jev chooses each action, an LLM plans and reads, and code owns verification, safety and secrets.

![python](https://img.shields.io/badge/python-3.14-475569?style=flat-square)
![license](https://img.shields.io/badge/license-MIT-475569?style=flat-square)
![status](https://img.shields.io/badge/status-pre--alpha-6366F1?style=flat-square)

</div>

---

## Why

Most browser agents generate each action from a screenshot. fastbrowse indexes the page into
candidates and has [Jev](https://typesafe.ai), a choice model, **pick one**, so it cannot click
something that was never on the page. An answer only counts if every fact in it is quoted verbatim
from a stored capture of the page.

Same six live tasks, two passes each, against hosted Browser Use ([method](docs/evals.md)):

| | passed | correct answer | median time | mean time | cost per task |
|:--|:--|:--|:--|:--|:--|
| **fastbrowse** (cloud browser) | 11/12 | 12/12 | **18.1s** | 27.6s | **$0.0152** |
| fastbrowse, previous build | 11/12 | | 27.5s | 26.7s | $0.0160 |
| hosted Browser Use | 11/12 | not graded apart | 16.9s | 27.4s | $0.4236 |

**Speed.** This build cut the median task by a third, 27.5s to 18.1s, and every lookup on the second
pass finished in 11 to 19s. Four changes did it: a direct address for the task proposed while the
start page loads, settling on DOM quiet instead of every image and tracker, short facts picked by
Jev instead of read by the LLM, and one fewer browser round trip per click. The mean has not moved
yet because it is carried by provider stalls (one 26s plan call, 34s of Jev in one run); cutting
those tails is next ([details](docs/evals.md#results)).

Twelve runs is a smoke test, not a benchmark: read it as "both finish these, at about the same
speed, and fastbrowse costs about a twenty-eighth". `passed` counts only `complete`; our miss had
the right answer but could not confirm the cart on the page, so it reported `unverified`. The
hosted row was measured once, on 2026-09-17.

## How it compares

| | hosted Browser Use | [jev-ultrafast](https://github.com/browser-use/jev-ultrafast) | fastbrowse |
|:--|:--|:--|:--|
| Choosing an action | LLM generates from a screenshot | Jev picks from indexed controls | Jev picks from indexed controls |
| Returns | an answer | `DONE` or `BLOCKED` | an answer with quotes, or why it stopped |
| Reads pages | yes | no | yes, every claim cited |
| Signing in | yes | password fields excluded | `--secret`, models see names only |
| Irreversible actions | not gated | not gated | stop unless `--authorize` |
| Browser | cloud | local Chrome, your profile | local Chrome or cloud |

jev-ultrafast is Browser Use's navigation agent, with a measured 7.1 second Google Flights run, and
fastbrowse shares its core techniques (one browser call per page read, operation and target chosen
in one Jev request). The column describes its `main` branch as of 2026-09-18; an experimental
branch adds a planner with requirement checks.

## Try it

Needs Python 3.14, [uv](https://docs.astral.sh/uv/), and Chrome (not needed with `--cloud`).

```sh
git clone https://github.com/agent-labs-dev/fastbrowse.git && cd fastbrowse
uv sync
cp .env.example .env            # then add a Jev key (AI_GATEWAY_API_KEY) and OPENROUTER_API_KEY
uv run fastbrowse "What is the title of the top story right now?" --start https://news.ycombinator.com/
```

```
   0 read  -> executed
complete ($0.0238, 1 steps)
The top story on Hacker News is titled "...".
```

Steps go to stderr and the answer to stdout. `--json` prints every step, the quotes behind the
answer, and cost by component.

| Flag | Effect |
|:--|:--|
| `--start URL` | required: the page to open first |
| `--cloud` | use a [Browser Use Cloud](https://cloud.browser-use.com) browser (`BROWSER_USE_API_KEY`); far less likely to be bot-challenged |
| `--authorize` | allow submit, pay, delete and send; without it the run stops at `needs_confirmation` first |
| `--secret NAME=ENV_VAR` | let the agent type `$ENV_VAR` on the start origin; models only see `NAME` |
| `--max-steps N`, `--max-dollars N` | bound the run |
| `--downloads DIR` | keep downloaded files |
| `--json` | full result instead of the answer |

```sh
export SAUCE_PASSWORD=secret_sauce
uv run fastbrowse "Log in as standard_user with the saved password and add the backpack to the cart." \
  --start https://www.saucedemo.com/ --secret password=SAUCE_PASSWORD --authorize
```

### Models

The LLM defaults to `google/gemini-3.8-flash` at low reasoning effort, with
`google/gemini-3.5-flash-lite` for planning, proposing a direct address and typing field text. Override with `FASTBROWSE_LLM_MODEL` (every
purpose), `FASTBROWSE_LLM_MODEL_<PURPOSE>` (`PLAN`, `READ`, `FIELD_TEXT`, `SHORTCUT`, `RECOVER`, `COMPOSE`,
`VERIFY`) and `FASTBROWSE_LLM_REASONING` (`low`, `medium`, `high`). Flash-lite everywhere is faster
but scored 8/12 live, so it is not the default.

### Results

The exit code is 0 only for `complete`.

| Status | Meaning |
|:--|:--|
| `complete` | every information requirement is backed by a quote, and every action is confirmed on the page |
| `unverified` | it believes it finished but could not back every claim |
| `needs_confirmation` | stopped before an irreversible action; re-run with `--authorize` |
| `needs_login` | a sign-in wall that no `--secret` covers |
| `needs_input` | a field needs a value you did not give, which is never invented |
| `stuck` | recovery ran out without the page moving |
| `budget_exceeded` | a step, call, time or dollar limit was reached |
| `observation_limit` | the page has more controls than Jev can take in |
| `error` | a model or browser failure |

## Embed it

```python
from fastbrowse import run_task
from fastbrowse.models import Limits

result = await run_task(
    "Find the cheapest kettle and tell me its price.",
    start="https://example.com/",
    output_schema=Kettle,  # any pydantic model
    limits=Limits(max_dollars=0.10),
)
```

`run_task` builds the browser and clients, runs the agent, and closes the browser on every path.
The result has `status`, `answer`, `data`, `evidence`, `final_url` and an itemized `cost`.

## Safety model

- **Irreversible actions.** Before any button or submit, Jev is asked whether it commits something
  that cannot be undone. Without `--authorize`, a yes stops the run. This is a classifier, not a
  guarantee: a page can word a harmful control to look harmless.
- **Secrets.** Models see secret names only. A value is resolved at the moment of typing, only for
  its declared origin, and is redacted from everything the run returns, in raw, URL-encoded and
  JSON-escaped form. A password field is typed only from a stored secret, never
  generated.
- **Page content is data.** Every prompt says so, and completion is judged against quotes and page
  state rather than the model's say-so.

## Evals and development

```sh
uv run python -m fastbrowse.evals.runner                     # local fixtures, about $0.005 a task
uv run --extra browser-use python -m fastbrowse.evals.live   # live head-to-head; --arms fast skips hosted
uv run ruff format . && uv run ruff check . && uv run pyright && uv run pytest && uv run python scripts/no_slop.py
```

Grades come only from things the agent cannot write: requests the fixture server recorded, truth
from a site's own API, or the URL the browser ended on. See [docs/evals.md](docs/evals.md),
[docs/design.md](docs/design.md), and [docs/jev.md](docs/jev.md) for every Jev assumption checked against
Typesafe's documentation.

## License

MIT, copyright Agent Labs. Adapted third-party code is credited in
[NOTICE](NOTICE).
