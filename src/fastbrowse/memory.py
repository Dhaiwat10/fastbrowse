"""Small evidence-backed memory with stable citation ids and explicit truncation."""

import json
from collections.abc import Iterable

from fastbrowse.models import Evidence, FactReader, Frozen
from fastbrowse.planner import Plan, Requirement


class Fact(Frozen):
    requirement_id: str | None = None
    text: str
    evidence: Evidence
    reader: FactReader
    """Which reader verified the quote; citations are built from it."""


class NotesTooLarge(RuntimeError):
    """A verdict cannot fit its requirement evidence without losing facts."""


class RenderedNotes(Frozen):
    text: str
    evidence_ids: tuple[str, ...]


def evidence_id(evidence: Evidence) -> str:
    return f"{evidence.capture_sha256}:{evidence.start}:{evidence.end}"


class Notes:
    def __init__(self, facts: Iterable[Fact] = ()) -> None:
        self._facts: dict[str, Fact] = {}
        self._requirements: dict[str, set[str]] = {}
        for fact in facts:
            self.add(fact)

    @property
    def facts(self) -> tuple[Fact, ...]:
        return tuple(self._facts.values())

    @property
    def evidence(self) -> dict[str, Evidence]:
        return {key: fact.evidence for key, fact in self._facts.items()}

    def add(self, fact: Fact) -> bool:
        """Return whether a new span was added; reused spans still evidence other requirements."""
        key = evidence_id(fact.evidence)
        requirements = self._requirements.setdefault(key, set())
        if fact.requirement_id is not None:
            requirements.add(fact.requirement_id)
        if key in self._facts:
            return False
        self._facts[key] = fact
        return True

    def evidenced(self, requirement_id: str) -> bool:
        return any(requirement_id in requirements for requirements in self._requirements.values())

    def supporting(self, requirement_id: str) -> tuple[tuple[str, Fact], ...]:
        """The facts citing a requirement, keyed by evidence id, in the order they were read."""
        return tuple((key, self._facts[key]) for key, ids in self._requirements.items() if requirement_id in ids)

    def unresolved(self, plan: Plan) -> tuple[Requirement, ...]:
        return tuple(requirement for requirement in plan.requirements if not self.evidenced(requirement.id))

    def render(self, max_chars: int, *, preserve_requirements: bool = False, json_encoded: bool = False) -> str:
        return self.render_with_ids(
            max_chars, preserve_requirements=preserve_requirements, json_encoded=json_encoded
        ).text

    def render_with_ids(
        self, max_chars: int, *, preserve_requirements: bool = False, json_encoded: bool = False
    ) -> RenderedNotes:
        """Every fact in read order when they all fit; otherwise uncited context is dropped before requirement
        evidence, each group kept in read order.

        Read order is what the composer weighs: listing requirement facts first put a reader's one-quote
        conclusion ("X is the most expensive") above the prices it compared, and the answer cited only that.
        Verdicts must fail when requirement evidence cannot fit, rather than decide without it.
        """
        if max_chars < 0:
            raise ValueError("max_chars must be nonnegative")

        def line(key: str, fact: Fact) -> str:
            return (
                f"[{key}] {json.dumps(fact.text, ensure_ascii=False)} "
                f"requirements={','.join(sorted(self._requirements[key])) or '-'} "
                f"source={json.dumps(fact.evidence.source_id)} url={json.dumps(fact.evidence.url)} "
                f"quote={json.dumps(fact.evidence.quote, ensure_ascii=False)}"
            )

        def size(text: str) -> int:
            # A JSON state escapes quotes and newlines; its notes budget must count those extra characters.
            return len(json.dumps(text)) - len('""') if json_encoded else len(text)

        complete = "\n".join(line(key, fact) for key, fact in self._facts.items())
        if size(complete) <= max_chars:
            return RenderedNotes(text=complete, evidence_ids=tuple(self._facts))
        ordered = sorted(self._facts.items(), key=lambda item: not self._requirements[item[0]])
        required = sum(bool(ids) for ids in self._requirements.values())
        lines = [line(key, fact) for key, fact in ordered]
        keys = tuple(key for key, _ in ordered)
        for count in range(len(lines) - 1, -1, -1):
            if preserve_requirements and count < required:
                raise NotesTooLarge(f"Requirement evidence exceeds the {max_chars} character notes budget")
            result = "\n".join([*lines[:count], f"[{len(lines) - count} facts omitted]"])
            if size(result) <= max_chars:
                return RenderedNotes(text=result, evidence_ids=keys[:count])
        if preserve_requirements:
            raise NotesTooLarge(f"The {max_chars} character notes budget cannot report omitted facts")
        raise ValueError("max_chars is too small to report omitted citations")
