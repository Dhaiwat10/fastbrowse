from datetime import UTC, datetime

import pytest

from fastbrowse.memory import Fact, FactReader, Notes, NotesTooLarge, evidence_id
from fastbrowse.models import Evidence
from fastbrowse.planner import Plan, Requirement, RequirementKind


def evidence(*, sha: str = "capture", start: int = 0, end: int = 4) -> Evidence:
    return Evidence(
        source_id="s1",
        url="https://example.test",
        frame_id=None,
        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
        capture_sha256=sha,
        start=start,
        end=end,
        quote="fact",
    )


def test_notes_deduplicate_spans_without_losing_requirement_coverage() -> None:
    notes = Notes()
    assert notes.add(Fact(reader=FactReader.LLM, requirement_id="r1", text="First fact", evidence=evidence()))
    assert not notes.add(
        Fact(reader=FactReader.LLM, requirement_id="r2", text="Same span, another requirement", evidence=evidence())
    )
    assert notes.evidenced("r1") and notes.evidenced("r2")
    assert len(notes.facts) == 1
    plan = Plan(
        requirements=tuple(
            Requirement(id=f"r{i}", text=f"Requirement {i}", kind=RequirementKind.INFORMATION) for i in range(1, 4)
        ),
        answer_expected=True,
    )
    assert tuple(requirement.id for requirement in notes.unresolved(plan)) == ("r3",)
    assert notes.add(Fact(reader=FactReader.LLM, text="Another capture", evidence=evidence(sha="different")))
    assert notes.add(Fact(reader=FactReader.LLM, text="Another span", evidence=evidence(start=10, end=14)))
    assert len(notes.facts) == 3
    copy = notes.evidence
    copy.clear()
    assert len(notes.evidence) == 3


def test_render_reports_omissions_and_never_slices_a_citation() -> None:
    first = Fact(reader=FactReader.LLM, text="A long cited fact", evidence=evidence())
    second = Fact(reader=FactReader.LLM, text="A second cited fact", evidence=evidence(sha="second"))
    notes = Notes((first, second))
    complete = notes.render(1000)
    assert evidence_id(first.evidence) in complete and evidence_id(second.evidence) in complete
    assert 'quote="fact"' in complete
    one_line = Notes((first,)).render(1000)
    bounded = notes.render(len(one_line) + len("\n[1 facts omitted]"))
    assert bounded == one_line + "\n[1 facts omitted]"
    assert notes.render(20) == "[2 facts omitted]"
    with pytest.raises(ValueError, match="too small"):
        notes.render(1)
    assert Notes().render(0) == ""


def test_requirement_evidence_has_priority_including_reused_spans() -> None:
    context = Fact(reader=FactReader.LLM, text="Context", evidence=evidence(sha="context"))
    early = Fact(reader=FactReader.LLM, requirement_id="r1", text="First answer", evidence=evidence(sha="early"))
    late = Fact(reader=FactReader.LLM, text="Checkout total", evidence=evidence(sha="late"))
    notes = Notes((context, early, late))
    notes.add(late.model_copy(update={"requirement_id": "r2"}))
    required = Notes((early, late.model_copy(update={"requirement_id": "r2"})))
    expected = required.render(1000) + "\n[1 facts omitted]"
    assert notes.render(len(expected), preserve_requirements=True) == expected
    with pytest.raises(NotesTooLarge, match=f"{len(expected) - 1} character notes budget"):
        notes.render(len(expected) - 1, preserve_requirements=True)
