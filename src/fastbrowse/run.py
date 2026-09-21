"""Run one task end to end: assemble a browser, the model clients and the agent, then return the result.

This is the entry point for embedding fastbrowse in something else. The terminal (`fastbrowse.cli`) and
the live eval are both callers of it, so the assembly has one definition rather than one per caller, and
an embedder gets the parts that are easy to forget: the cloud browser's own cost folded into the result,
downloads kept when a directory is given, and the browser closed on every path out.
"""

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from pydantic import BaseModel

from fastbrowse.adapters.browser_use_cloud import BrowserUseCloudBrowser
from fastbrowse.adapters.local_chrome import async_local_chrome
from fastbrowse.agent import Agent
from fastbrowse.artifacts import DirectorySink
from fastbrowse.browser import BrowserSession, CdpPage
from fastbrowse.browser.recording import Recording
from fastbrowse.clients.environment import load_settings
from fastbrowse.config import Config
from fastbrowse.jev import JevClient
from fastbrowse.llm import LLMClient
from fastbrowse.models import (
    Attachment,
    Authorization,
    BrowserConnection,
    BrowserEvent,
    CostBreakdown,
    CostLine,
    EventHandler,
    FrameHandler,
    Limits,
    LocalChrome,
    RunResult,
    SecretResolver,
    Status,
    UntilCheck,
)
from fastbrowse.page import BrowserError


@asynccontextmanager
async def _browser(
    key: str | None,
    chrome: LocalChrome,
    http: httpx.AsyncClient,
    cost: list[CostLine],
    *,
    profile: str | None = None,
    cdp_url: str | None = None,
    proxy_country: str | None = "us",
    viewport: tuple[int, int] | None = None,
) -> AsyncGenerator[BrowserConnection]:
    """The browser a run drives: one it is handed, a cloud browser, or local Chrome."""
    if cdp_url is not None:
        if key is not None:
            raise BrowserError("cdp_url is a browser to attach to; a cloud key would start a second one")
        if profile is not None:
            raise BrowserError("cloud_profile belongs to a browser fastbrowse starts, not to one it is handed")
        # Nothing to start and nothing to stop: the caller's browser outlives the run. The session opens its
        # own tab and closes only that, so a browser handed over is left exactly as it was found.
        yield BrowserConnection(cdp_url=cdp_url, live_url=None, remote=True)
        return
    if key is None:
        if profile is not None:
            raise BrowserError("cloud_profile names a Browser Use Cloud profile, which needs a cloud browser")
        async with async_local_chrome(chrome) as connection:
            yield connection
        return
    remote = BrowserUseCloudBrowser(key, http=http, profile=profile, proxy_country=proxy_country, viewport=viewport)
    try:
        async with remote:
            yield remote.connection
    finally:
        # Keep the last reported cost even when setup or teardown fails.
        cost.extend(remote.cost)


async def run_task(
    task: str,
    *,
    start: str | None = None,
    browser_api_key: str | None = None,
    chrome: LocalChrome | None = None,
    cloud_profile: str | None = None,
    cdp_url: str | None = None,
    proxy_country: str | None = "us",
    viewport: tuple[int, int] | None = None,
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
    on_frame: FrameHandler | None = None,
    until: UntilCheck | None = None,
    config: Config | None = None,
    http: httpx.AsyncClient | None = None,
    record: Path | None = None,
) -> RunResult:
    """Open `start`, pursue `task`, and return what the run could prove.

    `start` may be omitted, for a caller whose own interface takes a goal and no URL: the first address is
    then proposed from the task and the run begins there, which is what a person does with the same sentence.

    The browser is one of three. `cdp_url` attaches to a browser that is already running, wherever it is
    (a container, a VM, a machine the caller owns), and the run neither starts nor stops it: it opens one tab
    and closes that tab. Otherwise `browser_api_key` runs on a Browser Use Cloud browser, and with neither,
    local Chrome as `chrome` describes (default: from `Settings`, headless with a throwaway profile).
    `cloud_profile` names a profile on that cloud account, so a site someone signed into once in that
    profile is still signed in here; it is the remote counterpart of `LocalChrome.profile`. `proxy_country`
    and `viewport` shape a cloud browser this run starts, and mean nothing for the other two. `jev` and
    `llm` default to clients built from `Settings` (the environment, then `.env`), so an embedder that
    resolves its own credentials, or serves Jev from somewhere else, passes them instead.

    Files the run downloads are discarded unless `downloads` names a directory to keep them in. `record` saves
    an MP4 of the tab, ending on the answer; it needs ffmpeg, and shows whatever the pages showed.
    `on_frame` receives JPEG bytes from the active tab, at most five times a second. Frames are dropped while
    the handler is busy; its failures are logged without interrupting the run. No handler means no live capture.
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
                async with _browser(
                    browser_api_key,
                    chrome or settings.local_chrome(),
                    client,
                    browser_cost,
                    profile=cloud_profile,
                    cdp_url=cdp_url,
                    proxy_country=proxy_country,
                    viewport=viewport,
                ) as connection:
                    if on_event is not None:
                        await on_event(BrowserEvent(live_url=connection.live_url, browser_id=connection.browser_id))
                    session = BrowserSession(
                        connection, sink, refuse_cookie_banners=config.refuse_cookie_banners, on_frame=on_frame
                    )
                    async with session:
                        page = CdpPage(session, config)
                        agent = Agent(page, jev, llm, config=config, secrets=secrets, on_event=on_event)
                        async with nullcontext() if record is None else Recording(session, record) as recording:
                            result = await agent.run(
                                task,
                                start=start,
                                # With no page named, the first address is worked out from the task. That
                                # holds for an attached browser too: the run opens its own tab rather than
                                # taking over one already open, so there is no page it is "already on".
                                choose_start=start is None,
                                output_schema=output_schema,
                                inputs=inputs,
                                attachments=attachments,
                                limits=limits,
                                authorization=authorization,
                                until=until,
                            )
                            if recording is not None:
                                await recording.show_result(task, result)
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
