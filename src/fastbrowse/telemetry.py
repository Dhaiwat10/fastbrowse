"""Spend and call accounting against `Limits`, checked before each call rather than discovered after."""

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

    def check(self, extra_dollars: float = 0.0) -> None:
        limits = self.limits
        if limits.max_seconds is not None and monotonic() - self.started > limits.max_seconds:
            raise BudgetExceeded(f"time limit {limits.max_seconds}s reached")
        if limits.max_dollars is not None and self.breakdown().known_dollars + extra_dollars > limits.max_dollars:
            raise BudgetExceeded(f"spend limit ${limits.max_dollars} reached")
        if self.steps >= limits.max_steps:
            raise BudgetExceeded(f"step limit {limits.max_steps} reached")

    def record(self, *lines: CostLine) -> None:
        for line in lines:
            match line.component:
                case CostComponent.JEV:
                    self.jev_calls += 1
                case CostComponent.LLM:
                    self.llm_calls += 1
                case CostComponent.BROWSER | CostComponent.PROXY:
                    pass
            self.lines.append(line)

    def breakdown(self) -> CostBreakdown:
        return CostBreakdown(lines=tuple(self.lines))
