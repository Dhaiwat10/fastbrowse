import json
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from pydantic import JsonValue

from fastbrowse.config import Config, Thresholds, TokenBudget
from fastbrowse.jev import Answer, Evaluation, NoulAnswer, Question
from fastbrowse.memory import Fact, Notes, fact_id
from fastbrowse.models import UNTRUSTED, CostBasis, CostComponent, CostLine, FactReader, Operation
from fastbrowse.page import Control, Observation
from fastbrowse.planner import Plan, Requirement, RequirementKind
from fastbrowse.retrieval import claim_check_questions, compose
from fastbrowse.verification import DoneVerdict, LLMVerdict, check_done, llm_verify, page_state
from tests.test_memory import evidence
from tests.test_retrieval import ScriptedLLM

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
        self.state: JsonValue = None
        self.questions: Mapping[str, Question] = {}

    async def evaluate(self, state: JsonValue, questions: Mapping[str, Question]) -> Evaluation:
        self.state, self.questions = state, questions
        answers: dict[str, Answer] = {k: NoulAnswer(probability=p) for k, p in self.answers.items() if k in questions}
        cost = CostLine(component=CostComponent.JEV, basis=CostBasis.ESTIMATED, dollars=0.0)
        return Evaluation(model="test", answers=answers, input_tokens=1, cost=cost)


# Completion scores came from live runs; requirement scores are the complements of the old doubt answers.
@pytest.mark.parametrize(
    ("answers", "verdict"),
    [
        ({"complete": 0.63, "satisfied_r1": 0.81}, DoneVerdict.ACCEPT),  # the repository, doubted as a whole
        ({"complete": 0.90, "satisfied_r1": 0.86}, DoneVerdict.ACCEPT),
        ({"complete": 0.03, "satisfied_r1": 0.15}, DoneVerdict.VERIFY),  # the organisation page
        ({"complete": 0.49, "satisfied_r1": 0.34}, DoneVerdict.VERIFY),  # doubt either way goes to the verifier
        ({"complete": 0.72, "satisfied_r1": 0.55}, DoneVerdict.VERIFY),
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


@pytest.mark.parametrize(
    ("satisfied", "complete", "verdict", "unmet"),
    [
        (0.71, 0.5, DoneVerdict.ACCEPT, ()),
        (0.70, 0.5, DoneVerdict.VERIFY, ()),
        (0.30, 0.9, DoneVerdict.ACCEPT, ()),
        (0.29, 0.9, DoneVerdict.VERIFY, ("r1",)),
    ],
)
async def test_positive_requirement_answers_preserve_the_doubt_thresholds(
    satisfied: float, complete: float, verdict: DoneVerdict, unmet: tuple[str, ...]
) -> None:
    check = await check_done(
        _Jev({"complete": complete, "satisfied_r1": satisfied}), "Open it", _OPEN, _PAGE, Notes(), Thresholds()
    )
    assert check.verdict is verdict and check.unmet == unmet


def test_verifier_schema_emits_evidence_before_the_verdict_with_field_guidance() -> None:
    properties = LLMVerdict.model_json_schema()["properties"]
    assert list(properties) == ["missing", "complete"]
    assert "Requirement ids" in properties["missing"]["description"]


async def test_verifier_receives_context_before_the_final_verdict_instruction() -> None:
    llm = ScriptedLLM([{"missing": [], "complete": True}])
    await llm_verify(llm, "Open the repository", _OPEN, _PAGE, (b"screenshot",), Notes(), ())
    messages = llm.calls[0][1]
    assert UNTRUSTED in messages[0].content
    prompt = messages[-1].content
    assert prompt.index("## Page") < prompt.index("## Notes") < prompt.index("## Verdict")
    assert messages[-1].images == (b"screenshot",)


@pytest.mark.parametrize(("largest", "total"), [(1500, 5000), (5000, 1500)])
async def test_verdict_prompts_keep_late_requirement_evidence_when_notes_overflow(largest: int, total: int) -> None:
    tokens = TokenBudget(state_plus_largest_question=largest, state_plus_all_questions=total)
    notes = Notes(
        Fact(reader=FactReader.LLM, text="Background " * 100, evidence=evidence(sha=f"context-{i}")) for i in range(20)
    )
    total_text = "Checkout total is $42"
    late = Fact(
        reader=FactReader.LLM,
        requirement_id="r1",
        text=total_text,
        evidence=evidence(sha="late", end=len(total_text)).model_copy(update={"quote": total_text}),
    )
    notes.add(late)
    plan = Plan(
        requirements=(Requirement(id="r1", text="Report the checkout total", kind=RequirementKind.INFORMATION),),
        answer_expected=True,
    )
    jev = _Jev({"complete": 0.9})
    assert (
        await check_done(jev, "Total?", plan, _PAGE, notes, Thresholds(), tokens=tokens)
    ).verdict is DoneVerdict.ACCEPT
    llm = ScriptedLLM(
        [
            {"complete": True, "missing": []},
            {"claims": [{"text": late.text, "evidence_ids": [fact_id(late)]}]},
        ]
    )
    await llm_verify(llm, "Total?", plan, _PAGE, (), notes, (), config=Config(tokens=tokens))
    composed = await compose(llm, "Total?", plan, notes, tokens=tokens)
    questions = claim_check_questions(composed.data, notes, tokens=tokens)
    for prompt in [
        json.dumps(jev.state),
        *(call[1][-1].content for call in llm.calls),
        questions["requirement_omitted"].instructions,
    ]:
        assert late.text in prompt and fact_id(late) in prompt
        assert "facts omitted]" in prompt
    for state, batch in [(jev.state, jev.questions), ({"answer": composed.data.answer}, questions)]:
        state_chars = len(json.dumps(state))
        sizes = [len(q.model_dump_json()) for q in batch.values()]
        assert state_chars + max(sizes) <= largest * tokens.chars_per_token
        assert state_chars + sum(sizes) <= total * tokens.chars_per_token


async def test_a_page_too_long_for_the_evidence_is_cut_rather_than_ending_the_run() -> None:
    tokens = TokenBudget(state_plus_largest_question=1500, state_plus_all_questions=1500)
    total_text = "Checkout total is $42"
    notes = Notes(
        [
            Fact(
                reader=FactReader.LLM,
                requirement_id="r1",
                text=total_text,
                evidence=evidence(sha="total", end=len(total_text)).model_copy(update={"quote": total_text}),
            )
        ]
    )
    page = _PAGE.model_copy(update={"viewport_text": 'Flight "row"\n' * 2000})
    state = page_state(page, notes, tokens)
    assert isinstance(state, dict) and isinstance(state["page"], dict) and isinstance(state["notes"], str)
    assert total_text in state["notes"]
    assert "[Viewport text cut:" in str(state["page"]["text"])
    assert len(json.dumps(state)) <= tokens.state_plus_all_questions * tokens.chars_per_token
    plan = Plan(
        requirements=(Requirement(id="r1", text="Report the checkout total", kind=RequirementKind.INFORMATION),),
        answer_expected=True,
    )
    llm = ScriptedLLM([{"complete": True, "missing": []}])
    await llm_verify(llm, "Total?", plan, page, (), notes, (), config=Config(tokens=tokens))
    prompt = llm.calls[0][1][-1].content
    assert total_text in prompt and "[Viewport text cut:" in prompt


def test_controls_without_state_give_way_to_the_evidence_before_the_run_ends() -> None:
    # "View more flights" put hundreds of result rows on the page as controls, and those alone left the done check
    # a 0 character notes budget, ending a run that had its evidence.
    tokens = TokenBudget(state_plus_largest_question=1500, state_plus_all_questions=1500)
    total_text = "Checkout total is $42"
    notes = Notes(
        [
            Fact(
                reader=FactReader.LLM,
                requirement_id="r1",
                text=total_text,
                evidence=evidence(sha="total", end=len(total_text)).model_copy(update={"quote": total_text}),
            )
        ]
    )
    stops = Control(
        id="stops",
        frame_id=None,
        role="checkbox",
        label="Nonstop only",
        operations=frozenset({Operation.CLICK}),
        checked=True,
    )
    rows = tuple(
        Control(
            id=f"row{i}",
            frame_id=None,
            role="button",
            label=f"From {100 + i} US dollars. Nonstop flight",
            operations=frozenset({Operation.CLICK}),
        )
        for i in range(300)
    )
    state = page_state(_PAGE.model_copy(update={"controls": (stops, *rows)}), notes, tokens)
    assert isinstance(state, dict) and isinstance(state["notes"], str)
    assert total_text in state["notes"]
    assert state["controls"] == [{"label": "Nonstop only", "role": "checkbox", "checked": True}]
    assert state["controls_omitted"] == 300
