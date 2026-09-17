"""`CdpPage`: implements the `Page` protocol on a `BrowserSession`.

Freshness (page_key + per-element guard), hit-test-before-input (covered detection), scroll-into-view
for offscreen targets, password masking and the offscreen-nearest cap are ported from jev-ultrafast
(MIT): jev_ultrafast/browser.py + jev_ultrafast/snapshot.js. `act` dispatches at most once per call.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, assert_never, cast

from cdp_use.cdp.input.commands import DispatchMouseEventParameters
from cdp_use.cdp.page.commands import CaptureScreenshotParameters

from fastbrowse.browser.session import BrowserSession
from fastbrowse.config import Config
from fastbrowse.models import Attachment, Operation, StepOutcome
from fastbrowse.page import Action, ActResult, Block, BlockKind, Capture, Control, Dialog, Observation, Page

_SNAPSHOT_JS = (Path(__file__).with_name("snapshot.js")).read_text()
_CAPTURE_JS = (Path(__file__).with_name("capture.js")).read_text()

_BLOCK_KIND = {
    "heading": BlockKind.HEADING,
    "paragraph": BlockKind.PARAGRAPH,
    "list_item": BlockKind.LIST_ITEM,
    "table": BlockKind.TABLE,
    "code": BlockKind.CODE,
    "link": BlockKind.LINK,
}

_SETTLE_SECONDS = 5.0
_SETTLE_POLL_SECONDS = 0.1
_SCREENSHOT_WAIT_SECONDS = 1.0

_MAIN = "main"
"""Frame key used for the top frame; OOPIF frames key on their CDP target id, per the browser session."""


class _FrameObservation:
    """Raw snapshot.js output for one frame, tagged with how to reach it again."""

    __slots__ = ("frame_id", "local_id", "raw", "session_id")

    def __init__(self, frame_id: str | None, session_id: str, local_id: str, raw: dict[str, Any]) -> None:
        self.frame_id = frame_id
        self.session_id = session_id
        self.local_id = local_id
        self.raw = raw


class _ObservedState:
    """What `act` needs to re-find and freshness-check the exact control an `Observation` named."""

    def __init__(self, page_key: str, controls: dict[str, tuple[str, str, int, list[object] | None]]) -> None:
        self.page_key = page_key
        # control_id -> (session_id, frame_key, local_node_id, guard snapshot)
        self.controls = controls


class CdpPage(Page):
    def __init__(self, session: BrowserSession, config: Config) -> None:
        self._session = session
        self._config = config
        self._last: _ObservedState | None = None
        self._blocked_inputs: set[asyncio.Future[object]] = set()

    # -- observe / capture -------------------------------------------------------------------------

    async def observe(self) -> Observation:
        dialog = self._session.pending_dialog()
        if dialog is not None:
            # A JavaScript dialog blocks the renderer, so any page evaluate would hang until it is handled.
            return self._dialog_observation(dialog)
        frames, inaccessible = await self._snapshot_all_frames()
        main = frames.get(_MAIN)
        controls: list[Control] = []
        control_state: dict[str, tuple[str, str, int, list[object] | None]] = {}
        omitted = 0
        for frame_key, frame in frames.items():
            raw = frame.raw
            omitted += int(raw.get("omitted_controls", 0))
            for c in raw["controls"]:
                local_id = int(c["id"])
                control_id = f"{frame_key}:{local_id}"
                fid = None if frame_key == _MAIN else frame_key
                controls.append(_control_from_raw(control_id, fid, c))
                guard = cast("list[object] | None", frame.raw["guards"].get(str(local_id)))
                control_state[control_id] = (frame.session_id, frame_key, local_id, guard)

        limits = self._config.observation
        onscreen = [c for c in controls if not c.offscreen]
        offscreen = [c for c in controls if c.offscreen][: limits.max_offscreen_controls]
        omitted += len(controls) - len(onscreen) - len(offscreen)
        kept = (onscreen + offscreen)[: limits.max_controls]
        omitted += max(0, len(onscreen) + len(offscreen) - limits.max_controls)
        control_state = {c.id: control_state[c.id] for c in kept}

        page_key = _combine_page_keys([f.raw["page_key"] for f in frames.values()])
        self._last = _ObservedState(page_key=page_key, controls=control_state)

        title = main.raw["title"] if main else ""
        url = main.raw["url"] if main else await self.origin()
        viewport_text = (main.raw["viewport_text"] if main else "")[: limits.viewport_text_chars]
        return Observation(
            url=url,
            title=title,
            page_key=page_key,
            captured_at=datetime.now(UTC),
            controls=tuple(kept),
            omitted_controls=omitted,
            viewport_text=viewport_text,
            tabs=self._session.tabs(),
            dialog=self._session.pending_dialog(),
            inaccessible_frames=inaccessible,
        )

    def _dialog_observation(self, dialog: Dialog) -> Observation:
        active = next((t for t in self._session.tabs() if t.active), None)
        self._last = _ObservedState(page_key=f"dialog:{dialog.kind}", controls={})
        return Observation(
            url=active.url if active else "",
            title=active.title if active else "",
            page_key=self._last.page_key,
            captured_at=datetime.now(UTC),
            controls=(),
            omitted_controls=0,
            viewport_text="",
            tabs=self._session.tabs(),
            dialog=dialog,
        )

    async def _snapshot_all_frames(self) -> tuple[dict[str, _FrameObservation], int]:
        result: dict[str, _FrameObservation] = {}
        inaccessible = 0
        main_session = self._session.active_session_id
        try:
            raw = await self._evaluate(main_session, _SNAPSHOT_JS)
        except RuntimeError:
            raw = None
        if raw is not None:
            self._session.set_tab_info(self._session.active_target_id, raw["url"], raw["title"])
            result[_MAIN] = _FrameObservation(None, main_session, _MAIN, raw)
        else:
            inaccessible += 1
        for frame_id, session_id in self._session.frame_sessions().items():
            try:
                raw = await self._evaluate(session_id, _SNAPSHOT_JS)
            except RuntimeError:
                raw = None
            if raw is None:
                inaccessible += 1
                continue
            result[frame_id] = _FrameObservation(frame_id, session_id, frame_id, raw)
        return result, inaccessible

    async def capture(self) -> Capture:
        text_parts: list[str] = []
        blocks: list[Block] = []
        offset = 0
        inaccessible = 0
        main_session = self._session.active_session_id
        frame_sources: list[tuple[str | None, str]] = [(None, main_session)]
        frame_sources += [(fid, sid) for fid, sid in self._session.frame_sessions().items()]
        title = ""
        url = await self.origin()
        for frame_id, session_id in frame_sources:
            try:
                raw = await self._evaluate(session_id, _CAPTURE_JS)
            except RuntimeError:
                raw = None
            if raw is None:
                inaccessible += 1
                continue
            if frame_id is None:
                title, url = raw["title"], raw["url"]
            for block in raw["blocks"]:
                text = str(block["text"])
                start = offset
                text_parts.append(text)
                offset += len(text)
                end = offset
                text_parts.append("\n\n")
                offset += 2
                blocks.append(
                    Block(
                        source_id=f"{frame_id or _MAIN}:{len(blocks)}",
                        kind=_BLOCK_KIND[block["kind"]],
                        frame_id=frame_id,
                        start=start,
                        end=end,
                        heading_path=tuple(block.get("heading_path", ())),
                        href=block.get("href"),
                    )
                )
        text = "".join(text_parts)
        return Capture(
            url=url,
            title=title,
            captured_at=datetime.now(UTC),
            sha256=hashlib.sha256(text.encode()).hexdigest(),
            text=text,
            blocks=tuple(blocks),
            inaccessible_frames=inaccessible,
        )

    # -- act ----------------------------------------------------------------------------------------

    async def act(self, action: Action, observation: Observation) -> ActResult:
        if self._last is None or self._last.page_key != observation.page_key:
            return ActResult(outcome=StepOutcome.STALE, page_changed=False, detail="observation is out of date")
        # A pending dialog already blocks the renderer's main thread; evaluating now would hang.
        before_fingerprint = "" if self._session.pending_dialog() is not None else await self._fingerprint()

        target = None
        if action.target_id is not None:
            target = self._last.controls.get(action.target_id)
            if target is None:
                return ActResult(outcome=StepOutcome.STALE, page_changed=False, detail="unknown control id")
            session_id, _frame_key, local_id, guard = target
            live_guard = await self._live_guard(session_id, local_id)
            if live_guard != guard:
                return ActResult(
                    outcome=StepOutcome.STALE, page_changed=False, detail="control changed since observation"
                )

        outcome, detail = await self._dispatch(action, target)
        if outcome != StepOutcome.EXECUTED:
            return ActResult(outcome=outcome, page_changed=False, detail=detail)
        changed = await self._changed_since(before_fingerprint)
        return ActResult(outcome=StepOutcome.EXECUTED, page_changed=changed, detail=detail)

    async def _dispatch(
        self, action: Action, target: tuple[str, str, int, list[object] | None] | None
    ) -> tuple[StepOutcome, str | None]:
        match action.operation:
            case Operation.CLICK:
                return await self._click(target)
            case Operation.FILL:
                return await self._fill(target, action.text or "", secret=action.secret)
            case Operation.SELECT:
                return await self._select(target, action.text or "")
            case Operation.ENTER:
                return await self._key(target, "Enter")
            case Operation.ESCAPE:
                return await self._key(None, "Escape")
            case Operation.SCROLL:
                await self._scroll(action.scroll_down)
                return StepOutcome.EXECUTED, None
            case Operation.BACK:
                return await self._back()
            case Operation.SWITCH_TAB:
                if action.tab_id is None:
                    return StepOutcome.FAILED, "switch_tab requires tab_id"
                await self._session.switch_tab(action.tab_id)
                return StepOutcome.EXECUTED, None
            case Operation.UPLOAD:
                return await self._upload(target, action.files)
            case Operation.DIALOG:
                if action.accept_dialog is None:
                    return StepOutcome.FAILED, "dialog requires accept_dialog"
                await self._session.handle_dialog(action.accept_dialog, action.text)
                return StepOutcome.EXECUTED, None
            case Operation.READ | Operation.DONE | Operation.ESCALATE:
                raise ValueError(f"{action.operation} does not dispatch through the browser layer")
            case _:
                assert_never(action.operation)

    async def _click(self, target: tuple[str, str, int, list[object] | None] | None) -> tuple[StepOutcome, str | None]:
        if target is None:
            return StepOutcome.FAILED, "click requires a target"
        session_id, _frame, local_id, _guard = target
        point = await self._hit_test(session_id, local_id)
        if point is None:
            return StepOutcome.STALE, "target disconnected"
        if point == "covered":
            return StepOutcome.COVERED, None
        await self._click_point(session_id, point)
        return StepOutcome.EXECUTED, None

    async def _fill(
        self, target: tuple[str, str, int, list[object] | None] | None, text: str, *, secret: bool = False
    ) -> tuple[StepOutcome, str | None]:
        if target is None:
            return StepOutcome.FAILED, "fill requires a target"
        session_id, _frame, local_id, _guard = target
        point = await self._hit_test(session_id, local_id)
        if point is None:
            return StepOutcome.STALE, "target disconnected"
        if point == "covered":
            return StepOutcome.COVERED, None
        if secret:
            # Masked before typing, so no frame renders the value; the snapshot then reports the field sensitive.
            await self._evaluate(
                session_id,
                f"(e => {{ if (e) {{ e.dataset.fastbrowseSecret = '1'; "
                f"e.style.setProperty('-webkit-text-security', 'disc', 'important'); }} }})"
                f"(window.__fastbrowse?.nodes.get({local_id}))",
            )
        await self._click_point(session_id, point)
        await self._session.client.send.Input.dispatchKeyEvent(
            params={"type": "keyDown", "key": "a", "code": "KeyA", "modifiers": 2, "commands": ["selectAll"]},
            session_id=session_id,
        )
        await self._session.client.send.Input.dispatchKeyEvent(
            params={"type": "keyUp", "key": "a", "code": "KeyA", "modifiers": 2}, session_id=session_id
        )
        await self._session.client.send.Input.insertText(params={"text": text}, session_id=session_id)
        return StepOutcome.EXECUTED, None

    async def _select(
        self, target: tuple[str, str, int, list[object] | None] | None, option: str
    ) -> tuple[StepOutcome, str | None]:
        if target is None:
            return StepOutcome.FAILED, "select requires a target"
        session_id, _frame, local_id, _guard = target
        script = (
            "((id, label) => { const e = window.__fastbrowse?.nodes.get(id); "
            "if (!e?.isConnected || e.tagName !== 'SELECT') return null; "
            "const opt = [...e.options].find(o => o.label === label && !o.disabled); "
            "if (!opt) return false; e.value = opt.value; "
            "e.dispatchEvent(new Event('input', {bubbles: true})); "
            "e.dispatchEvent(new Event('change', {bubbles: true})); return true; })"
            f"({local_id}, {json.dumps(option)})"
        )
        result = await self._evaluate(session_id, script)
        if result is None:
            return StepOutcome.STALE, "select target disconnected"
        if result is False:
            return StepOutcome.FAILED, f"no option labelled {option!r}"
        return StepOutcome.EXECUTED, None

    async def _key(
        self, target: tuple[str, str, int, list[object] | None] | None, key: str
    ) -> tuple[StepOutcome, str | None]:
        session_id = target[0] if target is not None else self._session.active_session_id
        if target is not None:
            _session_id, _frame, local_id, _guard = target
            point = await self._hit_test(session_id, local_id)
            if point is None:
                return StepOutcome.STALE, "target disconnected"
            if point == "covered":
                return StepOutcome.COVERED, None
        code, virtual_key = {"Enter": ("\r", 13), "Escape": ("", 27)}[key]
        await self._input(
            self._session.client.send.Input.dispatchKeyEvent(
                params={"type": "keyDown", "key": key, "code": key, "text": code, "windowsVirtualKeyCode": virtual_key},
                session_id=session_id,
            )
        )
        await self._input(
            self._session.client.send.Input.dispatchKeyEvent(
                params={"type": "keyUp", "key": key, "code": key, "windowsVirtualKeyCode": virtual_key},
                session_id=session_id,
            )
        )
        return StepOutcome.EXECUTED, None

    async def _scroll(self, down: bool) -> None:
        session_id = self._session.active_session_id
        await self._session.client.send.Input.dispatchMouseEvent(
            params={"type": "mouseWheel", "x": 550, "y": 650, "deltaX": 0, "deltaY": 560 if down else -560},
            session_id=session_id,
        )

    async def _back(self) -> tuple[StepOutcome, str | None]:
        session_id = self._session.active_session_id
        history = await self._session.client.send.Page.getNavigationHistory(params=None, session_id=session_id)
        index = history["currentIndex"]
        if index <= 0:
            return StepOutcome.FAILED, "no earlier history entry"
        entry_id = history["entries"][index - 1]["id"]
        await self._session.client.send.Page.navigateToHistoryEntry(params={"entryId": entry_id}, session_id=session_id)
        return StepOutcome.EXECUTED, None

    async def _upload(
        self, target: tuple[str, str, int, list[object] | None] | None, files: tuple[Attachment, ...]
    ) -> tuple[StepOutcome, str | None]:
        if target is None:
            return StepOutcome.FAILED, "upload requires a target"
        if not files:
            return StepOutcome.FAILED, "no files supplied"
        total = sum(len(f.content) for f in files)
        if total > self._config.max_upload_bytes:
            return StepOutcome.FAILED, f"{total} bytes exceeds max_upload_bytes ({self._config.max_upload_bytes})"
        session_id, _frame, local_id, _guard = target
        payload = json.dumps(
            [{"name": f.name, "type": f.mime_type, "data": base64.b64encode(f.content).decode()} for f in files]
        )
        script = (
            "((id, files) => { const e = window.__fastbrowse?.nodes.get(id); "
            "if (!e?.isConnected) return null; "
            "const dt = new DataTransfer(); "
            "for (const f of files) { const bin = atob(f.data); "
            "const bytes = Uint8Array.from(bin, c => c.charCodeAt(0)); "
            "dt.items.add(new File([bytes], f.name, {type: f.type})); } "
            "e.files = dt.files; "
            "e.dispatchEvent(new Event('input', {bubbles: true})); "
            "e.dispatchEvent(new Event('change', {bubbles: true})); return true; })"
            f"({local_id}, {payload})"
        )
        result = await self._evaluate(session_id, script)
        if result is None:
            return StepOutcome.STALE, "upload target disconnected"
        return StepOutcome.EXECUTED, None

    async def _click_point(self, session_id: str, point: tuple[float, float]) -> None:
        x, y = point
        for kind in ("mousePressed", "mouseReleased"):
            params: DispatchMouseEventParameters = {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1}
            await self._input(self._session.client.send.Input.dispatchMouseEvent(params=params, session_id=session_id))

    async def _input(self, send: Coroutine[None, None, object]) -> None:
        """Dispatch an input event without waiting on a handler that a JavaScript dialog is blocking.

        CDP answers an input event only after the page's handlers return, and `confirm()` inside a handler
        does not return until the dialog is handled; the event has been delivered either way.
        """
        task = asyncio.ensure_future(send)
        while not task.done():
            if self._session.pending_dialog() is not None:
                self._blocked_inputs.add(task)
                task.add_done_callback(self._blocked_inputs.discard)
                return
            await asyncio.wait({task}, timeout=0.05)
        task.result()

    async def screenshot(self) -> bytes:
        """Capture the active tab, activating it only if a background tab produces no frame to capture.

        An idle background tab composites nothing new, so a capture can wait many seconds; focus emulation and
        compositor-level nudges proved unreliable, while activating always yields a frame at once. Screenshots
        are rare (recovery and uncertain completion), so focus moves only when it has to.
        """
        client, session_id = self._session.client, self._session.active_session_id
        params: CaptureScreenshotParameters = {"format": "jpeg", "quality": 70}
        capture = asyncio.ensure_future(client.send.Page.captureScreenshot(params=params, session_id=session_id))
        done, _ = await asyncio.wait({capture}, timeout=_SCREENSHOT_WAIT_SECONDS)
        if not done:
            await client.send.Target.activateTarget(params={"targetId": self._session.active_target_id})
        return base64.b64decode((await capture)["data"])

    async def origin(self) -> str:
        raw = await self._evaluate(self._session.active_session_id, "location.origin")
        return str(raw) if raw is not None else ""

    async def navigate(self, url: str, load_timeout_seconds: float = 15.0) -> None:
        """Setup helper (tests, initial task URL): navigate the active tab and wait until its document is usable.

        Waiting for `complete` also waits on every image and tracker, which behind a proxy can outlast the page
        becoming interactive; observation settles the rest.
        """
        session_id = self._session.active_session_id
        result = await self._session.client.send.Page.navigate(params={"url": url}, session_id=session_id)
        if error := result.get("errorText"):
            raise RuntimeError(f"navigation to {url} failed: {error}")
        deadline = asyncio.get_event_loop().time() + load_timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            state = await self._evaluate(session_id, "document.readyState")
            if state in {"interactive", "complete"}:
                return
            await asyncio.sleep(0.05)
        raise TimeoutError(f"navigation to {url} did not complete within {load_timeout_seconds}s")

    # -- shared helpers -------------------------------------------------------------------------------

    async def _hit_test(self, session_id: str, local_id: int) -> tuple[float, float] | Literal["covered"] | None:
        script = (
            "(id => { const e = window.__fastbrowse?.nodes.get(id); "
            "if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled=\"true\"],[inert]') || "
            "!e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return null; "
            "let r = e.getBoundingClientRect(); "
            "if (r.y + r.height / 2 < 0 || r.y + r.height / 2 >= innerHeight) { "
            "e.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'}); "
            "r = e.getBoundingClientRect(); } "
            "const x = r.x + r.width / 2, y = r.y + r.height / 2; "
            "if (!r.width || !r.height || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return null; "
            # Descend through open shadow roots: the document-level hit is only the outermost host.
            "let hit = document.elementFromPoint(x, y); "
            "while (hit?.shadowRoot) { const inner = hit.shadowRoot.elementFromPoint(x, y); "
            "if (!inner || inner === hit) break; hit = inner; } "
            "if (!e.contains(hit)) return 'covered'; "
            "return [x, y]; })"
            f"({local_id})"
        )
        result = await self._evaluate(session_id, script)
        if result is None or result == "covered":
            return result
        return float(result[0]), float(result[1])

    async def _live_guard(self, session_id: str, local_id: int) -> list[object] | None:
        script = f"(() => {{ const r = window.__fastbrowse; return r ? r.guard(r.nodes.get({local_id})) : null; }})()"
        return cast("list[object] | None", await self._evaluate(session_id, script))

    async def _fingerprint(self) -> str:
        script = (
            "location.href + '|' + document.title + '|' + "
            "(document.body ? document.body.innerText.length : 0) + '|' + scrollY"
        )
        result = await self._evaluate(self._session.active_session_id, script)
        return str(result)

    async def _changed_since(self, before: str) -> bool:
        """Wait for the page to settle after an action, then report whether it changed.

        A click that navigates returns before the navigation starts, so an immediate fingerprint would describe
        the old page. Settled means the document is loaded and two polls agree. A blocking JavaScript dialog
        freezes the renderer, and an evaluate that fails mid-navigation is itself evidence of change.
        """
        deadline = time.monotonic() + _SETTLE_SECONDS
        previous: str | None = None
        await asyncio.sleep(_SETTLE_POLL_SECONDS)
        while time.monotonic() < deadline:
            if self._session.pending_dialog() is not None:
                return True
            try:
                current = await asyncio.wait_for(self._settled_fingerprint(), timeout=_SETTLE_POLL_SECONDS * 4)
            except (TimeoutError, RuntimeError):
                current = None
            if current is not None and current == previous:
                return current != before
            previous = current
            await asyncio.sleep(_SETTLE_POLL_SECONDS)
        return previous != before

    async def _settled_fingerprint(self) -> str | None:
        """The fingerprint once the document has loaded; None while it is still loading."""
        if await self._evaluate(self._session.active_session_id, "document.readyState") != "complete":
            return None
        return await self._fingerprint()

    async def _evaluate(self, session_id: str, expression: str) -> Any:
        out = await self._session.client.send.Runtime.evaluate(
            params={"expression": expression, "returnByValue": True, "awaitPromise": True}, session_id=session_id
        )
        if "exceptionDetails" in out:
            raise RuntimeError(json.dumps(out["exceptionDetails"])[:300])
        return out["result"].get("value")


def _control_from_raw(control_id: str, frame_id: str | None, c: dict[str, Any]) -> Control:
    return Control(
        id=control_id,
        frame_id=frame_id,
        role=c["role"],
        label=c["label"],
        operations=frozenset(Operation(op) for op in c["operations"]),
        value=c.get("value"),
        href=c.get("href"),
        options=tuple(c.get("options", ())),
        input_type=c.get("input_type"),
        checked=c.get("checked"),
        selected=c.get("selected"),
        expanded=c.get("expanded"),
        sensitive=bool(c.get("sensitive", False)),
        offscreen=bool(c.get("offscreen", False)),
    )


def _combine_page_keys(keys: list[str]) -> str:
    return hashlib.sha256("|".join(sorted(keys)).encode()).hexdigest()
