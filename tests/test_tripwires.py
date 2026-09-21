"""The tripwire predicates. Pure functions over recorded history, so these are the cheapest proof they hold."""

from fastbrowse.models import Operation, StepOutcome
from fastbrowse.policy import HistoryEntry
from fastbrowse.tripwires import Tripwire, action_signature, repeated_action, stagnant_plan, trailing_run


def entry(
    operation: Operation | None, target: str | None, text: str | None = None, effect: str | None = None
) -> HistoryEntry:
    return HistoryEntry(
        operation=operation, target=target, outcome=StepOutcome.EXECUTED, page_changed=True, text=text, effect=effect
    )


def test_reads_are_not_repeatable_actions() -> None:
    # A run that reads the same page repeatedly is being careful, not grinding; only the unchanged-page
    # count should have an opinion about it.
    assert action_signature(entry(Operation.READ, None)) is None
    assert action_signature(entry(Operation.DONE, None)) is None
    assert action_signature(entry(None, None)) is None
    assert action_signature(entry(Operation.CLICK, "Next")) == "click:Next::"


def test_paging_forward_is_walking_and_the_same_click_to_the_same_result_is_grinding() -> None:
    walk = [entry(Operation.CLICK, "Next", effect=f"address: /results?page={n}") for n in range(2, 6)]
    assert repeated_action(walk, 3) is None
    grind = [entry(Operation.CLICK, "Stops", effect="address: /flights?tfs=ABC") for _ in range(3)]
    assert repeated_action(grind, 3) is not None


def test_same_target_with_a_different_value_is_not_a_repetition() -> None:
    history = [entry(Operation.FILL, "Email", "a@example.com"), entry(Operation.FILL, "Email", "b@example.com")]
    assert repeated_action(history * 2, limit=3) is None


def test_one_interaction_recurring_trips() -> None:
    history = [entry(Operation.CLICK, "Submit")] * 3
    tripped = repeated_action(history, limit=3)
    assert tripped is not None
    assert tripped.tripwire is Tripwire.ACTION_REPETITION
    assert tripped.streak == 3


def test_trailing_run_counts_only_the_end() -> None:
    assert trailing_run(["a", "b", "b", "b"]) == 3
    assert trailing_run(["b", "b", "a"]) == 1
    assert trailing_run([]) == 0


def test_plan_stagnation_needs_an_unbroken_tail() -> None:
    # Resolving one requirement changes the mark, which is what clears the tripwire.
    assert stagnant_plan(["r1,r2", "r1,r2", "r1"], limit=3) is None
    assert stagnant_plan([], limit=3) is None
    tripped = stagnant_plan(["r1", "r1,r2", "r1,r2", "r1,r2"], limit=3)
    assert tripped is not None
    assert tripped.tripwire is Tripwire.PLAN_STAGNATION
    assert tripped.streak == 3
