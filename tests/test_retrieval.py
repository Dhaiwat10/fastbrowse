import hashlib
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import BaseModel, Field, JsonValue

from fastbrowse.jev import ChoiceAnswer
from fastbrowse.llm import Generation, Message
from fastbrowse.memory import Fact, Notes, evidence_id
from fastbrowse.models import CostBasis, CostComponent, CostLine, Frozen, LLMPurpose
from fastbrowse.page import Block, BlockKind, Capture
from fastbrowse.planner import Plan, Requirement, RequirementKind
from fastbrowse.retrieval import (
    UnsupportedField,
    chunk,
    claim_check_questions,
    compose,
    copy_field,
    field_candidates,
    field_question,
    locate_quote,
    propose_text_fields,
    read,
)
from fastbrowse.telemetry import Ledger


def capture(*parts: tuple[BlockKind, str]) -> Capture:
    text = "\n\n".join(part for _, part in parts)
    blocks: list[Block] = []
    start = 0
    for i, (kind, part) in enumerate(parts):
        blocks.append(Block(source_id=f"s{i}", kind=kind, frame_id="frame", start=start, end=start + len(part)))
        start += len(part) + 2
    return Capture(
        url="https://example.test",
        title="Example",
        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
        blocks=tuple(blocks),
    )


class ScriptedLLM:
    def __init__(self, responses: Sequence[JsonValue]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[LLMPurpose, tuple[Message, ...]]] = []

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
        return Generation(
            data=schema.model_validate(self.responses.pop(0)),
            cost=CostLine(component=CostComponent.LLM, basis=CostBasis.METERED, dollars=0.001, purpose=purpose),
        )


def test_quote_location_preserves_original_offsets_and_is_block_scoped() -> None:
    page = capture(
        (BlockKind.PARAGRAPH, "Outside match"), (BlockKind.PARAGRAPH, "Prefix  bright\n\t blue\u00a0 sky suffix")
    )
    evidence = locate_quote(page, "s1", "bright blue sky")
    assert evidence is not None
    assert evidence.quote == "bright\n\t blue\u00a0 sky"
    assert evidence.start == page.text.index("bright")
    assert evidence.end == page.text.index(" sky") + len(" sky")
    assert page.text[evidence.start : evidence.end] == evidence.quote
    assert evidence.capture_sha256 == page.sha256 and evidence.frame_id == "frame"
    assert locate_quote(page, "s0", "bright blue sky") is None
    assert locate_quote(page, "s1", "BRIGHT blue sky") is None
    assert locate_quote(page, "missing", "bright") is None
    assert locate_quote(page, "s1", " \n ") is None
    assert locate_quote(page, "s0", "match Prefix") is None


def test_chunk_prefers_headings_and_preserves_block_coverage() -> None:
    page = capture(
        (BlockKind.HEADING, "Intro"),
        (BlockKind.PARAGRAPH, "First block"),
        (BlockKind.HEADING, "Next"),
        (BlockKind.PARAGRAPH, "Second block with text"),
    )
    parts = chunk(page, 27, overlap_blocks=0)
    assert parts[0].block_ids == ("s0", "s1")
    assert parts[1].block_ids[0] == "s2"
    assert {key for part in parts for key in part.block_ids} == {block.source_id for block in page.blocks}
    assert [part.index for part in parts] == list(range(len(parts)))
    assert all(part.total == len(parts) for part in parts)
    for part in parts:
        assert len(part.text) <= 27
        assert part.start in {block.start for block in page.blocks}
        assert part.end in {block.end for block in page.blocks}


def test_chunk_overlap_and_indivisible_blocks_make_progress() -> None:
    page = capture(*((BlockKind.PARAGRAPH, text) for text in ("aaaa", "bbbb", "cccc", "dddd")))
    parts = chunk(page, 9)
    assert [part.block_ids for part in parts] == [("s0", "s1"), ("s1", "s2"), ("s2", "s3")]
    huge = capture((BlockKind.CODE, "x" * 100), (BlockKind.PARAGRAPH, "tail"))
    assert [part.text for part in chunk(huge, 10)] == ["x" * 100, "tail"]
    assert chunk(capture(), 10) == ()
    with pytest.raises(ValueError):
        chunk(page, 0)


def test_chunk_repeats_markdown_table_header_and_keeps_rows_grounded() -> None:
    header = "| Name | Cost |\n| --- | --- |"
    rows = [f"| Item{i} | ${i}.00 |" for i in range(8)]
    page = capture((BlockKind.TABLE, header + "\n" + "\n".join(rows)))
    parts = chunk(page, 65, overlap_blocks=0)
    assert len(parts) > 1
    assert all(part.text.startswith(header) for part in parts)
    assert all(part.block_ids == ("s0",) for part in parts)
    assert all(len(part.text) <= 65 for part in parts)
    for row in rows:
        assert any(row in part.text for part in parts)
        evidence = locate_quote(page, "s0", row)
        assert evidence is not None and page.text[evidence.start : evidence.end] == row


def test_chunk_repeats_nearest_header_when_table_rows_are_separate_blocks() -> None:
    page = capture(
        (BlockKind.TABLE, "Name | Cost"),
        (BlockKind.TABLE, "AAA | 100"),
        (BlockKind.TABLE, "BBB | 200"),
        (BlockKind.HEADING, "Other"),
        (BlockKind.TABLE, "Age | Count"),
        (BlockKind.TABLE, "CCC | 300"),
    )
    parts = chunk(page, 22, overlap_blocks=0)
    continued = next(part for part in parts if "BBB" in part.text)
    assert continued.text.startswith("Name | Cost") and "s0" in continued.block_ids
    last = next(part for part in parts if "CCC" in part.text)
    assert "Age | Count" in last.text and "Name | Cost" not in last.text


async def test_read_continues_after_forged_quote_tracks_coverage_and_cost() -> None:
    page = capture(
        (BlockKind.PARAGRAPH, "Price unknown"),
        (BlockKind.PARAGRAPH, "Price is $12"),
        (BlockKind.PARAGRAPH, "Irrelevant end"),
    )
    llm = ScriptedLLM(
        [
            {
                "claims": [{"requirement_id": "r1", "text": "Free", "source_id": "s0", "quote": "Price is free"}],
                "answered": True,
            },
            {
                "claims": [{"requirement_id": "r1", "text": "Costs $12", "source_id": "s1", "quote": "Price is $12"}],
                "answered": True,
            },
        ]
    )
    notes = Notes()
    result = await read(llm, page, "What price?", ["r1"], notes, max_chars=15)
    assert result.coverage == (0, 1) and result.rejected_quotes == 1
    assert len(result.facts) == 1 and result.facts[0].evidence.quote == "Price is $12"
    assert notes.evidenced("r1")
    assert sum(line.dollars or 0 for line in result.cost_lines) == 0.002
    assert all(purpose is LLMPurpose.READ for purpose, _ in llm.calls)
    assert "s1" in llm.calls[1][1][-1].content


async def test_read_reaches_end_and_does_not_evidence_unknown_requirements() -> None:
    page = capture((BlockKind.PARAGRAPH, "Known fact"), (BlockKind.PARAGRAPH, "Other fact"))
    llm = ScriptedLLM(
        [
            {
                "claims": [
                    {"requirement_id": "invented", "text": "Known fact", "source_id": "s0", "quote": "Known fact"}
                ],
                "answered": False,
            },
            {"claims": [], "answered": False},
        ]
    )
    notes = Notes()
    result = await read(llm, page, "Need more", ["r1"], notes, max_chars=11)
    assert result.coverage == (0, 1) and len(result.facts) == 1
    assert not notes.evidenced("invented") and not notes.evidenced("r1")


class Fields(Frozen):
    label: str
    count: int = Field(ge=1000)
    amount: Decimal
    weight: float
    when: date
    available: bool
    records: list[str]


@pytest.mark.parametrize(
    "name,text,expected",
    [
        ("count", "Stock: 1,234 units", 1234),
        ("amount", "Price: $1,234.50 today", Decimal("1234.50")),
        ("weight", "Weight: 12.25 kg", 12.25),
        ("when", "Due on 2026-09-17", date(2026, 9, 17)),
        ("available", "Available: YES", True),
    ],
)
def test_field_copy_is_typed_and_keeps_verbatim_evidence(name: str, text: str, expected: object) -> None:
    page = capture((BlockKind.PARAGRAPH, text))
    field = Fields.model_fields[name]
    candidates = field_candidates(page, field)
    assert not isinstance(candidates, UnsupportedField)
    assert len(candidates) == 1
    question = field_question(field, candidates)
    candidate = candidates[0]
    answer = ChoiceAnswer(choice=candidate.id, probabilities={candidate.id: 1, "none": 0}, confidence=1)
    copied = copy_field(answer, candidates)
    assert copied is not None and copied[0] == expected
    assert type(copied[0]) is type(expected)
    assert copied[1].quote == page.text[copied[1].start : copied[1].end]
    assert question.criteria[candidate.id] == {"source_id": "s0", "quote": copied[1].quote, "context": text}
    assert "none" in question.criteria
    assert copy_field(answer.model_copy(update={"choice": "none"}), candidates) is None
    assert copy_field(answer.model_copy(update={"choice": "invented"}), candidates) is None


def test_field_constraints_and_explicit_unsupported_records() -> None:
    page = capture((BlockKind.PARAGRAPH, "Stock 5 or 1,500.5"))
    assert field_candidates(page, Fields.model_fields["count"]) == ()
    assert isinstance(field_candidates(page, Fields.model_fields["records"]), UnsupportedField)
    assert field_candidates(capture((BlockKind.PARAGRAPH, "2026-02-30")), Fields.model_fields["when"]) == ()


async def test_compose_drops_uncited_and_unknown_claims_including_answer_text() -> None:
    page = capture((BlockKind.PARAGRAPH, "Price is $12"))
    evidence = locate_quote(page, "s0", "Price is $12")
    assert evidence is not None
    notes = Notes((Fact(requirement_id="r1", text="Price is $12", evidence=evidence),))
    key = evidence_id(evidence)
    llm = ScriptedLLM(
        [
            {
                "answer": "It is $12 and shipping is free. It arrives tomorrow.",
                "claims": [
                    {"text": "It is $12.", "evidence_ids": [key]},
                    {"text": "Shipping is free.", "evidence_ids": []},
                    {"text": "It arrives tomorrow.", "evidence_ids": ["invented"]},
                ],
            }
        ]
    )
    plan = Plan(
        requirements=(
            Requirement(id="r1", text="Find price", kind=RequirementKind.INFORMATION),
            Requirement(id="r2", text="Find shipping", kind=RequirementKind.INFORMATION),
        ),
        subgoals=(),
        answer_expected=True,
    )
    result = await compose(llm, "Find price and shipping", plan, notes)
    assert result.data.answer == "It is $12."
    assert result.data.dropped_claims == 2 and len(result.data.claims) == 1
    assert result.cost.dollars == 0.001
    questions = claim_check_questions(result.data, notes)
    assert set(questions) == {"unsupported_0", "contradicted_0", "requirement_omitted"}
    assert "Price is $12" in questions["unsupported_0"].instructions
    assert "Find shipping" in questions["requirement_omitted"].instructions
    assert all(question.true is not None and question.true.startswith("Yes,") for question in questions.values())


async def test_compose_cannot_return_uncited_free_text_without_claims() -> None:
    llm = ScriptedLLM([{"answer": "Everything is complete", "claims": []}])
    result = await compose(llm, "Do it", Plan(requirements=(), subgoals=(), answer_expected=True), Notes())
    assert result.data.answer == "" and result.data.claims == ()


def test_currency_sentence_punctuation_and_candidate_context() -> None:
    page = capture((BlockKind.PARAGRAPH, "Revenue: $1,234.50. Costs: $200.00."))
    field = Fields.model_fields["amount"]
    candidates = field_candidates(page, field)
    assert not isinstance(candidates, UnsupportedField)
    assert [candidate.value for candidate in candidates] == [Decimal("1234.50"), Decimal("200.00")]
    assert [candidate.evidence.quote for candidate in candidates] == ["$1,234.50", "$200.00"]
    question = field_question(field, candidates, name="revenue")
    assert "revenue" in question.instructions
    assert question.criteria[candidates[0].id] == {
        "source_id": "s0",
        "quote": "$1,234.50",
        "context": page.text,
    }


async def test_text_fields_are_kept_only_when_quoted_verbatim_from_the_page() -> None:
    page = capture((BlockKind.HEADING, "httpx 0.28.1"), (BlockKind.PARAGRAPH, "License: BSD"))
    llm = ScriptedLLM(
        [
            {
                "fields": [
                    {"field": "label", "value": "0.28.1", "source_id": "s0", "quote": "httpx 0.28.1"},
                    {"field": "license", "value": "MIT", "source_id": "s1", "quote": "License: BSD"},
                    {"field": "owner", "value": "encode", "source_id": "s1", "quote": "Owner: encode"},
                ]
            }
        ]
    )
    fields = {name: Fields.model_fields["label"] for name in ("label", "license", "owner")}
    found, _ = await propose_text_fields(llm, "Get the version", page, fields)
    assert found.keys() == {"label"}
    value, evidence = found["label"]
    assert value == "0.28.1" and evidence.quote == "httpx 0.28.1"


@pytest.mark.parametrize("limit", ["calls", "dollars"])
@pytest.mark.parametrize("reader", ["read", "fields"])
async def test_each_chunk_reserves_budget_before_request(limit: str, reader: str) -> None:
    from fastbrowse.models import Limits
    from fastbrowse.telemetry import BudgetExceeded, Ledger

    page = capture((BlockKind.PARAGRAPH, "First chunk"), (BlockKind.PARAGRAPH, "Second chunk"))
    response: JsonValue = {"claims": [], "answered": False} if reader == "read" else {"fields": []}
    llm = ScriptedLLM([response, response])
    ledger = Ledger(Limits(max_llm_calls=1) if limit == "calls" else Limits(max_dollars=0.001))
    with pytest.raises(BudgetExceeded):
        if reader == "read":
            await read(llm, page, "Find it", (), Notes(), max_chars=12, ledger=ledger)
        else:
            await propose_text_fields(
                llm, "Find it", page, {"label": Fields.model_fields["label"]}, max_chars=12, ledger=ledger
            )
    assert len(llm.calls) == 1
    assert ledger.llm_calls == 1 and ledger.breakdown().known_dollars == 0.001


async def test_extraction_reserves_each_scalar_field() -> None:
    from fastbrowse.models import Limits
    from fastbrowse.telemetry import BudgetExceeded, Ledger
    from fastbrowse.verification import extract
    from tests.test_policy import ScriptedJev

    class Record(Frozen):
        first: int
        second: int

    page = capture((BlockKind.PARAGRAPH, "First: 10. Second: 20."))
    jev = ScriptedJev({})
    ledger = Ledger(Limits(max_jev_calls=1))
    with pytest.raises(BudgetExceeded):
        await extract(jev, ScriptedLLM([]), "Get both fields", page, Record, ledger=ledger)
    assert len(jev.requests) == 1 and ledger.jev_calls == 1


async def test_composition_and_claims_share_budget() -> None:
    from fastbrowse.config import Thresholds
    from fastbrowse.models import Limits
    from fastbrowse.telemetry import BudgetExceeded, Ledger
    from fastbrowse.verification import check_claims
    from tests.test_policy import ScriptedJev

    llm = ScriptedLLM([{"answer": "", "claims": []}])
    ledger = Ledger(Limits(max_dollars=0.001))
    composed = await compose(
        llm, "Find it", Plan(requirements=(), subgoals=(), answer_expected=True), Notes(), ledger=ledger
    )
    jev = ScriptedJev({})
    with pytest.raises(BudgetExceeded):
        await check_claims(jev, composed.data, Notes(), Thresholds(), ledger=ledger)
    assert len(llm.calls) == 1 and jev.requests == []
