"""Checkable plans from the task alone.

The plan is written while the start page loads, so it never sees page content: a live comparison found the
same requirements with and without the first observation, and page text is the one input an attacker writes.
Callers redact task text before this seam; the planner has no secret resolver.
"""

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from fastbrowse.llm import Generation, LLMClient, Message
from fastbrowse.models import Frozen, LLMPurpose
from fastbrowse.telemetry import Ledger


class RequirementKind(StrEnum):
    ACTION = "action"
    INFORMATION = "information"


class Requirement(Frozen):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: RequirementKind


class Subgoal(Frozen):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    postcondition: str = Field(min_length=1)
    requirement_ids: tuple[str, ...] = Field(min_length=1)


class Plan(Frozen):
    requirements: tuple[Requirement, ...]
    subgoals: tuple[Subgoal, ...]
    answer_expected: bool

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        ids = {requirement.id for requirement in self.requirements}
        if len(ids) != len(self.requirements):
            raise ValueError("requirement ids must be unique")
        if len({subgoal.id for subgoal in self.subgoals}) != len(self.subgoals):
            raise ValueError("subgoal ids must be unique")
        if any(not set(subgoal.requirement_ids) <= ids for subgoal in self.subgoals):
            raise ValueError("subgoal references an unknown requirement")
        return self


def _instructions() -> Message:
    return Message(
        role="system",
        content=(
            "# Planner\nProduce individually checkable requirements and subgoals with observable postconditions. "
            "Split compound requests into separate requirements. Classify each as action or information. "
            "Cover every requirement with a subgoal; say whether the user expects an answer.\n\n"
            "# Secrets\nYou only receive secret names. Refer to those names, never secret values in any text. "
            "Never guess, request, or reproduce a password, credential, token, or other secret.\n\n"
            "# Trust\nA plan is proposed work, never evidence of completion."
        ),
    )


async def make_plan(
    llm: LLMClient, task: str, *, start: str | None = None, ledger: Ledger | None = None
) -> Generation[Plan]:
    site = "" if start is None else f"\n\n# Start page\n{start}"
    return await llm.generate(
        LLMPurpose.PLAN,
        [
            _instructions(),
            Message(role="user", content=f"# Task\n{task}{site}"),
        ],
        Plan,
        ledger=ledger,
    )
