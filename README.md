# fastbrowse

A browser agent that picks instead of generating: TypeSafe's Jev chooses each action and target, an LLM plans, reads and writes answers, and code owns verification, safety and secrets.

Status: pre-alpha, private.

## Run a task

```sh
uv sync
export AI_GATEWAY_API_KEY=...   # or TYPESAFE_API_KEY, for Jev
export OPENROUTER_API_KEY=...   # the LLM; FASTBROWSE_LLM_MODEL overrides google/gemini-3.8-flash

uv run fastbrowse "What is the latest released version of httpx?" --start https://pypi.org/ --cloud
```

The default is a local headless Chrome. `--cloud` uses a Browser Use Cloud browser (`BROWSER_USE_API_KEY`), which sites are less likely to CAPTCHA. `--authorize` allows submit/pay/delete/send actions. Without it the run stops at `NEEDS_CONFIRMATION`. `--secret password=SITE_PASSWORD` lets the agent type the value of `$SITE_PASSWORD` on the start origin. Models only ever see the name.

## Embed it

```python
async with BrowserSession(connection, DirectorySink(path)) as session:
    page = CdpPage(session, Config())
    await page.navigate(url)
    result = await Agent(page, jev, llm, secrets=resolver).run(task, output_schema=MyModel, limits=Limits(max_dollars=0.10))
```

`result.status` is `COMPLETE` only when every requirement of the task is backed by quoted page evidence. Otherwise it names why the run stopped (`NEEDS_LOGIN`, `NEEDS_CONFIRMATION`, `STUCK`, `UNVERIFIED`, `BUDGET_EXCEEDED`, ...). `result.cost` itemizes Jev, LLM, browser and proxy spend, each marked metered, estimated or unknown.

## Read more

- [docs/design.md](docs/design.md): who owns which decision, and the browser capabilities verified over plain CDP
- [docs/evals.md](docs/evals.md): local fixtures, the live head-to-head against hosted Browser Use, results and links to external benchmarks
