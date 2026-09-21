"""Pointer handlers can invalidate a target before its press or replace an editor before typing."""

import asyncio
from collections.abc import Coroutine
from unittest.mock import AsyncMock

import pytest

from fastbrowse.agent import Agent
from fastbrowse.browser import BrowserSession, CdpPage
from fastbrowse.browser import page as page_module
from fastbrowse.models import Operation, StepOutcome
from fastbrowse.page import Action, BrowserError
from tests.browser.test_browser import eval_value, find, observe_until, wait_until
from tests.test_policy import ScriptedJev
from tests.test_retrieval import ScriptedLLM


@pytest.mark.parametrize("mode", ["move", "animate", "cover", "relabel"])
async def test_pointer_entry_rechecks_the_target_before_pressing(
    page: CdpPage, browser_session: BrowserSession, main_site: str, mode: str
) -> None:
    await page.navigate(f"{main_site}/dispatch.html?{mode}")
    obs = await page.observe()
    target = find(obs, "One way")
    result = await page.act(Action(operation=Operation.CLICK, target_id=target.id), obs)
    presses, clicks, selected = await eval_value(
        browser_session,
        browser_session.active_session_id,
        "[presses, clicks, document.getElementById('target').getAttribute('aria-selected')]",
    )
    if mode in {"move", "animate"}:
        assert result.outcome is StepOutcome.EXECUTED
        assert presses == clicks == ["target"]
        assert selected == "true"
        assert await eval_value(browser_session, browser_session.active_session_id, "pressPositions") == [400]
    else:
        assert result.outcome is (StepOutcome.COVERED if mode == "cover" else StepOutcome.STALE)
        assert presses == clicks == []
        assert selected == "false"


@pytest.mark.parametrize("mode", ["replace", "replace-late", "replace-decoy"])
@pytest.mark.parametrize("framed", [False, True])
async def test_fill_follows_a_replacement_only_at_the_original_position(
    page: CdpPage, browser_session: BrowserSession, main_site: str, mode: str, framed: bool
) -> None:
    await page.navigate(f"{main_site}/dispatch.html?{mode}")
    if framed:
        await eval_value(
            browser_session,
            browser_session.active_session_id,
            f"document.body.innerHTML = '<iframe width=700 height=300 src=\"/dispatch.html?{mode}\"></iframe>'",
        )
    obs = await observe_until(page, "Origin")
    result = await page.act(Action(operation=Operation.FILL, target_id=find(obs, "Origin").id, text="London"), obs)
    assert result.outcome is (StepOutcome.FAILED if mode == "replace-decoy" else StepOutcome.EXECUTED)
    assert find(await page.observe(), "Origin").value == ("" if mode == "replace-decoy" else "London")
    view = "document.querySelector('iframe').contentWindow" if framed else "window"
    assert await eval_value(browser_session, browser_session.active_session_id, f"{view}.clicks") == ["target"]


async def test_secret_fill_does_not_follow_a_replacement(
    page: CdpPage, browser_session: BrowserSession, main_site: str
) -> None:
    await page.navigate(f"{main_site}/dispatch.html?replace")
    obs = await page.observe()
    result = await page.act(
        Action(
            operation=Operation.FILL,
            target_id=find(obs, "Origin").id,
            text="secret-value",
            secret=True,
            secret_origin=main_site,
        ),
        obs,
    )
    assert result.outcome is StepOutcome.FAILED
    assert find(await page.observe(), "Origin").value == ""
    assert await eval_value(browser_session, browser_session.active_session_id, "clicks") == ["target"]


@pytest.mark.parametrize("change", ["clone", "reload", "scope"])
async def test_retargeting_requires_the_receiving_document_and_guard_to_survive(
    page: CdpPage, browser_session: BrowserSession, main_site: str, change: str
) -> None:
    await page.navigate(f"{main_site}/dispatch.html")
    await eval_value(
        browser_session,
        browser_session.active_session_id,
        "document.body.innerHTML = '<iframe width=700 height=300 src=\"/dispatch.html\"></iframe>'",
    )
    before = await observe_until(page, "One way")
    target = find(before, "One way")
    await eval_value(
        browser_session,
        browser_session.active_session_id,
        "const frame = document.querySelector('iframe'); "
        + (
            "frame.contentWindow.location.reload();"
            if change == "reload"
            else "const e = frame.contentDocument.getElementById('target'); e.replaceWith(e.cloneNode(true));"
            + (
                "frame.contentDocument.getElementById('decoy').textContent = 'Changed context';"
                if change == "scope"
                else ""
            )
        ),
    )
    if change == "reload":
        # The frame may still expose the old document on the tick reload() returns.
        async def reloaded() -> bool:
            return any(
                c.label == "One way" and c.retarget_key != target.retarget_key for c in (await page.observe()).controls
            )

        await wait_until(reloaded)
    result = await Agent(page, ScriptedJev({}), ScriptedLLM([]))._act_on_twin(
        Action(operation=Operation.CLICK, target_id=target.id), before, target
    )
    if change == "clone":
        assert result is not None and result.outcome is StepOutcome.EXECUTED
    else:
        assert result is None
    assert await eval_value(
        browser_session, browser_session.active_session_id, "document.querySelector('iframe').contentWindow.presses"
    ) == (["target"] if change == "clone" else [])


async def test_uncertain_press_is_never_replayed(
    page: CdpPage, browser_session: BrowserSession, main_site: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await page.navigate(f"{main_site}/dispatch.html")
    obs = await page.observe()
    original = page._input
    sends = 0

    async def lose_press_response(send: Coroutine[None, None, object]) -> None:
        nonlocal sends
        await original(send)
        sends += 1
        if sends == 2:
            raise BrowserError("press response lost")

    monkeypatch.setattr(page, "_input", lose_press_response)
    with pytest.raises(BrowserError, match="press response lost"):
        await page.act(Action(operation=Operation.CLICK, target_id=find(obs, "One way").id), obs)
    assert await eval_value(browser_session, browser_session.active_session_id, "[presses, clicks]") == [["target"], []]


@pytest.mark.parametrize("deferred", [False, True])
async def test_pointer_entry_dialog_does_not_block_a_fresh_hit_test(
    page: CdpPage, browser_session: BrowserSession, main_site: str, deferred: bool
) -> None:
    await page.navigate(f"{main_site}/dispatch.html")
    await eval_value(
        browser_session,
        browser_session.active_session_id,
        "document.getElementById('target').onpointerenter = () => "
        + ("requestAnimationFrame(() => confirm('Continue?'))" if deferred else "confirm('Continue?')"),
    )
    obs = await page.observe()
    # The bound catches a click that hangs behind the dialog; a busy CI runner took over 2s to get through it.
    async with asyncio.timeout(10):
        result = await page.act(Action(operation=Operation.CLICK, target_id=find(obs, "One way").id), obs)
    assert result.outcome is StepOutcome.FAILED
    assert browser_session.pending_dialog() is not None
    await browser_session.handle_dialog(False)
    assert await eval_value(browser_session, browser_session.active_session_id, "presses") == []


async def test_an_unstable_target_expires_without_a_press(page: CdpPage, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(page_module, "_TARGET_STABILITY_SECONDS", 0)
    monkeypatch.setattr(page, "_move", AsyncMock())
    monkeypatch.setattr(page, "_evaluate", AsyncMock())
    monkeypatch.setattr(page, "_before_action", AsyncMock(return_value=("fingerprint", ["guard"], (20.0, 20.0))))
    pressed = AsyncMock()
    monkeypatch.setattr(page, "_input", pressed)
    outcome, _ = await page._click_point(("session", "main", 1, ["guard"]), (10.0, 10.0))
    assert outcome is StepOutcome.STALE
    pressed.assert_not_called()
