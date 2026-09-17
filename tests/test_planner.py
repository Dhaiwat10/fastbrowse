from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from fastbrowse.llm import Generation, Message
from fastbrowse.models import (
    CostBasis,
    CostComponent,
    CostLine,
    LLMPurpose,
    Operation,
)
from fastbrowse.page import Control, Observation
from fastbrowse.planner import Plan, Requirement, RequirementKind, Subgoal, make_plan
from fastbrowse.telemetry import Ledger


def example_plan() -> Plan:
    return Plan(
        requirements=(Requirement(id="r1", text="Find the price", kind=RequirementKind.INFORMATION),),
        subgoals=(
            Subgoal(id="s1", text="Read product", postcondition="A quoted price is recorded", requirement_ids=("r1",)),
        ),
        answer_expected=True,
    )


class PlannerLLM:
    def __init__(self, plan: Plan | None = None) -> None:
        self.calls: list[tuple[LLMPurpose, tuple[Message, ...]]] = []
        self.plan = plan if plan is not None else example_plan()
        self.cost = CostLine(
            component=CostComponent.LLM, basis=CostBasis.METERED, dollars=0.01, purpose=LLMPurpose.PLAN
        )

    async def generate[T: BaseModel](
        self,
        purpose: LLMPurpose,
        messages: Sequence[Message],
        schema: type[T],
        *,
        max_output_tokens: int = 2000,
        ledger: Ledger | None = None,
    ) -> Generation[T]:
        # Reserve exactly as the real client does, so a test can see a budget stop a request.
        if ledger is not None:
            ledger.reserve(CostComponent.LLM)
        self.calls.append((purpose, tuple(messages)))
        return Generation(data=schema.model_validate_json(self.plan.model_dump_json()), cost=self.cost)


def observation() -> Observation:
    return Observation(
        url="https://shop.test",
        title="Shop",
        page_key="key",
        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
        controls=(
            Control(
                id="pw",
                frame_id=None,
                role="textbox",
                label="password_name",
                operations=frozenset({Operation.FILL}),
                sensitive=True,
                value="SECRET_VALUE",
            ),
        ),
        omitted_controls=0,
        viewport_text="Product page",
        tabs=(),
    )


async def test_planning_uses_redacted_markdown_context_and_preserves_cost() -> None:
    llm = PlannerLLM()
    result = await make_plan(llm, "Find the price", observation())
    purpose, messages = llm.calls[0]
    prompt = "\n".join(message.content for message in messages)
    assert purpose is LLMPurpose.PLAN
    assert result.data == example_plan() and result.cost == llm.cost
    assert "# Task" in prompt and "# Observation" in prompt
    assert "SECRET_VALUE" not in prompt and "password_name" in prompt
    assert "individually checkable" in prompt
    assert observation().controls[0].value == "SECRET_VALUE"


@pytest.mark.parametrize("reference", ["unknown", ""])
def test_plan_rejects_dangling_requirement_links(reference: str) -> None:
    with pytest.raises(ValidationError):
        Plan(
            requirements=example_plan().requirements,
            subgoals=(Subgoal(id="s1", text="Read", postcondition="Read", requirement_ids=(reference,)),),
            answer_expected=True,
        )


def test_plan_rejects_duplicate_ids() -> None:
    with pytest.raises(ValidationError, match="unique"):
        Plan(requirements=example_plan().requirements * 2, subgoals=example_plan().subgoals, answer_expected=True)
