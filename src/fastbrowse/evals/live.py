"""Head-to-head on live sites: fastbrowse, jev-ultrafast and hosted Browser Use, same prompts, no dollar or time caps.

    uv run --extra browser-use python -m fastbrowse.evals.live [--only TASK_ID ...] [--category CATEGORY ...]
        [--arms fast ultrafast hosted] [--bitwarden] [--repeat N] [--record DIR]
        [--out artifacts/evals/live.jsonl]

Needs BROWSER_USE_API_KEY (every arm), and the Jev and LLM keys in fastbrowse.clients.environment (fast and
ultrafast arms). Each run prints a WATCH line with the URL where its browser can be watched live.

The fast and ultrafast arms each drive a fresh Browser Use Cloud browser; hosted Browser Use brings its own.
jev-ultrafast runs as its published package in an environment of its own (see scripts/ultrafast_arm.py).

Tasks and their grading live in fastbrowse.evals.live_tasks. With --bitwarden, the fast arm reads each login
task's credentials from its vault item (created by scripts/eval_vault.py) instead of the task. With --record,
each run is saved as DIR/<arm>/<task>-<n>.mp4, n counting up from 1 past any video already there.

Each task runs only on the arms it grades on equal terms (LiveTask.arms). Answer tasks compare fastbrowse with hosted
Browser Use; jev-ultrafast returns no answer, only DONE or BLOCKED. Navigation tasks, graded on the page the
run ended on, compare fastbrowse with jev-ultrafast; the hosted SDK does not say where its browser ended.
fastbrowse must also end with the task's expected status, and jev-ultrafast with DONE.
"""

import argparse
import asyncio
import json
import logging
import os
import statistics
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
from pydantic import BaseModel, JsonValue

import fastbrowse.run
from fastbrowse.adapters.bitwarden import bitwarden_login
from fastbrowse.adapters.browser_use_cloud import BrowserUseCloudBrowser
from fastbrowse.agent import Agent
from fastbrowse.browser import CdpPage
from fastbrowse.browser.recording import Recording
from fastbrowse.clients.environment import load_settings
from fastbrowse.evals.live_tasks import TASKS, Category, LiveTask, Outcome
from fastbrowse.evals.more_tasks import DEV, HELDOUT
from fastbrowse.evals.shadow import shadow_counts
from fastbrowse.models import Authorization, BrowserEvent, Limits, RunResult, StepEvent
from fastbrowse.page import Observation
from fastbrowse.run import run_task
from fastbrowse.safety import ScopedSecrets, origin_of
from fastbrowse.telemetry import TRACE

ARMS = ("fast", "ultrafast", "hosted")
MAX_STEPS = 30
LIMITS = Limits(max_steps=MAX_STEPS)
"""No arm has a dollar or time cap: a cap one arm reaches measures the budget, not the arm, so every run ends
when its agent does. fastbrowse and jev-ultrafast share a step limit; hosted Browser Use has none to set."""

ULTRAFAST = "jev-ultrafast @ git+https://github.com/browser-use/jev-ultrafast@1231850a0bf1a0c0341fe408ef1668dbbfdfac46"
ULTRAFAST_RUNNER = Path(__file__).resolve().parents[3] / "scripts" / "ultrafast_arm.py"
ULTRAFAST_COMMAND = ("uv", "run", "--no-project", "--quiet", "--python", "3.14", "--with", ULTRAFAST, "python")
ULTRAFAST_TEXT_MODEL = "inception/mercury-2.5"
"""jev-ultrafast's own configuration (.env.example): its text helper on OpenRouter, reasoning off."""


def _watch(arm: str, task: LiveTask, live_url: str | None) -> None:
    if live_url:
        print(f"WATCH {arm:9} {task.id:18} {live_url}", flush=True)


def _secrets(task: LiveTask, bitwarden: bool) -> ScopedSecrets | None:
    origin = origin_of(task.start)
    if bitwarden and task.bitwarden_item is not None:
        return ScopedSecrets(bitwarden_login(task.bitwarden_item, origin), origin)
    return ScopedSecrets(task.secrets, origin) if task.secrets else None


def video_path(folder: Path, arm: str, task: LiveTask) -> Path:
    """The first free DIR/<arm>/<task>-<n>.mp4, absolute because the ultrafast runner has its own working directory.

    Counting past existing files means repeated invocations never overwrite a video.
    """
    n = 1
    while (path := folder.resolve() / arm / f"{task.id}-{n}.mp4").exists():
        n += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class _Observed:
    """What the harness sees of one fastbrowse run beyond its result. One per run: eight runs overlap, and state
    kept on the classes let a run starting reset the page another had just observed, which then graded as
    "no final page to grade"."""

    controls: tuple[tuple[str, str | None], ...] | None = None
    observe_error: str | None = None
    """Why the page could not be observed after the run; a grader then reports it had no page."""
    ended: float | None = None
    """When the run ended, so the recording's result card is not timed as the run."""


_observed: ContextVar[_Observed] = ContextVar("live_observed")


class _TimedRecording(Recording):
    async def show_result(self, task: str, result: RunResult) -> None:
        _observed.get().ended = time.monotonic()
        await super().show_result(task, result)


# A popup left open marks the rest of the page aria-hidden, which the agent rightly ignores and a grader
# reading the form must not; so the grader's look lifts the marks, observes as the agent would, and restores them.
_UNHIDE = """(() => {
  const marked = [...document.querySelectorAll('[aria-hidden="true"],[inert]')];
  window.__fastbrowseHidden = marked.map(e => [e, e.getAttribute('aria-hidden'), e.hasAttribute('inert')]);
  marked.forEach(e => { e.removeAttribute('aria-hidden'); e.removeAttribute('inert'); });
})()"""
_RESTORE = """(() => {
  for (const [e, hidden, inert] of window.__fastbrowseHidden || []) {
    if (hidden !== null) e.setAttribute('aria-hidden', hidden);
    if (inert) e.setAttribute('inert', '');
  }
  delete window.__fastbrowseHidden;
})()"""


class _GradedPage(CdpPage):
    async def observe_all(self) -> Observation:
        """Every control on the page, including those a popup has hidden from the agent."""
        session_id = self._session.active_session_id
        await self._evaluate(session_id, _UNHIDE)
        try:
            return await self.observe()
        finally:
            await self._evaluate(session_id, _RESTORE)


class _ObservedAgent(Agent):
    """fastbrowse's agent, with the page it ended on observed once more after the run, for graders that read the
    page itself (what a form ended up holding) rather than anything the agent reported."""

    async def run(self, *args: Any, **kwargs: Any) -> RunResult:
        result = await super().run(*args, **kwargs)
        try:
            observation = await self._page.observe()
            hidden = await self._page.observe_all() if isinstance(self._page, _GradedPage) else None
        except Exception as exc:  # a page that cannot be observed leaves nothing to grade, which the grader reports
            _observed.get().observe_error = f"{type(exc).__name__}: {exc}"
            return result
        controls = (*observation.controls, *(hidden.controls if hidden is not None else ()))
        _observed.get().controls = tuple((c.label, c.value) for c in controls)
        return result


_trace_events: ContextVar[list[object] | None] = ContextVar("live_trace_events", default=None)


class ArmReport(BaseModel):
    """What an arm says about one run beyond its outcome; a field belongs to the arms named beside it."""

    status: str
    seconds: float
    dollars: float | None
    """None when some of the run's spend could not be priced: a known total would then be only a floor."""
    error: str | None = None
    steps: int | None = None
    trace: list[str] = []
    cost_by_component: dict[str, float] = {}
    # fastbrowse
    would_fire: dict[str, int] = {}
    citations: list[JsonValue] = []
    step_log: list[JsonValue] = []
    events: list[object] = []
    unknown_cost: bool | None = None
    observe_error: str | None = None
    seconds_by_call: dict[str, float] = {}
    # jev-ultrafast
    actions: int | None = None
    unmetered_requests: int | None = None
    text_model: str | None = None
    # hosted Browser Use
    model: str | None = None
    session_id: str | None = None


class EvalRow(ArmReport):
    """One line of the results file: the arm's report, graded against the task's truth."""

    arm: str
    task: str
    category: str
    at: float
    concurrency: int | None = None
    status: str | None = None  # a crashed arm reports nothing
    correct: bool
    passed: bool
    failure: str | None
    answer: str | None = None
    data: object = None
    final_url: str | None = None
    video: str | None = None


class _UltrafastReport(BaseModel):
    """The jev-ultrafast runner's result line (`scripts/ultrafast_arm.py`)."""

    status: str
    error: str | None
    seconds: float
    final_url: str | None
    controls: list[tuple[str, str | None]] | None
    steps: int
    actions: int
    trace: list[str]
    jev_dollars: float
    text_dollars: float
    unmetered_requests: int
    text_model: str | None


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.events: list[object] = []
        self.previous_level = next(
            (handler.previous_level for handler in TRACE.handlers if isinstance(handler, _Collect)), TRACE.level
        )

    def emit(self, record: logging.LogRecord) -> None:
        if _trace_events.get() is self.events:
            self.events.append(getattr(record, "trace", record.getMessage()))


@contextmanager
def _traced() -> Generator[list[object]]:
    """Only this run and its child tasks contribute to its trace while other runs overlap."""
    handler = _Collect()
    token = _trace_events.set(handler.events)
    TRACE.addHandler(handler)
    TRACE.setLevel(logging.DEBUG)
    try:
        yield handler.events
    finally:
        TRACE.removeHandler(handler)
        _trace_events.reset(token)
        # Runs can finish out of order; the last collector restores the level from before any run started.
        if not any(isinstance(active, _Collect) for active in TRACE.handlers):
            TRACE.setLevel(handler.previous_level)


async def fast_arm(
    task: LiveTask,
    http: httpx.AsyncClient,
    downloads: Path,
    *,
    bitwarden: bool,
    record: Path | None,
) -> tuple[Outcome, RunResult, _Observed]:
    seen = _Observed()
    token = _observed.set(seen)
    try:
        result = await run_task(
            task.task,
            start=task.start,
            browser_api_key=load_settings().browser_key(),
            output_schema=task.output_schema,
            secrets=_secrets(task, bitwarden),
            limits=LIMITS,
            authorization=Authorization(irreversible_actions=task.authorize),
            downloads=downloads,
            http=http,
            on_event=lambda event: _on_fast_event(task, event),
            record=record,
        )
    finally:
        _observed.reset(token)
    quotes = tuple((e.url, e.quote) for e in result.evidence)
    outcome = Outcome(result.answer, result.data, result.final_url or task.start, quotes, seen.controls)
    return outcome, result, seen


async def _on_fast_event(task: LiveTask, event: StepEvent | BrowserEvent) -> None:
    if isinstance(event, BrowserEvent):
        _watch("fast", task, event.live_url)


async def prepare_ultrafast() -> None:
    """Install jev-ultrafast's environment once, so no run is timed installing it."""
    process = await asyncio.create_subprocess_exec(*ULTRAFAST_COMMAND, "-c", "import jev_ultrafast")
    if await process.wait() != 0:
        raise RuntimeError(f"could not install {ULTRAFAST}")


def _ultrafast_env(cdp_ws: str, runtime: str) -> dict[str, str]:
    settings = load_settings()
    env = dict(os.environ)
    # Its text helper is an OpenRouter model; Jev comes from TypeSafe directly or through the gateway.
    env |= {
        "TEXT_MODEL_API_KEY": settings.openrouter_key(),
        "TEXT_MODEL_BASE_URL": "https://openrouter.ai/api/v1",
        "TEXT_MODEL": ULTRAFAST_TEXT_MODEL,
        "TEXT_MODEL_REASONING": "none",
        "BU_CDP_WS": cdp_ws,
        "BU_NAME": "fastbrowse-eval",
        "BH_RUNTIME_DIR": runtime,
        "BH_TELEMETRY": "0",
        "BH_UPDATE_CHECK": "0",
        "BH_TAB_MARKER": "0",
    }
    for name, key in (
        ("TYPESAFE_API_KEY", settings.typesafe_api_key),
        ("AI_GATEWAY_API_KEY", settings.ai_gateway_api_key),
    ):
        if key is not None:
            env[name] = key.get_secret_value()
    return env


async def ultrafast_arm(task: LiveTask, http: httpx.AsyncClient, *, record: Path | None) -> tuple[Outcome, ArmReport]:
    started = time.monotonic()
    cloud = BrowserUseCloudBrowser(load_settings().browser_key(), http=http)
    async with cloud:
        booted = time.monotonic() - started
        _watch("ultrafast", task, cloud.connection.live_url)
        request = {
            "start": task.start,
            "goal": task.task,
            "cdp_ws": cloud.connection.cdp_url,
            "max_steps": MAX_STEPS,
            "record": None if record is None else str(record),
        }
        with tempfile.TemporaryDirectory(prefix="bh-") as runtime:
            process = await asyncio.create_subprocess_exec(
                *ULTRAFAST_COMMAND,
                str(ULTRAFAST_RUNNER),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                env=_ultrafast_env(cloud.connection.cdp_url, runtime),
                cwd=runtime,
            )
            stdout, _ = await process.communicate(json.dumps(request).encode())
        lines = stdout.decode().strip().splitlines()
        if process.returncode != 0 or not lines:
            raise RuntimeError(f"the jev-ultrafast runner exited {process.returncode} without a result")
        ran = _UltrafastReport.model_validate_json(lines[-1])
    browser = sum(line.dollars or 0 for line in cloud.cost)
    dollars = ran.jev_dollars + ran.text_dollars + browser
    report = ArmReport(
        status=ran.status,
        seconds=round(booted + ran.seconds, 2),
        dollars=None if ran.unmetered_requests else round(dollars, 5),
        error=ran.error,
        steps=ran.steps,
        trace=ran.trace,
        cost_by_component={"jev": ran.jev_dollars, "llm": ran.text_dollars, "browser": round(browser, 5)},
        actions=ran.actions,
        unmetered_requests=ran.unmetered_requests,
        text_model=ran.text_model,
    )
    observed = None if ran.controls is None else tuple(ran.controls)
    return Outcome(None, None, ran.final_url, controls=observed), report


async def hosted_arm(task: LiveTask, http: httpx.AsyncClient, *, record: Path | None) -> tuple[Outcome, ArmReport]:
    from browser_use_sdk.v3 import AsyncBrowserUse  # an optional extra

    client = AsyncBrowserUse(api_key=load_settings().browser_key())
    run = client.run(
        f"Start at {task.start}. {task.task}",
        output_schema=task.output_schema,
        proxy_country_code="us",
        sensitive_data=dict(task.secrets) or None,
        enable_recording=record is not None,
    )
    started = time.monotonic()
    finishing = asyncio.ensure_future(run)
    # The session id appears once the SDK has created the session, which is when its live URL exists.
    while run.session_id is None and not finishing.done():
        await asyncio.wait({finishing}, timeout=0.2)
    if run.session_id is not None:
        _watch("hosted", task, (await client.sessions.get(run.session_id)).live_url)
    # gather, not await: the SDK raises on output that fails the task's schema, before the session's cost is read.
    await asyncio.gather(finishing, return_exceptions=True)
    seconds = time.monotonic() - started
    if (error := finishing.exception()) is None:
        result = finishing.result()
        session, output = result.session, result.output
    elif run.session_id is not None:
        # The SDK raises on output that fails the task's schema; the session still holds that output and its cost.
        session = await client.sessions.get(run.session_id)
        output = session.output
    else:
        raise error
    if isinstance(output, BaseModel):
        outcome = Outcome(output.model_dump_json(), output.model_dump(), None)
    elif isinstance(output, dict):
        outcome = Outcome(json.dumps(output), output, None)
    else:
        outcome = Outcome(str(output) if output else None, None, None)
    cost = session.total_cost_usd
    report = ArmReport(
        status=session.status.value,
        seconds=round(seconds, 2),
        dollars=None if cost is None else float(cost),
        model=str(session.model.value if hasattr(session.model, "value") else session.model),
        steps=session.step_count,
        session_id=str(session.id),
    )
    if record is not None:
        urls = await client.sessions.wait_for_recording(session.id, timeout=60)
        if urls:
            response = await http.get(urls[0], follow_redirects=True)
            if response.is_success:
                await asyncio.to_thread(record.write_bytes, response.content)
    return outcome, report


def _video(record: Path | None) -> str | None:
    return str(record) if record is not None and record.exists() else None


async def run_arm(
    arm: str,
    task: LiveTask,
    http: httpx.AsyncClient,
    downloads: Path,
    *,
    bitwarden: bool,
    record: Path | None,
) -> EvalRow:
    truth = await task.truth(http)
    started = time.monotonic()
    at = time.time()
    try:
        if arm == "fast":
            outcome, report = await _fast_report(
                task, http, downloads, bitwarden=bitwarden, record=record, started=started
            )
        elif arm == "ultrafast":
            outcome, report = await ultrafast_arm(task, http, record=record)
        else:
            outcome, report = await hosted_arm(task, http, record=record)
    except Exception as exc:  # a crashed arm is a failed task, recorded rather than aborting the comparison
        return EvalRow(
            arm=arm,
            task=task.id,
            category=task.category.value,
            at=at,
            correct=False,
            passed=False,
            failure=f"{type(exc).__name__}: {exc}",
            seconds=round(time.monotonic() - started, 1),
            dollars=None,
            video=_video(record),
        )
    failure = task.check(outcome, truth)
    # Right and proven are graded apart: a correct answer the agent could not back with quotes is a
    # different defect from a wrong one, and one pass/fail column hid which the suite was showing.
    correct = failure is None
    expected = {"fast": task.expect.value, "ultrafast": "done"}.get(arm)
    if expected is not None and failure is None and report.status != expected:
        failure = f"status {report.status}, expected {expected}"
    return EvalRow.model_validate(
        report.model_dump()
        | {
            "arm": arm,
            "task": task.id,
            "category": task.category.value,
            "at": at,
            "correct": correct,
            "passed": failure is None,
            "failure": failure,
            "seconds": round(report.seconds, 1),
            "dollars": None if report.dollars is None else round(report.dollars, 5),
            "answer": outcome.answer,
            "data": outcome.data,
            "final_url": outcome.final_url,
            "video": _video(record),
        }
    )


async def _fast_report(
    task: LiveTask, http: httpx.AsyncClient, downloads: Path, *, bitwarden: bool, record: Path | None, started: float
) -> tuple[Outcome, ArmReport]:
    with _traced() as events, shadow_counts() as would_fire:
        outcome, result, seen = await fast_arm(task, http, downloads, bitwarden=bitwarden, record=record)
    cost = result.cost
    return outcome, ArmReport(
        status=result.status.value,
        seconds=(seen.ended or time.monotonic()) - started,
        # An unknown line makes the known total a floor, not a cost.
        dollars=None if cost.has_unknown else cost.known_dollars,
        # A shadow tripwire only earns arming on evidence from LIVE sites: the local fixtures never
        # grind and never spin, so a zero there says nothing about the rate that matters.
        would_fire=dict(would_fire),
        error=result.error,
        citations=[citation.model_dump(mode="json") for citation in result.citations],
        trace=[f"{s.operation.value} {s.target or ''} -> {s.outcome.value}" for s in result.steps],
        # Enough to say why a run failed without running it again: every step as the agent judged it, and
        # each read, done check, verification, claim check and recovery in order.
        step_log=[s.model_dump(mode="json") for s in result.steps],
        events=events,
        steps=len(result.steps),
        unknown_cost=cost.has_unknown,
        observe_error=seen.observe_error,
        seconds_by_call=cost.seconds_by_call(),
        cost_by_component={
            c: round(sum(line.dollars or 0 for line in cost.lines if line.component == c), 5)
            for c in {line.component.value for line in cost.lines}
        },
    )


SUITES: dict[str, tuple[LiveTask, ...]] = {"core": TASKS, "dev": DEV, "heldout": HELDOUT}
"""`core` is the published suite; `dev` and `heldout` are the split in `more_tasks`."""


def summarize(rows: list[EvalRow], arms: list[str]) -> None:
    for arm in arms:
        arm_rows = [r for r in rows if r.arm == arm]
        if not arm_rows:
            continue
        passed = sum(r.passed for r in arm_rows)
        correct = sum(r.correct for r in arm_rows)
        priced = [r.dollars for r in arm_rows if r.dollars is not None]
        seconds = [r.seconds for r in arm_rows]
        unknown = len(arm_rows) - len(priced)
        print(
            f"{arm}: {passed}/{len(arm_rows)} passed, {correct} correct, median {statistics.median(seconds):.1f}s, "
            f"${sum(priced):.4f}" + (f" ({unknown} runs of unknown cost)" if unknown else "")
        )
        calls: dict[str, float] = {}
        for r in arm_rows:
            for label, spent in r.seconds_by_call.items():
                calls[label] = calls.get(label, 0.0) + spent
        for label, spent in sorted(calls.items(), key=lambda item: -item[1]):
            print(f"  {label:18} {spent / len(arm_rows):5.1f}s a task")
        # RUNS affected, not fires, and only among the passing ones - that is the false-positive rate the
        # arming decision turns on. Summing fires reads like a rate and is not one: four repetitions inside
        # a single grinding run reported as "4 on 19" invites the reading "4 runs of 19", which is 5%
        # misread as 21%. A tripwire firing on a run that failed anyway costs nothing and is excluded.
        shadow: Counter[str] = Counter()
        for r in arm_rows:
            if r.passed:
                shadow.update(set(r.would_fire))
        for tripwire, runs in shadow.most_common():
            print(f"  would-fire {tripwire:18} {runs}/{passed} passing runs")


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=[])
    parser.add_argument("--suite", nargs="*", default=["core"], choices=list(SUITES), help="task sets to run")
    parser.add_argument("--category", nargs="*", default=[], choices=[c.value for c in Category])
    parser.add_argument("--bitwarden", action="store_true", help="login credentials from the vault items")
    parser.add_argument("--arms", nargs="*", default=list(ARMS), choices=ARMS)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--record", type=Path, metavar="DIR", help="save each run as DIR/<arm>/<task>-<n>.mp4")
    parser.add_argument("--out", type=Path, default=Path("artifacts/evals/live.jsonl"))
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="runs in flight at once (default 8). A run waits on pages rather than on this machine, so eight at a "
        "time timed the same as one at a time; compare arms only under the same setting.",
    )
    args = parser.parse_args(argv)
    tasks = [
        t
        for suite in args.suite
        for t in SUITES[suite]
        if (not args.only or t.id in args.only) and (not args.category or t.category.value in args.category)
    ]
    if "ultrafast" in args.arms:
        await prepare_ultrafast()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    rows: list[EvalRow] = []
    # Patched once for the whole run: overlapping runs each patching and restoring would interleave, and a run ending
    # would restore the originals under a run still going.
    patches = (
        mock.patch.object(fastbrowse.run, "Recording", _TimedRecording),
        mock.patch.object(fastbrowse.run, "Agent", _ObservedAgent),
        mock.patch.object(fastbrowse.run, "CdpPage", _GradedPage),
    )
    gate = asyncio.Semaphore(args.concurrency)
    with (
        patches[0],
        patches[1],
        patches[2],
        tempfile.TemporaryDirectory() as downloads,
        args.out.open("a", encoding="utf-8") as out,
    ):
        async with httpx.AsyncClient(timeout=60) as http:

            async def one(arm: str, task: LiveTask, record: Path | None) -> EvalRow:
                async with gate:
                    row = await run_arm(arm, task, http, Path(downloads), bitwarden=args.bitwarden, record=record)
                row = row.model_copy(update={"concurrency": args.concurrency})
                # Trace records hold whatever a component logged, so anything JSON cannot hold is written as text.
                out.write(row.model_dump_json(fallback=str) + "\n")
                out.flush()
                mark = "PASS" if row.passed else "FAIL"
                print(
                    f"{mark} {arm:9} {task.id:20} {row.seconds!s:>6}s ${row.dollars!s:<8}",
                    row.failure or "",
                    flush=True,
                )
                return row

            planned = [
                (arm, task, None if args.record is None else video_path(args.record, arm, task))
                for _ in range(args.repeat)
                for task in tasks
                for arm in args.arms
                if arm in task.arms
            ]
            rows = list(await asyncio.gather(*(one(arm, task, record) for arm, task, record in planned)))
    summarize(rows, args.arms)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
