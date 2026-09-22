import asyncio
from collections.abc import Mapping

import pytest
from pydantic import JsonValue

from fastbrowse.batches import evaluate_batches
from fastbrowse.config import TokenBudget
from fastbrowse.jev import Evaluation, NoulAnswer, NoulQuestion, Question
from fastbrowse.models import CostBasis, CostComponent, CostLine, Limits
from fastbrowse.telemetry import BudgetExceeded, Ledger

# Room for one question per request, so every question is its own batch.
_ONE_PER_BATCH = TokenBudget(state_plus_largest_question=40, state_plus_all_questions=40)
_QUESTIONS = {"a": NoulQuestion(instructions="first"), "b": NoulQuestion(instructions="second")}


class _SlowSecond:
    def __init__(self) -> None:
        self.sent: list[Mapping[str, Question]] = []

    async def evaluate(self, state: JsonValue, questions: Mapping[str, Question]) -> Evaluation:
        self.sent.append(questions)
        if "b" in questions:
            await asyncio.Event().wait()
        return Evaluation(
            model="test",
            answers={key: NoulAnswer(probability=0.5) for key in questions},
            input_tokens=10,
            cost=CostLine(component=CostComponent.JEV, basis=CostBasis.ESTIMATED, dollars=0.01),
        )


async def test_a_pass_cancelled_by_a_deadline_keeps_the_cost_already_billed() -> None:
    jev, ledger = _SlowSecond(), Ledger(Limits())
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await evaluate_batches(jev, {}, _QUESTIONS, tokens=_ONE_PER_BATCH, ledger=ledger)
    assert len(jev.sent) == 2 and ledger.breakdown().known_dollars == pytest.approx(0.01)


async def test_a_pass_the_call_limit_cannot_cover_counts_no_call() -> None:
    jev, ledger = _SlowSecond(), Ledger(Limits(max_jev_calls=1))
    with pytest.raises(BudgetExceeded):
        await evaluate_batches(jev, {}, _QUESTIONS, tokens=_ONE_PER_BATCH, ledger=ledger)
    assert jev.sent == [] and ledger.jev_calls == 0


class _Answers:
    def __init__(self) -> None:
        self.sent: list[Mapping[str, Question]] = []

    async def evaluate(self, state: JsonValue, questions: Mapping[str, Question]) -> Evaluation:
        self.sent.append(questions)
        return Evaluation(
            model="test",
            answers={key: NoulAnswer(probability=0.5) for key in questions},
            input_tokens=10,
            cost=CostLine(component=CostComponent.JEV, basis=CostBasis.ESTIMATED, dollars=0.01),
        )


async def test_many_questions_go_as_small_requests_even_when_one_would_fit() -> None:
    jev = _Answers()
    questions = {f"q{i}": NoulQuestion(instructions="x" * 300) for i in range(40)}
    answered = await evaluate_batches(jev, {}, questions, tokens=TokenBudget(batch_tokens=1000), ledger=None)
    assert answered is not None and len(answered.answers) == 40
    assert len(jev.sent) > 1 and all(len(sent) < 40 for sent in jev.sent)
