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

Two passes of all six live tasks through both arms, 2026-09-17, `google/gemini-3.8-flash` behind Jev:

| | passed | cost | wall clock | cost per task |
|---|---|---|---|---|
| fastbrowse on a cloud browser | 11/12 | $0.2354 | 462s | $0.0196 |
| hosted Browser Use | 11/12 | $5.0828 | 329s | $0.4236 |

**About 22x cheaper and about 40% slower.** The cost gap is structural: picking from indexed candidates spends a fraction of the tokens that generating actions from screenshots does, and most of what is left is the LLM rather than Jev (on a representative run, $0.0127 LLM against $0.0024 Jev and $0.0003 browser).

**Where the time goes**, measured on one 49.1s run before the latency work: 64% LLM, 7% Jev, and the rest browser round trips at about 0.42s each. The round trips were the tractable half and have been attacked: action preparation, settling, frame reads and session setup were merged or made concurrent, and a fill now costs 7 CDP calls instead of 13. The same suite measured 44.2s a task before that work and 38.5s after, at the same cost. Those are single and paired runs of a twelve-task suite, and two runs of the identical build came out 36.3s and 40.7s, so the gain is real but its precision is not: treat it as several seconds a task, not as a figure to two significant digits. What is left is mostly the LLM, and about 7s of every cloud run is the network under the browser: the local fixture suite finishes a task in 1.6 to 12.6 seconds.

**Six tasks is a smoke test, not a benchmark, and a single pass does not separate these arms on correctness.** Across the passes taken while developing this suite, fastbrowse scored 10-12/12 and hosted 11-12/12. Each arm failed exactly one run above, and neither failure was a wrong answer: ours is PyPI presenting a sign-in wall, reported as `needs_login` rather than answered with a guess, and hosted's is its own `Task ended unexpectedly`. Read the score as "both arms usually finish these tasks" and the cost column as the real finding.

**Model choice was measured, and the fast answer lost.** `evals.latency` timed 17 candidates on the two request shapes a run is made of, and `google/gemini-3.5-flash-lite` won both outright (plan 1.6s against 5.9s, read 0.6s against 1.7s). On the local fixtures that was free: 12/12 at 2.9x the speed. Live sites disagreed, at 12 runs a configuration: flash-lite everywhere scored 8/12, a stronger planner 7/12, a stronger reader 9/12. Twelve runs cannot say which purpose is responsible, so the default does not guess and stays on `gemini-3.8-flash` everywhere except `FIELD_TEXT`, which only produces the string to type. `FASTBROWSE_LLM_MODEL` takes the trade for anyone who wants it.

The local suite passes 6/6 at about $0.005 a task.

Both suites depend on upstream availability and fail loudly when it is absent: one pass scored 0/6 and 10/12 during a Jev `model_unavailable` outage, after the retry budget was exhausted. Re-read a red run before believing it is a regression.

## External benchmarks

We don't vendor other people's benchmarks. To compare on them:

- [BrowserGym](https://github.com/ServiceNow/BrowserGym): a gym interface over MiniWoB, WebArena, WorkArena and others
- [WebArena](https://github.com/web-arena-x/webarena) and [WebArena-Verified](https://github.com/ServiceNow/webarena-verified): self-hosted sites with functional graders
- [Mind2Web](https://github.com/OSU-NLP-Group/Mind2Web) and [Online-Mind2Web](https://github.com/OSU-NLP-Group/Online-Mind2Web): real-site tasks, offline and live
- [WebVoyager](https://github.com/MinorJerry/WebVoyager): live tasks on 15 popular sites
- [WebBench](https://github.com/Halluminate/WebBench): large live read/write task set
- [AssistantBench](https://github.com/oriyor/assistantbench): time-consuming information-seeking tasks
