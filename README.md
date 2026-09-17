<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/wordmark-dark.svg">
  <img src="assets/wordmark.svg" alt="fastbrowse" width="360">
</picture>

**A browser agent that picks instead of generating.**

Jev chooses each action and target, an LLM plans, reads and writes the answer,
and code owns verification, safety and secrets.

![python](https://img.shields.io/badge/python-3.14-475569?style=flat-square)
![license](https://img.shields.io/badge/license-MIT-475569?style=flat-square)
![status](https://img.shields.io/badge/status-pre--alpha-6366F1?style=flat-square)
![cost](https://img.shields.io/badge/vs%20hosted-22%C3%97%20cheaper-6366F1?style=flat-square)

</div>

---

## Why

Most browser agents generate an action from a screenshot every step. That is expensive, and it is
free to invent a button that was never on the page. fastbrowse indexes the page into candidates and
asks a choice model to **pick one**: a target that was never indexed cannot be chosen, and an answer
is not allowed to exist unless a quote backing it is found verbatim in a stored capture of the page.

Measured against hosted Browser Use on the same six tasks, two passes each ([docs/evals.md](docs/evals.md)):

| | passed | wall clock | cost per task |
|:--|:--|:--|:--|
| **fastbrowse** on a cloud browser | 11/12 | 38.5s | **$0.0196** |
| hosted Browser Use | 11/12 | 27.4s | $0.4236 |

About **22x cheaper and 40% slower**. The cost gap is structural: picking from indexed candidates
costs a fraction of generating actions from screenshots. The time gap is mostly the browser, not the
thinking. These numbers are a cloud browser over the network, where every round trip costs about
0.42s; the same agent against the local fixtures finishes a task in 1.6 to 12.6 seconds. The rest of
the gap is work hosted does not do at all, since reading the page and verifying the answer against it
are what make the answer checkable.

Our row is the mean of two twelve-task runs that came out 4.4s a task apart, which is the honest
precision available here: a live suite this size moves by seconds between runs, so read any
difference smaller than that as noise rather than as a result.

Six tasks over two passes is a smoke test rather than a benchmark, and it does not separate the two
on correctness: each arm failed exactly one run, and neither failure was a wrong answer. Ours is
PyPI presenting a sign-in wall, reported as `needs_login` rather than guessed at; hosted's is its own
`Task ended unexpectedly`. Read the score as "both usually finish these", and the cost column as the
finding.

## How it compares

Three browser agents doing three different jobs. This table is about capability. The only measured
head-to-head here is the one above, against hosted Browser Use on the same tasks and the same day.

| | hosted Browser Use | [jev-ultrafast](https://github.com/browser-use/jev-ultrafast) | fastbrowse |
|:--|:--|:--|:--|
| How an action is chosen | an LLM generates one from a screenshot | Jev picks from an indexed element table | Jev picks from indexed candidates |
| What a run returns | an answer | a stopping state, `DONE` or `BLOCKED` | an answer, or a named reason it stopped |
| Reading a page | yes | no read step at all | claims located verbatim in a stored capture |
| Signing in | yes | password fields are excluded from the action space | `--secret`, resolved locally, models see names only |
| Irreversible actions | not gated in the API we called | not gated | stops at `needs_confirmation` unless authorized |
| Browser | cloud | local Chrome, your profile | local Chrome or cloud |

jev-ultrafast is worth reading. Browser Use published it with a measured 7.1 second Google Flights
run, and it is the fastest of the three at the job it does. That job is navigation: it has no read
step, so it cannot answer a question about a page, and `DONE` only means the model believes the goal
is visible, which their own documentation is careful not to call evidence. fastbrowse is attempting
the harder thing, which is coming back with an answer you can check.

Two of their techniques matter, and fastbrowse already had both: the page is read in a single
browser call rather than by walking the accessibility tree, and the operation and its target ride in
one request as speculative heads, so a step costs one round trip rather than two. Where they are
genuinely ahead is the browser, running local Chrome against an existing profile while the numbers
above are measured against a cloud browser over the network.

## Try it

Needs Python 3.14, [uv](https://docs.astral.sh/uv/getting-started/installation/), and Chrome. Chrome
is only for local runs: `--cloud` needs neither Chrome nor a display.

```sh
git clone git@github.com:agent-labs-dev/fastbrowse.git && cd fastbrowse
uv sync
```

Two keys are required. A third unlocks the cloud browser.

| Variable | Pays for | From |
|:--|:--|:--|
| `AI_GATEWAY_API_KEY` or `TYPESAFE_API_KEY` | Jev, the choice model that picks every action | Vercel AI Gateway, or TypeSafe directly |
| `OPENROUTER_API_KEY` | the LLM that plans, reads and writes the answer | [openrouter.ai/keys](https://openrouter.ai/keys) |
| `BROWSER_USE_API_KEY` | the cloud browser behind `--cloud` (optional) | [cloud.browser-use.com](https://cloud.browser-use.com) |

```sh
export AI_GATEWAY_API_KEY=...
export OPENROUTER_API_KEY=...

uv run fastbrowse "What is the title of the top story right now?" --start https://news.ycombinator.com/
```

Steps stream to stderr, and the answer lands on stdout:

```
   0 read  -> executed
complete ($0.0238, 1 steps)
The top story on Hacker News is titled "Hister: A private search engine for the pages you visit and the files you keep".
```

That run cost two cents. Add `--json` for the whole result: every step, the evidence behind the
answer, and cost split by component.

> [!TIP]
> Plenty of large sites challenge a local headless browser before showing anything. PyPI answers one
> with a **Client Challenge** page, and the run stops there rather than pretending. Use `--cloud` for
> those.

### Flags

| Flag | Effect |
|:--|:--|
| `--start URL` | **required**, the page to open before the task begins |
| `--cloud` | run on a Browser Use Cloud browser instead of local Chrome: slower to start, far less likely to be challenged, and the only option without Chrome |
| `--authorize` | allow submit, pay, delete and send. Without it the run stops at `needs_confirmation` **before** the irreversible action, having changed nothing |
| `--secret NAME=ENV_VAR` | let the agent type the value of `$ENV_VAR`, only on the start origin. Models see the name, never the value |
| `--max-steps N`, `--max-dollars N` | bound the run. Exceeding either ends at `budget_exceeded` |
| `--downloads DIR` | keep files the run downloads, which otherwise go to a temp directory |
| `--json` | print the full result instead of just the answer |

A run that signs in, where the password never reaches a model:

```sh
export SAUCE_PASSWORD=secret_sauce
uv run fastbrowse "Log in as standard_user with the saved password and add the backpack to the cart." \
  --start https://www.saucedemo.com/ --secret password=SAUCE_PASSWORD --authorize
```

### Choosing models

Jev picks the actions; an LLM plans, reads, writes the answer and recovers. The LLM defaults to
`google/gemini-3.8-flash` for every purpose but one, and `google/gemini-3.5-flash-lite` for
`FIELD_TEXT`, which only turns "the password" or "Zurich" into the string to type and so mints
nothing a conclusion rests on.

| Variable | Effect |
|:--|:--|
| `FASTBROWSE_LLM_MODEL` | one model for every purpose |
| `FASTBROWSE_LLM_MODEL_<PURPOSE>` | one purpose only: `PLAN`, `READ`, `FIELD_TEXT`, `RECOVER`, `COMPOSE`, `VERIFY` |

The fast trade is available and is not the default, because it was measured and it costs
correctness. `FASTBROWSE_LLM_MODEL=google/gemini-3.5-flash-lite` is the quickest configuration on
the request shapes a run is made of, and the cheapest, and on the live suite it scores 8/12 instead
of 12/12. A run that answers nothing is not a fast run. Take it when a wrong stop is cheap for you.

### Reading the result

`complete` is the only success, and it is earned: every requirement the planner derived from your
task must be backed by a quote located verbatim in a stored capture. Everything else names why the
run stopped instead of guessing.

| Status | Meaning |
|:--|:--|
| `complete` | every requirement evidenced |
| `unverified` | it believes it finished but could not evidence a requirement, so read the answer with suspicion |
| `needs_confirmation` | paused before an irreversible action you did not authorize; re-run with `--authorize` |
| `needs_login` | a sign-in or verification wall blocks the task and no `--secret` covers it |
| `needs_input` | a field needs a value you did not supply, and inventing one is not allowed |
| `stuck` | recovery was exhausted without the page moving |
| `budget_exceeded` | a step, call, time or dollar limit was reached |

The exit code is 0 only for `complete`.

## Evals

```sh
uv run python -m fastbrowse.evals.runner                     # six local fixtures, about $0.005 each
uv run --extra browser-use python -m fastbrowse.evals.live   # live head-to-head; --arms fast skips the hosted side
```

Every grade comes from something the agent cannot write: a request the fixture server recorded,
truth fetched from a site's own API, or the URL the browser actually ended on. A claim of success
never counts. [docs/evals.md](docs/evals.md) has what each task proves.

## Embed it

```python
from fastbrowse import run_task

result = await run_task(
    "Find the cheapest kettle and tell me its price.",
    start="https://example.com/",
    browser_api_key=BROWSER_USE_KEY,  # omit for local headless Chrome
    output_schema=Kettle,
    limits=Limits(max_dollars=0.10),
)
```

One call assembles the browser, the Jev and LLM clients, and the agent, then closes the browser on
every path out with the cloud session's own cost folded into the result. Pass `jev=` and `llm=` to
supply clients built from credentials you resolved yourself, rather than from the environment.

`result.status` is the table above, `result.data` holds your `output_schema`, `result.evidence` the
quotes behind the answer, `result.final_url` where the browser ended, and `result.cost` itemizes
Jev, LLM, browser and proxy spend, each marked metered, estimated or unknown.

## Troubleshooting

| Symptom | Cause |
|:--|:--|
| `set TYPESAFE_API_KEY or AI_GATEWAY_API_KEY for Jev` | the key is missing from the environment you ran in, which is easy under `sudo` or in a fresh shell |
| a local run sees a different page than you do | headless Chrome opens at 1280x900 with no profile, so you get the logged-out, no-extensions view |
| repeated challenges or an instant block | the site refuses datacentre or headless browsers, and `--cloud` usually clears it |
| `stuck` on a page you know has the control | run `--json` and read the steps. Each names the control chosen, the URL, Jev's confidence and a note. Scrolling and re-reading without ever choosing it means the control was never indexed: covered, in a frame that had not loaded, or absent at that viewport |

## Contributing

`uv run ruff format . && uv run ruff check . && uv run pyright && uv run pytest` is what CI runs,
plus `uv run python scripts/no_slop.py`, which fails the build on em dashes, curly quotes and filler
phrasing. Annotate a deliberate one with `slop-ok: <reason>`.

## Read more

- [docs/design.md](docs/design.md), who owns which decision, and the browser capabilities verified over plain CDP
- [docs/evals.md](docs/evals.md), the suites, the measured results, and links to external benchmarks

<div align="center"><sub>MIT, pre-alpha, private</sub></div>
