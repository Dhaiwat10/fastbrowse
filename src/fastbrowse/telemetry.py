"""Spend and call accounting against `Limits`, checked before each call rather than discovered after."""

import logging
from dataclasses import dataclass, field
from time import monotonic

from fastbrowse.models import CostBreakdown, CostComponent, CostLine, Limits, Status


class BudgetExceeded(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.status = Status.BUDGET_EXCEEDED


@dataclass(slots=True)
class Ledger:
    limits: Limits
    started: float = field(default_factory=monotonic)
    lines: list[CostLine] = field(default_factory=list[CostLine])
    steps: int = 0
    jev_calls: int = 0
    llm_calls: int = 0

    def reserve(self, component: CostComponent, estimate_dollars: float = 0.0) -> None:
        """Raise before a call that would break a limit; `record` the actual line afterwards."""
        match component:
            case CostComponent.JEV:
                if self.jev_calls >= self.limits.max_jev_calls:
                    raise BudgetExceeded(f"Jev call limit {self.limits.max_jev_calls} reached")
            case CostComponent.LLM:
                if self.llm_calls >= self.limits.max_llm_calls:
                    raise BudgetExceeded(f"LLM call limit {self.limits.max_llm_calls} reached")
            case CostComponent.BROWSER | CostComponent.PROXY:
                pass
        self.check(estimate_dollars)
        if self.limits.max_dollars is not None and self.breakdown().known_dollars >= self.limits.max_dollars:
            raise BudgetExceeded(f"spend limit ${self.limits.max_dollars} reached")
        # Failed requests still consume a call, including retries after an input-size rejection.
        if component is CostComponent.JEV:
            self.jev_calls += 1
        elif component is CostComponent.LLM:
            self.llm_calls += 1

    def check(self, extra_dollars: float = 0.0) -> None:
        limits = self.limits
        if limits.max_seconds is not None and monotonic() - self.started > limits.max_seconds:
            raise BudgetExceeded(f"time limit {limits.max_seconds}s reached")
        if limits.max_dollars is not None:
            spent = self.breakdown()
            # An unpriced call could have spent anything, so a dollar cap cannot be enforced past it.
            if spent.has_unknown:
                raise BudgetExceeded(f"spend limit ${limits.max_dollars} cannot be enforced: a call reported no cost")
            if spent.known_dollars + extra_dollars > limits.max_dollars:
                raise BudgetExceeded(f"spend limit ${limits.max_dollars} reached")
        if self.steps >= limits.max_steps:
            raise BudgetExceeded(f"step limit {limits.max_steps} reached")

    def record(self, *lines: CostLine) -> None:
        self.lines.extend(lines)
        self.check()

    def breakdown(self) -> CostBreakdown:
        return CostBreakdown(lines=tuple(self.lines))


TRACE = logging.getLogger("fastbrowse.trace")
"""Why a run went the way it did, one record per judgement, for evals to keep beside each result: ids, verdicts,
scores and redacted addresses, never page text. Nothing is built unless DEBUG is enabled on this logger."""


def trace(event: str, **fields: object) -> None:
    if TRACE.isEnabledFor(logging.DEBUG):
        TRACE.debug(event, extra={"trace": {"event": event, **fields}})
