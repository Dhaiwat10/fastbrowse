from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from pydantic import JsonValue

from fastbrowse.config import Thresholds
from fastbrowse.jev import Answer, Evaluation, NoulAnswer, Question
from fastbrowse.memory import Notes
from fastbrowse.models import CostBasis, CostComponent, CostLine
from fastbrowse.page import Observation
from fastbrowse.planner import Plan, Requirement, RequirementKind
from fastbrowse.verification import DoneVerdict, check_done

_OPEN = Plan(
    requirements=(Requirement(id="r1", text="Open the httpx repository.", kind=RequirementKind.ACTION),),
    answer_expected=False,
)
_PAGE = Observation(
    url="https://example.test/encode/httpx",
    title="encode/httpx",
    page_key="k",
    captured_at=datetime.now(UTC),
    controls=(),
    omitted_controls=0,
    viewport_text="encode/httpx",
    tabs=(),
)


class _Jev:
    def __init__(self, answers: Mapping[str, float]) -> None:
        self.answers = answers

    async def evaluate(self, state: JsonValue, questions: Mapping[str, Question]) -> Evaluation:
        answers: dict[str, Answer] = {k: NoulAnswer(probability=p) for k, p in self.answers.items() if k in questions}
        cost = CostLine(component=CostComponent.JEV, basis=CostBasis.ESTIMATED, dollars=0.0)
        return Evaluation(model="test", answers=answers, input_tokens=1, cost=cost)


# Answers Jev gave live: right pages scored 0.49 to 0.91 complete, near misses 0.04 or less.
@pytest.mark.parametrize(
    ("answers", "verdict"),
    [
        ({"complete": 0.63, "unmet_r1": 0.19}, DoneVerdict.ACCEPT),  # the repository, doubted as a whole
        ({"complete": 0.90, "unmet_r1": 0.14}, DoneVerdict.ACCEPT),
        ({"complete": 0.03, "unmet_r1": 0.85}, DoneVerdict.VERIFY),  # the organisation page
        ({"complete": 0.49, "unmet_r1": 0.66}, DoneVerdict.VERIFY),  # doubt either way goes to the verifier
        ({"complete": 0.72, "unmet_r1": 0.45}, DoneVerdict.VERIFY),
        ({"complete": 0.72}, DoneVerdict.VERIFY),  # an answer Jev did not give confirms nothing
    ],
)
async def test_confirmed_requirements_accept_a_doubted_page(answers: dict[str, float], verdict: DoneVerdict) -> None:
    check = await check_done(_Jev(answers), "Open the httpx repository.", _OPEN, _PAGE, Notes(), Thresholds())
    assert check.verdict is verdict


async def test_a_task_with_nothing_to_do_keeps_the_verifier() -> None:
    plan = Plan(requirements=(), answer_expected=False)
    check = await check_done(_Jev({"complete": 0.7}), "Look around.", plan, _PAGE, Notes(), Thresholds())
    assert check.verdict is DoneVerdict.VERIFY
