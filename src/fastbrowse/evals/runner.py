"""Run the local eval tasks with real Jev and LLM clients against fixture sites in a headless Chrome.

    uv run python -m fastbrowse.evals.runner [--only TASK_ID ...] [--repeat N] [--out results.jsonl]

Needs AI_GATEWAY_API_KEY (Jev) and OPENROUTER_API_KEY (LLM); FASTBROWSE_LLM_MODEL overrides the LLM.
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import httpx

from fastbrowse.agent import Agent
from fastbrowse.artifacts import DirectorySink
from fastbrowse.browser import BrowserSession, CdpPage
from fastbrowse.clients.openai_compatible import OpenAICompatibleLLM
from fastbrowse.clients.vercel import VercelGatewayJevClient
from fastbrowse.config import Config
from fastbrowse.evals.local import Recorder, fixture_server, local_chrome
from fastbrowse.evals.tasks import TASKS, LocalTask
from fastbrowse.models import BrowserConnection, Limits, LLMPurpose

DEFAULT_LLM = "google/gemini-3.8-flash"


async def run_task(
    task: LocalTask, base_url: str, recorder: Recorder, ws_url: str, http: httpx.AsyncClient, sink: DirectorySink
) -> dict[str, object]:
    recorder.clear()
    config = Config()
    jev = VercelGatewayJevClient(os.environ["AI_GATEWAY_API_KEY"], http=http)
    model = os.environ.get("FASTBROWSE_LLM_MODEL", DEFAULT_LLM)
    llm = OpenAICompatibleLLM(
        os.environ["OPENROUTER_API_KEY"],
        http=http,
        base_url="https://openrouter.ai/api/v1",
        models=dict.fromkeys(LLMPurpose, model),
    )
    started = time.monotonic()
    async with BrowserSession(BrowserConnection(cdp_url=ws_url, live_url=None, remote=False), sink) as session:
        page = CdpPage(session, config)
        await page.navigate(base_url + task.start)
        result = await Agent(page, jev, llm, config=config).run(
            task.task,
            inputs=task.inputs,
            output_schema=task.output_schema,
            limits=Limits(max_steps=25, max_dollars=0.25, max_seconds=180),
            authorization=task.authorization,
        )
    failure = task.check(result, recorder.snapshot())
    return {
        "task": task.id,
        "passed": failure is None,
        "failure": failure,
        "status": result.status.value,
        "seconds": round(time.monotonic() - started, 1),
        "dollars": round(result.cost.known_dollars, 5),
        "unknown_cost": result.cost.has_unknown,
        "steps": len(result.steps),
        "answer": result.answer,
        "data": result.data,
        "error": result.error,
        "trace": [f"{s.operation.value} {s.target or ''} -> {s.outcome.value}" for s in result.steps],
    }


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=[])
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--out", type=Path, default=Path("artifacts/evals/local.jsonl"))
    args = parser.parse_args(argv)
    tasks = [t for t in TASKS if not args.only or t.id in args.only]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    with (
        fixture_server() as (base_url, recorder),
        local_chrome() as ws_url,
        tempfile.TemporaryDirectory() as downloads,
        args.out.open("a") as out,
    ):
        async with httpx.AsyncClient(timeout=60) as http:
            for _ in range(args.repeat):
                for task in tasks:
                    row = await run_task(task, base_url, recorder, ws_url, http, DirectorySink(Path(downloads)))
                    rows.append(row)
                    out.write(json.dumps(row) + "\n")
                    mark = "PASS" if row["passed"] else "FAIL"
                    summary = f"{mark} {task.id:28} {row['status']:20} {row['seconds']:>6}s ${row['dollars']:<8}"
                    print(summary, row["failure"] or "")
    passed = sum(bool(r["passed"]) for r in rows)
    print(f"{passed}/{len(rows)} passed")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
