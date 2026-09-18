"""Run one task end to end: assemble a browser, the model clients and the agent, then return the result.

This is the entry point for embedding fastbrowse in something else. The terminal (`fastbrowse.cli`) and
the live eval are both callers of it, so the assembly has one definition rather than one per caller, and
an embedder gets the parts that are easy to forget: the cloud browser's own cost folded into the result,
downloads landing somewhere that outlives the run, and the browser closed on every path out.
"""

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from pydantic import BaseModel

from fastbrowse.adapters.browser_use_cloud import BrowserUseCloudBrowser
from fastbrowse.adapters.local_chrome import async_local_chrome
from fastbrowse.agent import Agent
from fastbrowse.artifacts import DirectorySink
from fastbrowse.browser import BrowserSession, CdpPage
from fastbrowse.clients.environment import Settings, load_settings
from fastbrowse.config import Config
from fastbrowse.jev import JevClient
from fastbrowse.llm import LLMClient
from fastbrowse.models import (
    Attachment,
    Authorization,
    BrowserConnection,
    CostBreakdown,
    CostLine,
    EventHandler,
    Limits,
    RunResult,
    SecretResolver,
    Status,
    UntilCheck,
)
from fastbrowse.page import BrowserError


@asynccontextmanager
async def _browser(
    key: str | None, settings: Settings, http: httpx.AsyncClient, cost: list[CostLine]
) -> AsyncGenerator[BrowserConnection]:
    """A cloud browser when a key is given, otherwise local headless Chrome."""
    if key is None:
        async with async_local_chrome(settings.chrome) as connection:
            yield connection
        return
    remote = BrowserUseCloudBrowser(key, http=http)
    try:
        async with remote:
            yield remote.connection
    finally:
        # Keep the last reported cost even when setup or teardown fails.
        cost.extend(remote.cost)


async def run_task(
    task: str,
    *,
    start: str,
    browser_api_key: str | None = None,
    jev: JevClient | None = None,
    llm: LLMClient | None = None,
    output_schema: type[BaseModel] | None = None,
    inputs: Mapping[str, str] | None = None,
    attachments: Sequence[Attachment] = (),
    limits: Limits | None = None,
    authorization: Authorization | None = None,
    secrets: SecretResolver | None = None,
    downloads: Path | None = None,
    on_event: EventHandler | None = None,
    until: UntilCheck | None = None,
    config: Config | None = None,
    http: httpx.AsyncClient | None = None,
) -> RunResult:
    """Open `start`, pursue `task`, and return what the run could prove.

    `browser_api_key` picks the browser: a Browser Use Cloud key runs there, and None runs local
    headless Chrome. `jev` and `llm` default to clients built from `Settings` (the environment, then
    `.env`), so an embedder that resolves its own credentials passes them instead.

    Files the run downloads are discarded unless `downloads` names a directory to keep them in.
    """
    config = config or Config()
    settings = load_settings()
    browser_cost: list[CostLine] = []
    with TemporaryDirectory() as scratch:
        sink = DirectorySink(downloads or Path(scratch))
        async with httpx.AsyncClient(timeout=60) if http is None else _borrowed(http) as client:
            jev = jev or settings.jev(client)
            llm = llm or settings.llm(client)
            session: BrowserSession | None = None
            result: RunResult | None = None
            try:
                async with _browser(browser_api_key, settings, client, browser_cost) as connection:
                    session = BrowserSession(connection, sink)
                    async with session:
                        page = CdpPage(session, config)
                        await page.navigate(start)
                        result = await Agent(page, jev, llm, config=config, secrets=secrets, on_event=on_event).run(
                            task,
                            output_schema=output_schema,
                            inputs=inputs,
                            attachments=attachments,
                            limits=limits,
                            authorization=authorization,
                            until=until,
                        )
            except BrowserError as exc:
                result = result or RunResult(
                    status=Status.ERROR,
                    answer=None,
                    data=None,
                    evidence=(),
                    steps=(),
                    cost=CostBreakdown(),
                    artifacts=session.artifacts if session is not None else (),
                )
                result = result.model_copy(update={"status": Status.ERROR, "error": str(exc)})
    assert result is not None
    return result.model_copy(update={"cost": CostBreakdown(lines=(*result.cost.lines, *browser_cost))})


@asynccontextmanager
async def _borrowed(http: httpx.AsyncClient) -> AsyncGenerator[httpx.AsyncClient]:
    """A caller's client outlives the run, so hand it back rather than closing it."""
    yield http
