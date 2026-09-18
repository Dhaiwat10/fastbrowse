"""Field context and recovery state regressions, with model outputs scripted."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

from fastbrowse.agent import Agent, _RunState, _Stop, _unread  # pyright: ignore[reportPrivateUsage]
from fastbrowse.config import Config
from fastbrowse.llm import Generation
from fastbrowse.memory import Notes
from fastbrowse.models import Authorization, Limits, LLMPurpose, Operation, Status, StepOutcome
from fastbrowse.page import ActResult, Control, Page
from fastbrowse.planner import Plan, Requirement, RequirementKind
from fastbrowse.policy import HistoryEntry, decide
from fastbrowse.telemetry import Ledger
from tests.test_policy import FREE, ScriptedJev, context, observation
from tests.test_retrieval import ScriptedLLM


async def run_state() -> _RunState:
    plan = Plan(requirements=(), answer_expected=False)

    async def planned() -> Generation[Plan]:
        return Generation(data=plan, cost=FREE)

    planning = asyncio.create_task(planned())
    await planning
    return _RunState(
        task="Find a train from Bristol to York on 16 October 2026",
        inputs={},
        attachments=(),
        authorization=Authorization(),
        ledger=Ledger(Limits()),
        planning=planning,
        ready_plan=plan,
    )


def field(label: str = "Search elsewhere") -> Control:
    return Control(
        id="field",
        frame_id=None,
        role="combobox",
        label=label,
        value="Bath",
        operations=frozenset({Operation.FILL, Operation.CLICK, Operation.ENTER}),
    )


async def test_field_writer_receives_popup_context_and_other_field_values() -> None:
    target = field()
    other = field("Destination").model_copy(update={"id": "destination", "value": "York"})
    obs = observation((target, other)).model_copy(update={"viewport_text": "Choose a station"})
    state = await run_state()
    state.hint = "Replace the origin"
    state.history.append(
        HistoryEntry(operation=Operation.CLICK, target="Origin", outcome=StepOutcome.EXECUTED, page_changed=True)
    )
    llm = ScriptedLLM([{"missing": False, "text": "Bristol"}])
    agent = Agent(Mock(spec=Page), ScriptedJev({}), llm)

    assert await agent._generate_text(state, obs, target) == "Bristol"  # pyright: ignore[reportPrivateUsage]
    purpose, messages = llm.calls[0]
    prompt = json.loads(messages[1].content)
    assert purpose is LLMPurpose.FIELD_TEXT
    assert prompt["field"]["value"] == "Bath"
    assert prompt["other_fields"][0]["value"] == "York"
    assert prompt["recent_actions"][0]["target"] == "Origin"
    assert prompt["subgoal"] == "Replace the origin"
    assert prompt["page"]["text"] == "Choose a station"


async def test_missing_personal_information_still_stops_without_filling() -> None:
    target = field("Account number")
    page = Mock(spec=Page)
    agent = Agent(page, ScriptedJev({}, noul=0.1), ScriptedLLM([{"missing": True, "text": ""}]))
    with pytest.raises(_Stop) as stopped:
        await agent._generate_text(await run_state(), observation((target,)), target)  # pyright: ignore[reportPrivateUsage]
    assert stopped.value.status is Status.NEEDS_INPUT
    page.act.assert_not_called()


async def test_a_value_the_task_states_is_asked_for_again_rather_than_ending_the_run() -> None:
    target = field("Last Name")
    llm = ScriptedLLM([{"missing": True, "text": ""}, {"missing": False, "text": "Lovelace"}])
    agent = Agent(Mock(spec=Page), ScriptedJev({}, noul=0.9), llm)

    assert await agent._generate_text(await run_state(), observation((target,)), target) == "Lovelace"  # pyright: ignore[reportPrivateUsage]
    assert "never invent one" in llm.calls[1][1][-1].content


async def test_a_second_missing_verdict_ends_the_run() -> None:
    target = field("Account number")
    llm = ScriptedLLM([{"missing": True, "text": ""}, {"missing": True, "text": ""}])
    agent = Agent(Mock(spec=Page), ScriptedJev({}, noul=0.9), llm)
    with pytest.raises(_Stop) as stopped:
        await agent._generate_text(await run_state(), observation((target,)), target)  # pyright: ignore[reportPrivateUsage]
    assert stopped.value.status is Status.NEEDS_INPUT


@pytest.mark.parametrize("outcome", [StepOutcome.EXECUTED, StepOutcome.STALE])
async def test_recovery_hint_is_consumed_only_when_action_progresses(outcome: StepOutcome) -> None:
    target = field()
    obs = observation((target,))
    state = await run_state()
    state.hint = "Open the origin picker"
    state.authorization = Authorization(irreversible_actions=True)
    page = Mock(spec=Page)
    page.act = AsyncMock(return_value=ActResult(outcome=outcome, page_changed=outcome is StepOutcome.EXECUTED))
    jev = ScriptedJev({"operation": "click", "click_target": target.id})
    decision = await decide(jev, obs, context(), Config())
    agent = Agent(page, jev, ScriptedLLM([]))
    await agent._step(state, obs, decision)  # pyright: ignore[reportPrivateUsage]
    assert state.hint == (None if outcome is StepOutcome.EXECUTED else "Open the origin picker")


async def test_step_log_names_which_twin_was_clicked() -> None:
    twins = tuple(
        Control(
            id=f"add{i}",
            frame_id=None,
            role="button",
            label="Add to cart",
            context=name,
            operations=frozenset({Operation.CLICK}),
        )
        for i, name in enumerate(("Sauce Labs Backpack", "Sauce Labs Bike Light"))
    )
    obs = observation(twins)
    page = Mock(spec=Page)
    page.act = AsyncMock(return_value=ActResult(outcome=StepOutcome.EXECUTED, page_changed=True))
    jev = ScriptedJev({"operation": "click", "click_target": "add1"})
    decision = await decide(jev, obs, context(), Config())
    state = await run_state()
    state.authorization = Authorization(irreversible_actions=True)
    agent = Agent(page, jev, ScriptedLLM([]))
    await agent._step(state, obs, decision)  # pyright: ignore[reportPrivateUsage]
    assert state.steps[0].target == "Add to cart (Sauce Labs Bike Light)"
    assert state.history[0].target == "Add to cart (Sauce Labs Bike Light)"


async def test_going_round_between_pages_stops_counting_as_progress() -> None:
    link = Control(id="about", frame_id=None, role="link", label="(about)", operations=frozenset({Operation.CLICK}))
    obs = observation((link,))
    page = Mock(spec=Page)
    page.act = AsyncMock(return_value=ActResult(outcome=StepOutcome.EXECUTED, page_changed=True))
    jev = ScriptedJev({"operation": "click", "click_target": "about"})
    decision = await decide(jev, obs, context(), Config())
    state = await run_state()
    state.authorization = Authorization(irreversible_actions=True)
    agent = Agent(page, jev, ScriptedLLM([]))
    for _ in range(2):
        await agent._step(state, obs, decision)  # pyright: ignore[reportPrivateUsage]
    assert state.unchanged == 0
    # The page changes every time, but the same click from the same page a third time is a cycle.
    await agent._step(state, obs, decision)  # pyright: ignore[reportPrivateUsage]
    assert state.unchanged == 1


def test_an_answer_owed_with_nothing_read_is_unread() -> None:
    action_only = Plan(
        requirements=(Requirement(id="r1", text="Search for the quote", kind=RequirementKind.ACTION),),
        answer_expected=True,
    )
    assert _unread(action_only, Notes())
    assert not _unread(action_only.model_copy(update={"answer_expected": False}), Notes())
