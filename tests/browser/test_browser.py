"""End-to-end tests of the browser layer against a real headless Chrome and fixture sites."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from fastbrowse.browser.page import CdpPage
from fastbrowse.browser.session import BrowserSession
from fastbrowse.models import Attachment, Operation, StepOutcome
from fastbrowse.page import Action, Control, Observation
from tests.browser.conftest import RecordingArtifactSink

pytestmark = pytest.mark.asyncio


def find(observation: Observation, label: str) -> Control:
    for control in observation.controls:
        if label in control.label:
            return control
    raise AssertionError(f"no control labelled like {label!r} in {[c.label for c in observation.controls]}")


async def wait_until(predicate: Callable[[], Awaitable[bool] | bool], timeout_seconds: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        result = predicate()
        if await result if inspect.isawaitable(result) else result:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition was not met in time")


async def eval_value(session: BrowserSession, session_id: str, expression: str) -> Any:
    out = await session.client.send.Runtime.evaluate(
        params={"expression": expression, "returnByValue": True}, session_id=session_id
    )
    return out["result"].get("value")


@pytest.fixture
async def loaded_page(page: CdpPage, main_site: str) -> CdpPage:
    await page.navigate(f"{main_site}/")
    return page


async def test_observe_lists_controls_and_masks_password(loaded_page: CdpPage) -> None:
    obs = await loaded_page.observe()
    sign_in = find(obs, "Sign in")
    assert Operation.CLICK in sign_in.operations

    password = find(obs, "Password")
    assert password.sensitive is True
    fill = await loaded_page.act(Action(operation=Operation.FILL, target_id=password.id, text="s3cr3t"), obs)
    assert fill.outcome == StepOutcome.EXECUTED

    obs2 = await loaded_page.observe()
    password2 = find(obs2, "Password")
    assert password2.value == "•" * len("s3cr3t")


async def test_secret_typed_into_text_field_is_masked_but_submitted(
    loaded_page: CdpPage, browser_session: BrowserSession
) -> None:
    obs = await loaded_page.observe()
    username = find(obs, "Username")
    action = Action(
        operation=Operation.FILL,
        target_id=username.id,
        text="tok-123",
        secret=True,
        secret_origin=username.frame_origin,
    )
    assert (await loaded_page.act(action, obs)).outcome == StepOutcome.EXECUTED

    field = find(await loaded_page.observe(), "Username")
    assert field.sensitive is True and field.value == "•" * len("tok-123")
    session_id = browser_session.active_session_id
    assert await eval_value(browser_session, session_id, "document.getElementById('username').value") == "tok-123"


async def test_fill_and_click_submits_form_and_page_changed(loaded_page: CdpPage) -> None:
    obs = await loaded_page.observe()
    username = find(obs, "Username")
    result = await loaded_page.act(Action(operation=Operation.FILL, target_id=username.id, text="alice"), obs)
    assert result.outcome == StepOutcome.EXECUTED

    obs2 = await loaded_page.observe()
    submit = find(obs2, "Sign in")
    result2 = await loaded_page.act(Action(operation=Operation.CLICK, target_id=submit.id), obs2)
    assert result2.outcome == StepOutcome.EXECUTED
    assert result2.page_changed is True


async def test_covered_click_dispatches_nothing(loaded_page: CdpPage, browser_session: BrowserSession) -> None:
    obs = await loaded_page.observe()
    open_modal = find(obs, "Open modal")
    opened = await loaded_page.act(Action(operation=Operation.CLICK, target_id=open_modal.id), obs)
    assert opened.outcome == StepOutcome.EXECUTED

    obs2 = await loaded_page.observe()
    covered = find(obs2, "Covered target")
    result = await loaded_page.act(Action(operation=Operation.CLICK, target_id=covered.id), obs2)
    assert result.outcome == StepOutcome.COVERED
    assert result.page_changed is False

    clicked = await eval_value(
        browser_session,
        browser_session.active_session_id,
        "document.getElementById('covered-target').dataset.clicked || null",
    )
    assert clicked is None


async def test_dom_swap_after_observe_makes_act_stale(loaded_page: CdpPage, browser_session: BrowserSession) -> None:
    obs = await loaded_page.observe()
    target = find(obs, "Swap target")

    await eval_value(
        browser_session, browser_session.active_session_id, "document.getElementById('swap-trigger').click(); true"
    )

    result = await loaded_page.act(Action(operation=Operation.CLICK, target_id=target.id), obs)
    assert result.outcome == StepOutcome.STALE
    assert result.page_changed is False


async def test_upload_bytes_match_sha256(loaded_page: CdpPage, browser_session: BrowserSession) -> None:
    obs = await loaded_page.observe()
    file_input = next(c for c in obs.controls if c.input_type == "file")
    content = os.urandom(4096)
    attachment = Attachment(name="payload.bin", mime_type="application/octet-stream", content=content)

    result = await loaded_page.act(
        Action(operation=Operation.UPLOAD, target_id=file_input.id, files=(attachment,)), obs
    )
    assert result.outcome == StepOutcome.EXECUTED

    async def sha_ready() -> bool:
        value = await eval_value(
            browser_session, browser_session.active_session_id, "document.getElementById('upload-sha').textContent"
        )
        return bool(value)

    await wait_until(sha_ready)
    value = await eval_value(
        browser_session, browser_session.active_session_id, "document.getElementById('upload-sha').textContent"
    )
    assert value == hashlib.sha256(content).hexdigest()


async def test_download_becomes_artifact_with_checksum(
    loaded_page: CdpPage, browser_session: BrowserSession, artifact_sink: RecordingArtifactSink
) -> None:
    obs = await loaded_page.observe()
    link = find(obs, "Download attachment")
    result = await loaded_page.act(Action(operation=Operation.CLICK, target_id=link.id), obs)
    assert result.outcome == StepOutcome.EXECUTED

    async def has_artifact() -> bool:
        return len(artifact_sink.artifacts) > 0

    await wait_until(has_artifact)
    artifact = artifact_sink.artifacts[0]
    expected = hashlib.sha256(b"fastbrowse fixture attachment bytes for download checksum test").hexdigest()
    assert artifact.sha256 == expected
    assert browser_session.artifacts == (artifact,)


async def test_cross_origin_iframe_control_is_observed_and_clickable(
    loaded_page: CdpPage, browser_session: BrowserSession
) -> None:
    async def frame_attached() -> bool:
        return len(browser_session.frame_sessions()) > 0

    await wait_until(frame_attached)

    obs = await loaded_page.observe()
    frame_button = next(c for c in obs.controls if "Frame button" in c.label)
    assert frame_button.frame_id is not None

    result = await loaded_page.act(Action(operation=Operation.CLICK, target_id=frame_button.id), obs)
    assert result.outcome == StepOutcome.EXECUTED

    frame_session_id = browser_session.frame_sessions()[frame_button.frame_id]
    value = await eval_value(browser_session, frame_session_id, "document.getElementById('frame-button').textContent")
    assert value == "Clicked"


async def test_popup_becomes_tab_and_switch_tab_works(loaded_page: CdpPage, browser_session: BrowserSession) -> None:
    obs = await loaded_page.observe()
    open_popup = find(obs, "Open popup")
    result = await loaded_page.act(Action(operation=Operation.CLICK, target_id=open_popup.id), obs)
    assert result.outcome == StepOutcome.EXECUTED

    async def popup_loaded() -> bool:
        return any("popup.html" in t.url for t in browser_session.tabs())

    await wait_until(popup_loaded)

    obs2 = await loaded_page.observe()
    popup_tab = next(t for t in obs2.tabs if "popup.html" in t.url)
    switch = await loaded_page.act(Action(operation=Operation.SWITCH_TAB, tab_id=popup_tab.id), obs2)
    assert switch.outcome == StepOutcome.EXECUTED

    obs3 = await loaded_page.observe()
    assert "popup.html" in obs3.url


async def test_confirm_dialog_handled_via_dialog_operation(
    loaded_page: CdpPage, browser_session: BrowserSession
) -> None:
    obs = await loaded_page.observe()
    open_dialog = find(obs, "Confirm")
    result = await loaded_page.act(Action(operation=Operation.CLICK, target_id=open_dialog.id), obs)
    assert result.outcome == StepOutcome.EXECUTED

    async def dialog_pending() -> bool:
        return browser_session.pending_dialog() is not None

    await wait_until(dialog_pending)

    obs2 = await loaded_page.observe()
    assert obs2.dialog is not None
    assert obs2.dialog.kind == "confirm"

    handled = await loaded_page.act(Action(operation=Operation.DIALOG, accept_dialog=True), obs2)
    assert handled.outcome == StepOutcome.EXECUTED

    value = await eval_value(
        browser_session, browser_session.active_session_id, "document.getElementById('dialog-result').textContent"
    )
    assert value == "confirmed"


async def test_shadow_dom_button_is_clickable(loaded_page: CdpPage, browser_session: BrowserSession) -> None:
    obs = await loaded_page.observe()
    shadow_button = find(obs, "Shadow button")
    result = await loaded_page.act(Action(operation=Operation.CLICK, target_id=shadow_button.id), obs)
    assert result.outcome == StepOutcome.EXECUTED

    value = await eval_value(browser_session, browser_session.active_session_id, "window.__shadowClicked === true")
    assert value is True


async def test_capture_offsets_slice_exactly_to_each_block(loaded_page: CdpPage) -> None:
    capture = await loaded_page.capture()
    assert capture.blocks, "expected at least one block"
    for block in capture.blocks:
        slice_text = capture.text[block.start : block.end]
        assert slice_text, f"block {block.source_id} sliced to empty text"
    headings = [b for b in capture.blocks if b.kind.value == "heading"]
    assert any(capture.text[h.start : h.end] == "Table" for h in headings)
    tables = [b for b in capture.blocks if b.kind.value == "table"]
    assert tables
    assert "Ada" in capture.text[tables[0].start : tables[0].end]
    links = [b for b in capture.blocks if b.kind.value == "link"]
    assert any(link.href == "https://example.com/docs" for link in links)


async def test_fill_succeeds_when_the_field_is_replaced_while_typing(loaded_page: CdpPage) -> None:
    """Wikipedia's search box swaps itself for a hydrated copy mid-insertion; the text still landed."""
    obs = await loaded_page.observe()
    field = find(obs, "Hydrating field")
    result = await loaded_page.act(Action(operation=Operation.FILL, target_id=field.id, text="godel"), obs)
    assert result.outcome == StepOutcome.EXECUTED

    obs2 = await loaded_page.observe()
    assert find(obs2, "Hydrating field").value == "godel"
