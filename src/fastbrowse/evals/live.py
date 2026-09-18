"""Head-to-head on live sites: fastbrowse on a Browser Use Cloud browser vs hosted Browser Use, same prompts.

    uv run --extra browser-use python -m fastbrowse.evals.live [--only TASK_ID ...] [--arms fast hosted]
        [--repeat N] [--out artifacts/evals/live.jsonl]

Needs BROWSER_USE_API_KEY (both arms), and the Jev and LLM keys in fastbrowse.clients.environment (fast arm).
Each run prints a WATCH line with the URL where its browser can be watched live.

Truth is fetched from each site's own API at run time, so the grade tracks the live page rather than a stale
fixture. Both arms are graded on their answer. The fast arm is also graded on the page it actually ended on;
the hosted SDK does not expose a final URL, so its navigation tasks rest on the answer alone.
"""

import argparse
import asyncio
import json
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlparse

import httpx
from pydantic import BaseModel

from fastbrowse.clients.environment import load_settings
from fastbrowse.models import BrowserEvent, CostBreakdown, Limits, RunResult, Status, StepEvent
from fastbrowse.run import run_task
from fastbrowse.safety import ScopedSecrets, origin_of

HOSTED_MAX_DOLLARS = 0.50


class Release(BaseModel):
    package: str
    version: str


@dataclass(frozen=True, slots=True)
class Outcome:
    answer: str | None
    data: object
    final_url: str | None
    """Only the fast arm can observe where it ended."""


type Truth = Callable[[httpx.AsyncClient], Awaitable[object]]
type Check = Callable[[Outcome, object], str | None]


@dataclass(frozen=True, slots=True)
class LiveTask:
    id: str
    start: str
    task: str
    truth: Truth
    check: Check
    secrets: Mapping[str, str] = field(default_factory=dict[str, str])
    output_schema: type[BaseModel] | None = None


async def _json(http: httpx.AsyncClient, url: str) -> object:
    response = await http.get(url, headers={"Accept": "application/json"})
    response.raise_for_status()
    return response.json()


async def _httpx_version(http: httpx.AsyncClient) -> object:
    body = await _json(http, "https://pypi.org/pypi/httpx/json")
    assert isinstance(body, dict)
    return str(body["info"]["version"])  # pyright: ignore[reportUnknownArgumentType]


async def _hn_top_titles(http: httpx.AsyncClient) -> object:
    ids = await _json(http, "https://hacker-news.firebaseio.com/v0/topstories.json")
    assert isinstance(ids, list)
    # The front page reorders during a run, so any of the leading stories counts as "the top story".
    items = await asyncio.gather(
        *(_json(http, f"https://hacker-news.firebaseio.com/v0/item/{i}.json") for i in ids[:5])  # pyright: ignore[reportUnknownVariableType]
    )
    return [str(item["title"]) for item in items if isinstance(item, dict)]  # pyright: ignore[reportUnknownArgumentType]


async def _httpx_license(http: httpx.AsyncClient) -> object:
    body = await _json(http, "https://api.github.com/repos/encode/httpx")
    assert isinstance(body, dict)
    return str(body["license"]["spdx_id"])  # pyright: ignore[reportUnknownArgumentType]


async def _constant(value: object) -> object:
    return value


def _answer_has(outcome: Outcome, *needles: str) -> str | None:
    answer = (outcome.answer or "").casefold()
    missing = [n for n in needles if n.casefold() not in answer]
    return f"answer lacks {missing}: {outcome.answer!r}" if missing else None


def _ended_on(outcome: Outcome, path: str) -> str | None:
    if outcome.final_url is None:
        return None
    actual = unquote(urlparse(outcome.final_url).path).rstrip("/")
    return None if actual == path else f"ended on {outcome.final_url}, expected path {path}"


def _version(outcome: Outcome, truth: object) -> str | None:
    return _answer_has(outcome, str(truth))


def _hn_top(outcome: Outcome, truth: object) -> str | None:
    assert isinstance(truth, list)
    answer = (outcome.answer or "").casefold()
    titles = [str(t) for t in truth]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return None if any(t.casefold() in answer for t in titles) else f"no leading title in {outcome.answer!r}"


def _license(outcome: Outcome, truth: object) -> str | None:
    return _answer_has(outcome, str(truth).removesuffix("-Clause"))


def _godel(outcome: Outcome, truth: object) -> str | None:
    return _answer_has(outcome, str(truth)) or _ended_on(outcome, "/wiki/Gödel's_incompleteness_theorems")


def _cart(outcome: Outcome, _: object) -> str | None:
    return _answer_has(outcome, "backpack") or _ended_on(outcome, "/cart.html")


def _release(outcome: Outcome, truth: object) -> str | None:
    data = outcome.data
    if not isinstance(data, dict):
        return f"no structured data: {data!r}"
    expected = {"package": "httpx", "version": str(truth)}
    return None if {k: str(v).strip() for k, v in data.items()} == expected else f"data {data}, expected {expected}"  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


TASKS: tuple[LiveTask, ...] = (
    LiveTask(
        "pypi-version", "https://pypi.org/", "What is the latest released version of httpx?", _httpx_version, _version
    ),
    LiveTask(
        "pypi-structured",
        "https://pypi.org/",
        "Find the httpx package and report its name and latest released version.",
        _httpx_version,
        _release,
        output_schema=Release,
    ),
    LiveTask(
        "hn-top",
        "https://news.ycombinator.com/",
        "What is the title of the top story right now?",
        _hn_top_titles,
        _hn_top,
    ),
    LiveTask(
        "github-license",
        "https://github.com/encode",
        "Open the httpx repository and tell me which license it uses.",
        _httpx_license,
        _license,
    ),
    LiveTask(
        "wiki-godel",
        "https://en.wikipedia.org/wiki/Main_Page",
        "Search for Gödel's incompleteness theorems, open that article, and tell me the year they were published.",
        lambda _: _constant("1931"),
        _godel,
    ),
    LiveTask(
        "saucedemo-cart",
        "https://www.saucedemo.com/",
        "Log in as standard_user with the saved password, add the Sauce Labs Backpack to the cart, open the cart, "
        "and tell me what is in it.",
        lambda _: _constant(None),
        _cart,
        secrets={"password": "secret_sauce"},
    ),
)


def _watch(arm: str, task: LiveTask, live_url: str | None) -> None:
    if live_url:
        print(f"WATCH {arm:6} {task.id:18} {live_url}", flush=True)


async def fast_arm(
    task: LiveTask, http: httpx.AsyncClient, downloads: Path
) -> tuple[Outcome, RunResult, CostBreakdown]:
    result = await run_task(
        task.task,
        start=task.start,
        browser_api_key=load_settings().browser_key(),
        output_schema=task.output_schema,
        secrets=ScopedSecrets(task.secrets, origin_of(task.start)) if task.secrets else None,
        limits=Limits(max_steps=30, max_dollars=0.25, max_seconds=300),
        downloads=downloads,
        http=http,
        on_event=lambda event: _on_fast_event(task, event),
    )
    return Outcome(result.answer, result.data, result.final_url or task.start), result, result.cost


async def _on_fast_event(task: LiveTask, event: StepEvent | BrowserEvent) -> None:
    if isinstance(event, BrowserEvent):
        _watch("fast", task, event.live_url)


async def hosted_arm(task: LiveTask) -> tuple[Outcome, str, float | None]:
    from browser_use_sdk.v3 import AsyncBrowserUse  # pyright: ignore[reportMissingTypeStubs] - optional extra

    client = AsyncBrowserUse(api_key=load_settings().browser_key())
    run = client.run(
        f"Start at {task.start}. {task.task}",
        output_schema=task.output_schema,
        max_cost_usd=HOSTED_MAX_DOLLARS,
        proxy_country_code="us",
        sensitive_data=dict(task.secrets) or None,
    )
    finishing = asyncio.ensure_future(run)
    # The session id appears once the SDK has created the session, which is when its live URL exists.
    while run.session_id is None and not finishing.done():
        await asyncio.wait({finishing}, timeout=0.2)
    if run.session_id is not None:
        _watch("hosted", task, (await client.sessions.get(run.session_id)).live_url)
    result = await finishing
    session = result.session
    output = result.output
    if isinstance(output, BaseModel):
        outcome = Outcome(output.model_dump_json(), output.model_dump(), None)
    else:
        outcome = Outcome(str(output) if output else None, None, None)
    status = session.status.value
    cost = session.total_cost_usd
    return outcome, status, None if cost is None else float(cost)


async def run_arm(arm: str, task: LiveTask, http: httpx.AsyncClient, downloads: Path) -> dict[str, object]:
    truth = await task.truth(http)
    started = time.monotonic()
    row: dict[str, object] = {"arm": arm, "task": task.id}
    try:
        if arm == "fast":
            outcome, result, cost = await fast_arm(task, http, downloads)
            status = result.status.value
            row["error"] = result.error
            row["trace"] = [f"{s.operation.value} {s.target or ''} -> {s.outcome.value}" for s in result.steps]
            dollars: float | None = cost.known_dollars
            row["unknown_cost"] = cost.has_unknown
            row["seconds_by_call"] = cost.seconds_by_call()
            row["cost_by_component"] = {
                c: round(sum(line.dollars or 0 for line in cost.lines if line.component == c), 5)
                for c in {line.component.value for line in cost.lines}
            }
        else:
            outcome, status, dollars = await hosted_arm(task)
    except Exception as exc:  # a crashed arm is a failed task, recorded rather than aborting the comparison
        row |= {
            "passed": False,
            "failure": f"{type(exc).__name__}: {exc}",
            "seconds": round(time.monotonic() - started, 1),
        }
        return row
    failure = task.check(outcome, truth)
    # Right and proven are graded apart: a correct answer the agent could not back with quotes is a
    # different defect from a wrong one, and one pass/fail column hid which the suite was showing.
    correct = failure is None
    if arm == "fast" and failure is None and status != Status.COMPLETE.value:
        failure = f"status {status}"
    return row | {
        "correct": correct,
        "passed": failure is None,
        "failure": failure,
        "status": status,
        "seconds": round(time.monotonic() - started, 1),
        "dollars": None if dollars is None else round(dollars, 5),
        "answer": outcome.answer,
        "data": outcome.data,
        "final_url": outcome.final_url,
    }


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=[])
    parser.add_argument("--arms", nargs="*", default=["fast", "hosted"], choices=["fast", "hosted"])
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--out", type=Path, default=Path("artifacts/evals/live.jsonl"))
    args = parser.parse_args(argv)
    tasks = [t for t in TASKS if not args.only or t.id in args.only]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory() as downloads, args.out.open("a") as out:
        async with httpx.AsyncClient(timeout=60) as http:
            for _ in range(args.repeat):
                for task in tasks:
                    for arm in args.arms:
                        row = await run_arm(arm, task, http, Path(downloads))
                        rows.append(row)
                        out.write(json.dumps(row, default=str) + "\n")
                        out.flush()
                        mark = "PASS" if row["passed"] else "FAIL"
                        print(
                            f"{mark} {arm:6} {task.id:16} {row.get('seconds')!s:>6}s ${row.get('dollars')!s:<8}",
                            row["failure"] or "",
                            flush=True,
                        )
    for arm in args.arms:
        arm_rows = [r for r in rows if r["arm"] == arm]
        passed = sum(bool(r["passed"]) for r in arm_rows)
        dollars = sum(float(d) for r in arm_rows if isinstance(d := r.get("dollars"), int | float))
        seconds = sum(float(s) for r in arm_rows if isinstance(s := r.get("seconds"), int | float))
        correct = sum(bool(r.get("correct")) for r in arm_rows)
        print(f"{arm}: {passed}/{len(arm_rows)} passed, {correct} correct, ${dollars:.4f}, {seconds:.0f}s")
        calls: dict[str, float] = {}
        for r in arm_rows:
            for label, spent in cast(dict[str, float], r.get("seconds_by_call", {})).items():
                calls[label] = calls.get(label, 0.0) + spent
        for label, spent in sorted(calls.items(), key=lambda item: -item[1]):
            print(f"  {label:18} {spent / len(arm_rows):5.1f}s a task")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
