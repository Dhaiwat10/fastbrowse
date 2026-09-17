"""Small evidence-backed memory with stable citation ids and explicit truncation."""

import json
from collections.abc import Iterable

from fastbrowse.models import Evidence, Frozen
from fastbrowse.planner import Plan, Requirement


class Fact(Frozen):
    requirement_id: str | None = None
    text: str
    evidence: Evidence


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

    def unresolved(self, plan: Plan) -> tuple[Requirement, ...]:
        return tuple(requirement for requirement in plan.requirements if not self.evidenced(requirement.id))

    def render(self, max_chars: int) -> str:
        """Only include whole cited facts. Tiny budgets unable to report omissions are invalid."""
        if max_chars < 0:
            raise ValueError("max_chars must be nonnegative")
        lines = [
            f"[{key}] {json.dumps(fact.text, ensure_ascii=False)} "
            f"requirements={','.join(sorted(self._requirements[key])) or '-'} "
            f"source={json.dumps(fact.evidence.source_id)} url={json.dumps(fact.evidence.url)} "
            f"quote={json.dumps(fact.evidence.quote, ensure_ascii=False)}"
            for key, fact in self._facts.items()
        ]
        complete = "\n".join(lines)
        if len(complete) <= max_chars:
            return complete
        for count in range(len(lines) - 1, -1, -1):
            result = "\n".join([*lines[:count], f"[{len(lines) - count} facts omitted]"])
            if len(result) <= max_chars:
                return result
        raise ValueError("max_chars is too small to report omitted citations")
