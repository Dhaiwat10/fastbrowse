"""Read live pages and check the answer drawn from them, without the agent, to measure the reader and claim check.

A live eval spends most of its minutes navigating to the page where a bug shows. This loads the pages once, then
runs the agent's own read, draft and claim check over them N times at once, so a reader or claim-check change is
measured in seconds and its run-to-run spread is visible:

    uv run python -m fastbrowse.evals.probe --task "How many quotes by Albert Einstein are on the site?" \\
        --repeat 6 https://quotes.toscrape.com/ https://quotes.toscrape.com/page/2/

Each run prints one JSON line: the facts the reader kept, the drafted answer, and the claim check's scores.
Navigation is not replayed, so a bug in choosing what to click or read next needs the live eval.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

import httpx

from fastbrowse.adapters.local_chrome import local_chrome
from fastbrowse.agent import next_page_control, next_page_notice, read_question
from fastbrowse.browser import CdpPage
from fastbrowse.browser.session import BrowserSession
from fastbrowse.clients.environment import load_settings
from fastbrowse.config import Config
from fastbrowse.jev import JevClient, NoulAnswer
from fastbrowse.llm import LLMClient
from fastbrowse.memory import Notes
from fastbrowse.models import Artifact, ArtifactKind
from fastbrowse.page import Capture, Observation
from fastbrowse.planner import Plan, RequirementKind, make_plan
from fastbrowse.retrieval import claim_check_questions, draft_answer, read


class _NoArtifacts:
    async def put(self, kind: ArtifactKind, name: str, mime_type: str, content: bytes) -> Artifact:
        raise NotImplementedError("the probe reads pages; it keeps no artifacts")


async def _once(
    llm: LLMClient, jev: JevClient, config: Config, task: str, plan: Plan, pages: Sequence[tuple[Capture, Observation]]
) -> dict[str, object]:
    notes = Notes()
    continuing: set[str] = set()
    rejected = 0
    for capture, observation in pages:
        wanted = [r for r in notes.unresolved(plan) if r.kind is RequirementKind.INFORMATION]
        if not wanted:
            break
        outcome = await read(
            llm,
            capture,
            read_question(task, wanted),
            [r.id for r in wanted],
            notes,
            tokens=config.tokens,
            jev=jev,
            requirements=wanted,
            notice=next_page_notice(next_page_control(observation)),
            continuing=continuing,
        )
        rejected += outcome.rejected_claims
        continuing = {key for key in outcome.continues if not notes.evidenced(key)}
    draft = draft_answer(plan, notes)
    scores: dict[str, float] = {}
    if draft is not None:
        questions = claim_check_questions(draft, notes, tokens=config.tokens)
        evaluation = await jev.evaluate({"answer": draft.answer}, questions)
        scores = {
            key: round(answer.probability, 3)
            for key, answer in evaluation.answers.items()
            if isinstance(answer, NoulAnswer)
        }
    return {
        "facts": [
            {
                "requirement_id": fact.requirement_id,
                "text": fact.text,
                "quote": None if fact.evidence is None else fact.evidence.quote,
                "basis": len(fact.basis),
                "reader": fact.reader.value,
            }
            for fact in notes.facts
        ],
        "rejected_claims": rejected,
        "continuing": sorted(continuing),
        "answer": None if draft is None else draft.answer,
        "scores": scores,
    }


async def main(argv: Sequence[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls", nargs="+", help="The pages to read, in the order the agent would reach them")
    parser.add_argument("--task", required=True)
    parser.add_argument("--repeat", type=int, default=4)
    parser.add_argument("--settle", type=float, default=2.0, help="Seconds to let each page finish rendering")
    args = parser.parse_args(argv)
    settings = load_settings()
    config = Config()
    with local_chrome(settings.local_chrome()) as connection:
        async with (
            BrowserSession(connection, _NoArtifacts()) as session,
            httpx.AsyncClient(timeout=120) as http,
        ):
            llm, jev = settings.llm(http), settings.jev(http)
            planning = asyncio.create_task(make_plan(llm, args.task, start=args.urls[0]))
            page = CdpPage(session, config)
            pages: list[tuple[Capture, Observation]] = []
            for url in args.urls:
                await page.navigate(url)
                await asyncio.sleep(args.settle)
                pages.append((await page.capture(), await page.observe()))
            plan = (await planning).data
            # A page that had not rendered reads as empty, and every run would then agree on an empty answer.
            captured = [
                {"url": capture.url, "blocks": len(capture.blocks), "chars": len(capture.text)} for capture, _ in pages
            ]
            print(
                json.dumps({"requirements": [r.model_dump(mode="json") for r in plan.requirements], "pages": captured})
            )
            runs = await asyncio.gather(
                *(_once(llm, jev, config, args.task, plan, pages) for _ in range(args.repeat)), return_exceptions=True
            )
            for index, run in enumerate(runs):
                line = {"error": repr(run)} if isinstance(run, BaseException) else run
                print(json.dumps({"run": index, **line}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
