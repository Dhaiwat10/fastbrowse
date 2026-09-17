<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/wordmark-dark.svg">
  <img src="assets/wordmark.svg" alt="fastbrowse" width="360">
</picture>

**A browser agent that picks instead of generating.**

Jev chooses each action and target, an LLM plans, reads and writes the answer,
and code owns verification, safety and secrets.

![python](https://img.shields.io/badge/python-3.13-475569?style=flat-square)
![license](https://img.shields.io/badge/license-MIT-475569?style=flat-square)
![status](https://img.shields.io/badge/status-pre--alpha-6366F1?style=flat-square)
![cost](https://img.shields.io/badge/vs%20hosted-19%C3%97%20cheaper-6366F1?style=flat-square)

</div>

---

## Why

Most browser agents generate an action from a screenshot every step. That is expensive, and it is
free to invent a button that was never on the page. fastbrowse indexes the page into candidates and
asks a choice model to **pick one**: a target that was never indexed cannot be chosen, and an answer
is not allowed to exist unless a quote backing it is found verbatim in a stored capture of the page.

Measured against hosted Browser Use on the same six tasks, two passes each ([docs/evals.md](docs/evals.md)):

| | passed | cost | wall clock | cost per task |
|:--|:--|:--|:--|:--|
| **fastbrowse** on a cloud browser | 12/12 | **$0.2622** | 498s | **$0.0219** |
| hosted Browser Use | 11/12 | $5.0751 | 384s | $0.4229 |

About **19x cheaper and 30% slower**. The cost gap is structural: picking from indexed candidates
costs a fraction of generating actions from screenshots. The time gap is real and unattacked, since
every step is a round trip and nothing runs speculatively. Six tasks over two passes is a smoke test
rather than a benchmark, and it does not separate the two on correctness; the one failure above is
hosted's own `Task ended unexpectedly`. Read the score as "both usually finish these", and the cost
column as the finding.

## Try it

Needs Python 3.13, [uv](https://docs.astral.sh/uv/getting-started/installation/), and Chrome. Chrome
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
async with BrowserSession(connection, DirectorySink(path)) as session:
    page = CdpPage(session, Config())
    await page.navigate(url)
    result = await Agent(page, jev, llm, secrets=resolver).run(
        task, output_schema=MyModel, limits=Limits(max_dollars=0.10)
    )
```

`result.data` holds your `output_schema`, `result.evidence` the quotes behind it, and `result.cost`
itemizes Jev, LLM, browser and proxy spend, each marked metered, estimated or unknown.
`src/fastbrowse/cli.py` is the entire assembly in twenty lines.

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
