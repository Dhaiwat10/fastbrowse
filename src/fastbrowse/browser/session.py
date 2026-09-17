"""One owned tab on a `BrowserConnection`: attach, OOPIF sessions, popup ownership, dialogs, downloads.

Session lifecycle, the "foreground the owned tab on a remote browser" behaviour, and the freshness/guard
primitives `page.py` builds on were proven live in jev-ultrafast (MIT): jev_ultrafast/browser.py.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from collections.abc import Coroutine
from dataclasses import dataclass
from types import TracebackType
from typing import Self

from cdp_use.cdp.fetch.events import RequestPausedEvent
from cdp_use.cdp.fetch.types import RequestPattern
from cdp_use.cdp.page.events import JavascriptDialogOpeningEvent
from cdp_use.cdp.target.events import (
    AttachedToTargetEvent,
    DetachedFromTargetEvent,
    TargetCreatedEvent,
    TargetDestroyedEvent,
    TargetInfoChangedEvent,
)
from cdp_use.client import CDPClient

from fastbrowse.models import Artifact, ArtifactKind, ArtifactSink
from fastbrowse.models import BrowserConnection as BrowserConnectionModel
from fastbrowse.page import Dialog, Tab

# Response-stage interception is enough: fastbrowse only needs the bytes of a save-as download, never to
# rewrite a request. Document covers navigations to a downloadable URL; Other covers a fetch/anchor-click
# download that never navigates the frame at all.
DOWNLOAD_PATTERNS: tuple[RequestPattern, ...] = (
    {"urlPattern": "*", "resourceType": "Document", "requestStage": "Response"},
    {"urlPattern": "*", "resourceType": "Other", "requestStage": "Response"},
)
_ENABLE_DOMAINS = ("Page", "Runtime", "DOM")


@dataclass
class _TabState:
    target_id: str
    session_id: str
    url: str = "about:blank"
    title: str = ""
    opener_id: str | None = None


class BrowserSession:
    """Async context manager over one owned tab (plus any popups it opens).

    Closes only the targets it owns; the caller's browser and any of its other tabs are never touched.
    Holds no module-level state, so many sessions can run concurrently in one process.
    """

    def __init__(
        self,
        connection: BrowserConnectionModel,
        artifact_sink: ArtifactSink,
        max_download_bytes: int = 200 * 1024 * 1024,
    ) -> None:
        self._connection = connection
        self._artifact_sink = artifact_sink
        self._max_download_bytes = max_download_bytes
        self._client: CDPClient | None = None
        self._tabs: dict[str, _TabState] = {}
        self._owned: set[str] = set()
        self._active_target_id = ""
        self._frame_sessions: dict[str, str] = {}
        """OOPIF frame_id -> session_id, keyed by target id per the plan's `frameId/targetId` guidance."""
        self._frame_parents: dict[str, str] = {}
        self._artifacts: list[Artifact] = []
        self._dialogs: dict[str, Dialog] = {}
        """Pending JS dialog per tab session id; cleared once handled."""
        self._dialog_opened = asyncio.Event()
        self._background: set[asyncio.Task[None]] = set()

    @property
    def client(self) -> CDPClient:
        if self._client is None:
            raise RuntimeError("BrowserSession is not open")
        return self._client

    @property
    def active_target_id(self) -> str:
        return self._active_target_id

    @property
    def active_session_id(self) -> str:
        return self._tabs[self._active_target_id].session_id

    def frame_sessions(self) -> dict[str, str]:
        """Only frame sessions descended from the active tab may contribute observations."""

        def belongs(session_id: str) -> bool:
            seen: set[str] = set()
            while session_id in self._frame_parents and session_id not in seen:
                seen.add(session_id)
                session_id = self._frame_parents[session_id]
            return session_id == self.active_session_id

        return {fid: sid for fid, sid in self._frame_sessions.items() if belongs(sid)}

    @property
    def artifacts(self) -> tuple[Artifact, ...]:
        return tuple(self._artifacts)

    def frame_parent_session(self, session_id: str) -> str | None:
        return self._frame_parents.get(session_id)

    def tabs(self) -> tuple[Tab, ...]:
        return tuple(
            Tab(
                id=t.target_id,
                url=t.url,
                title=t.title,
                active=t.target_id == self._active_target_id,
                opener_id=t.opener_id,
            )
            for t in self._tabs.values()
        )

    def set_tab_info(self, target_id: str, url: str, title: str) -> None:
        if target_id in self._tabs:
            self._tabs[target_id].url = url
            self._tabs[target_id].title = title

    def pending_dialog(self) -> Dialog | None:
        return next(
            (
                self._dialogs[sid]
                for sid in (self.active_session_id, *self.frame_sessions().values())
                if sid in self._dialogs
            ),
            None,
        )

    async def wait_for_dialog(self) -> None:
        while True:
            self._dialog_opened.clear()
            if self.pending_dialog() is not None:
                return
            await self._dialog_opened.wait()

    async def __aenter__(self) -> Self:
        self._client = CDPClient(self._connection.cdp_url)
        await self._client.start()
        self._register_events()
        await self.client.send.Target.setDiscoverTargets(params={"discover": True})
        await self._open_owned_tab("about:blank")
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        for task in list(self._background):
            task.cancel()
        for target_id in list(self._owned):
            with contextlib.suppress(Exception):  # best-effort teardown; the browser may already be gone
                await self.client.send.Target.closeTarget(params={"targetId": target_id})
        if self._client is not None:
            await self._client.stop()
            self._client = None

    async def switch_tab(self, target_id: str) -> None:
        if target_id not in self._tabs:
            raise ValueError(f"Unknown tab {target_id}")
        self._active_target_id = target_id
        await self.client.send.Target.activateTarget(params={"targetId": target_id})

    async def _open_owned_tab(self, url: str) -> str:
        remote = self._connection.remote
        # A remote browser has no user tab to protect and its background targets do not render (menus
        # never open, screenshots hang, every click reads as covered), so foreground it.
        created = await self.client.send.Target.createTarget(params={"url": url, "background": not remote})
        target_id = created["targetId"]
        attach = await self.client.send.Target.attachToTarget(params={"targetId": target_id, "flatten": True})
        session_id = attach["sessionId"]
        if remote:
            await self.client.send.Target.activateTarget(params={"targetId": target_id})
        await self._prepare_session(session_id)
        self._tabs[target_id] = _TabState(target_id=target_id, session_id=session_id, url=url)
        self._owned.add(target_id)
        self._active_target_id = target_id
        return target_id

    async def _prepare_session(self, session_id: str) -> None:
        # These domains are independent, but all must be ready before the session can be used.
        async with asyncio.TaskGroup() as tasks:
            for domain in _ENABLE_DOMAINS:
                tasks.create_task(self.client.send_raw(f"{domain}.enable", session_id=session_id))
            tasks.create_task(
                self.client.send_raw(
                    "Target.setAutoAttach",
                    {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
                    session_id=session_id,
                )
            )
            tasks.create_task(
                self.client.send.Fetch.enable(params={"patterns": list(DOWNLOAD_PATTERNS)}, session_id=session_id)
            )

    def _register_events(self) -> None:
        client = self.client
        client.register.Target.attachedToTarget(self._on_attached)
        client.register.Target.detachedFromTarget(self._on_detached)
        client.register.Target.targetCreated(self._on_target_created)
        client.register.Target.targetInfoChanged(self._on_target_info_changed)
        client.register.Target.targetDestroyed(self._on_target_destroyed)
        client.register.Page.javascriptDialogOpening(self._on_dialog)
        client.register.Fetch.requestPaused(self._on_request_paused)

    def _spawn(self, coro: Coroutine[None, None, None]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # -- Target/frame tracking -------------------------------------------------------------------------

    def _on_attached(self, event: AttachedToTargetEvent, session_id: str | None) -> None:
        info = event["targetInfo"]
        if info["type"] == "iframe" and session_id is not None:
            self._frame_sessions[info["targetId"]] = event["sessionId"]
            self._frame_parents[event["sessionId"]] = session_id
            self._spawn(self._enable_frame_domains(event["sessionId"]))

    async def _enable_frame_domains(self, session_id: str) -> None:
        await self._prepare_session(session_id)

    def _on_detached(self, event: DetachedFromTargetEvent, session_id: str | None) -> None:
        self._frame_parents.pop(event["sessionId"], None)
        for frame_id in [fid for fid, sid in self._frame_sessions.items() if sid == event["sessionId"]]:
            del self._frame_sessions[frame_id]

    def _on_target_created(self, event: TargetCreatedEvent, session_id: str | None) -> None:
        info = event["targetInfo"]
        opener_id = info.get("openerId")
        if info["type"] == "page" and opener_id in self._owned:
            self._spawn(self._adopt_popup(info["targetId"], opener_id))

    async def _adopt_popup(self, target_id: str, opener_id: str) -> None:
        attach = await self.client.send.Target.attachToTarget(params={"targetId": target_id, "flatten": True})
        session_id = attach["sessionId"]
        if self._connection.remote:
            await self.client.send.Target.activateTarget(params={"targetId": target_id})
        await self._prepare_session(session_id)
        self._tabs[target_id] = _TabState(target_id=target_id, session_id=session_id, opener_id=opener_id)
        self._owned.add(target_id)
        # The popup may already have navigated to its final URL before this coroutine got scheduled
        # (targetCreated -> targetInfoChanged can both fire while we're still awaiting attachToTarget
        # above), so a targetInfoChanged event carrying it can arrive and be dropped because the tab
        # wasn't in self._tabs yet. Fetch current info now rather than relying on a future event.
        info = await self.client.send.Target.getTargetInfo(params={"targetId": target_id})
        self.set_tab_info(target_id, info["targetInfo"]["url"], info["targetInfo"]["title"])

    def _on_target_info_changed(self, event: TargetInfoChangedEvent, session_id: str | None) -> None:
        info = event["targetInfo"]
        if info["targetId"] in self._tabs:
            self.set_tab_info(info["targetId"], info["url"], info["title"])

    def _on_target_destroyed(self, event: TargetDestroyedEvent, session_id: str | None) -> None:
        target_id = event["targetId"]
        self._tabs.pop(target_id, None)
        self._owned.discard(target_id)
        if target_id == self._active_target_id and self._tabs:
            self._active_target_id = next(iter(self._tabs))

    # -- Dialogs ------------------------------------------------------------------------------------

    def _on_dialog(self, event: JavascriptDialogOpeningEvent, session_id: str | None) -> None:
        if session_id is None:
            return
        self._dialogs[session_id] = Dialog(
            kind=event["type"], message=event["message"], default_prompt=event.get("defaultPrompt")
        )
        self._dialog_opened.set()

    async def handle_dialog(self, accept: bool, prompt_text: str | None = None) -> None:
        session_id = next(
            sid for sid in (self.active_session_id, *self.frame_sessions().values()) if sid in self._dialogs
        )
        params: dict[str, bool | str] = {"accept": accept}
        if prompt_text is not None:
            params["promptText"] = prompt_text
        await self.client.send.Page.handleJavaScriptDialog(params=params, session_id=session_id)  # type: ignore[arg-type]
        self._dialogs.pop(session_id, None)

    # -- Downloads ------------------------------------------------------------------------------------

    def _on_request_paused(self, event: RequestPausedEvent, session_id: str | None) -> None:
        if session_id is not None:
            self._spawn(self._handle_paused(event, session_id))

    async def _handle_paused(self, event: RequestPausedEvent, session_id: str) -> None:
        request_id = event["requestId"]
        try:
            headers = {h["name"].lower(): h["value"] for h in event.get("responseHeaders", [])}
            disposition = headers.get("content-disposition", "")
            if "attachment" in disposition.lower():
                await self._capture_download(request_id, session_id, event["request"]["url"], disposition)
        finally:
            # Never leave a request paused, even if capture above raised. The request/session may
            # already be gone (navigation, tab close), which is fine to swallow here.
            with contextlib.suppress(Exception):
                await self.client.send.Fetch.continueRequest(params={"requestId": request_id}, session_id=session_id)

    async def _capture_download(self, request_id: str, session_id: str, url: str, disposition: str) -> None:
        body = await self.client.send.Fetch.getResponseBody(params={"requestId": request_id}, session_id=session_id)
        raw = base64.b64decode(body["body"]) if body["base64Encoded"] else body["body"].encode()
        if len(raw) > self._max_download_bytes:
            return  # bounded size: refuse to hold an oversized body in memory as an artifact
        name = _filename_from_disposition(disposition) or url.rsplit("/", 1)[-1] or "download"
        artifact = await self._artifact_sink.put(ArtifactKind.DOWNLOAD, name, "application/octet-stream", raw)
        self._artifacts.append(artifact)


def _filename_from_disposition(disposition: str) -> str | None:
    for part in disposition.split(";"):
        part = part.strip()
        if part.lower().startswith("filename="):
            return part.split("=", 1)[1].strip('"')
    return None
