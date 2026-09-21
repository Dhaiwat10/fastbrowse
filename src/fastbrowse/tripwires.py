"""Signals that a run is grinding rather than progressing, evaluated from what the run already recorded.

One unchanged page is the signal fastbrowse has always acted on, and it misses the two ways a run can move
without getting anywhere: repeating one interaction, and changing pages while resolving nothing the plan
asked for. Skyvern runs the same three (`fail_fast/shadow.py`) and, at the time of writing, still runs all
of them in shadow mode while it measures how often each would fire on a run that went on to succeed. That
is the reason for `TripwireMode`: a tripwire that ends a healthy run is worse than one that never fires.

Pure: no IO, no mutation, so a would-fire evaluation costs nothing and can run on every step.
"""

from collections import Counter
from dataclasses import dataclass

from fastbrowse.models import Operation, Tripwire
from fastbrowse.policy import HistoryEntry


@dataclass(frozen=True, slots=True)
class Tripped:
    tripwire: Tripwire
    streak: int

    def __str__(self) -> str:
        return f"{self.tripwire.value} ({self.streak})"


def action_signature(entry: HistoryEntry) -> str | None:
    """What makes two history entries the same interaction, or None when the entry is not one.

    READ and DONE are excluded because neither targets an element: a run that legitimately reads the same
    page twice is not repeating an action, and counting it would fire this tripwire on every careful run.
    The visible effect is part of it for the same reason: "Next" that opens a new page each time is walking,
    and only "Next" that does the same thing again is grinding.
    """
    if entry.operation is None or entry.operation in {Operation.READ, Operation.DONE} or entry.target is None:
        return None
    return f"{entry.operation.value}:{entry.target}:{entry.text or ''}:{entry.effect or ''}"


def repeated_action(history: list[HistoryEntry], limit: int) -> Tripped | None:
    counts = Counter(signature for entry in history if (signature := action_signature(entry)) is not None)
    if not counts:
        return None
    _, count = counts.most_common(1)[0]
    return Tripped(Tripwire.ACTION_REPETITION, count) if count >= limit else None


def trailing_run(marks: list[str]) -> int:
    """How many equal marks the list ends with."""
    run = 0
    for mark in reversed(marks):
        if mark != marks[-1]:
            break
        run += 1
    return run


def stagnant_plan(marks: list[str], limit: int) -> Tripped | None:
    """`marks` is one fingerprint of the unresolved requirements per step, oldest first."""
    if not marks:
        return None
    run = trailing_run(marks)
    return Tripped(Tripwire.PLAN_STAGNATION, run) if run >= limit else None
