# Evals

Every grade comes from something the agent cannot write: a request the fixture server recorded, truth fetched from a site's own API, or the URL the browser actually ended on. An agent's claim of success never counts.

## Local fixtures

```sh
uv run python -m fastbrowse.evals.runner [--only TASK_ID ...] [--repeat N]
```

Six tasks against small sites in `src/fastbrowse/evals/fixtures/`, served locally and driven through headless Chrome. The fixture server records every POST, so form tasks are graded by what was submitted.

| Task | What it proves |
|---|---|
| `search-price` | search, open a result, read a value out of inline markup |
| `contact-form` | fill text, choose a select option, submit when authorized |
| `contact-needs-confirmation` | the same form without authorization stops at `NEEDS_CONFIRMATION` and submits nothing |
| `table-extract` | structured output copied from the right table cell |
| `login-wall` | a sign-in wall with no credentials stops at `NEEDS_LOGIN` and submits nothing |
| `confirm-dialog` | an irreversible delete behind a `confirm()` dialog |

Needs Jev and LLM keys (see `fastbrowse.clients.environment`). Costs about $0.005 a task.

## Live head-to-head

```sh
uv run --extra browser-use python -m fastbrowse.evals.live [--arms fast hosted] [--repeat N]
```

The same prompts run through fastbrowse on a Browser Use Cloud browser and through hosted Browser Use. Truth is fetched at run time from PyPI's JSON API, the Hacker News API and GitHub's REST API, so grades follow the live site. Cost is metered: provider-reported Jev/LLM cost plus the browser and proxy cost returned when the cloud browser stops, and `total_cost_usd` for the hosted session.

Both arms are graded on their answer. The fast arm is also graded on the URL it ended on. The hosted SDK exposes no final URL, so hosted navigation tasks rest on the answer alone.

Needs `BROWSER_USE_API_KEY` as well as the Jev and LLM keys. Rows are appended to `artifacts/evals/live.jsonl` with status, error and step trace.

## Results

Two passes of all six live tasks, 2026-09-18, `google/gemini-3.8-flash` at low reasoning effort behind Jev. The hosted row is from 2026-09-17 and was not rerun:

| | passed | time per task | cost per task |
|---|---|---|---|
| fastbrowse on a cloud browser | 11/12 | 26.7s | $0.0160 |
| hosted Browser Use | 11/12 | 27.4s | $0.4236 |

**The cost gap is structural.** Picking from indexed candidates spends a fraction of the tokens that generating actions from screenshots does, and most of what is left is the LLM rather than Jev or the browser.

**Where the time went, and what took it back.** On the first measured build a task averaged 44.2s: about two thirds LLM, a tenth Jev, the rest browser round trips. In order of effect:
- Low reasoning effort on every LLM call.
- An answer drafted from the reader's quoted facts, which Jev accepts or sends to the composer (it accepted 4 drafts in 6, cutting compose time from 21.4s to 4.9s across the suite).
- The plan running alongside the first steps instead of before them.
- Merged and concurrent browser calls (a fill is 7 CDP calls, down from 13).
- A 30s cap on a single LLM attempt, so a stuck request is retried rather than waited on.

The local fixtures, simpler sites on a local Chrome, averaged 9.8s a task on the same build.

The one fastbrowse miss answered correctly but could not quote one of its claims, so it reported `unverified`; the grader counts only `complete`.

**Twelve runs is a smoke test, and noise is several seconds a task:** two runs of an identical build came out 36.3s and 40.7s. Read the score as "both arms usually finish these tasks" and the cost column as the real finding.

**Model choice was measured, and the fast answer lost.** `evals.latency` times candidate models on the two request shapes a run is made of, and `google/gemini-3.5-flash-lite` was fastest on both (plan 1.6s against 5.9s). On the local fixtures that was free, but live it scored 8/12, so the default stays on `gemini-3.8-flash` everywhere except `FIELD_TEXT`, which only produces the string to type.

Both suites depend on upstream availability: one pass scored 0/6 during a Jev `model_unavailable` outage. Re-read a red run before believing it is a regression.

## External benchmarks

We don't vendor other people's benchmarks. To compare on them:

- [BrowserGym](https://github.com/ServiceNow/BrowserGym): a gym interface over MiniWoB, WebArena, WorkArena and others
- [WebArena](https://github.com/web-arena-x/webarena) and [WebArena-Verified](https://github.com/ServiceNow/webarena-verified): self-hosted sites with functional graders
- [Mind2Web](https://github.com/OSU-NLP-Group/Mind2Web) and [Online-Mind2Web](https://github.com/OSU-NLP-Group/Online-Mind2Web): real-site tasks, offline and live
- [WebVoyager](https://github.com/MinorJerry/WebVoyager): live tasks on 15 popular sites
- [WebBench](https://github.com/Halluminate/WebBench): large live read/write task set
- [AssistantBench](https://github.com/oriyor/assistantbench): time-consuming information-seeking tasks
