"""What an action did, as the policy and recovery are told it."""

from fastbrowse.effects import effect, state_key
from fastbrowse.models import Operation
from fastbrowse.page import Control
from tests.test_policy import observation


def control(id: str, label: str, role: str = "button", **values: object) -> Control:
    made = Control(id=id, frame_id=None, role=role, label=label, operations=frozenset({Operation.CLICK}))
    return made.model_copy(update=values)


TRIGGER = control("trip", "Ticket type", "combobox", value="Round trip", expanded=True)
OPTIONS = (control("one", "One way", "option"), control("round", "Round trip", "option"))


def test_a_menu_closing_without_a_new_value_set_nothing() -> None:
    before = observation((TRIGGER, *OPTIONS))
    after = observation((TRIGGER.model_copy(update={"expanded": False}),))
    done = effect(before, after)
    assert not done.set_something
    assert "Ticket type expanded: True -> False" in done.summary
    assert "removed 2 controls: One way, Round trip" in done.summary


def test_a_value_transition_is_named_and_counts() -> None:
    before = observation((TRIGGER, *OPTIONS))
    after = observation((TRIGGER.model_copy(update={"value": "One way", "expanded": False}),))
    done = effect(before, after)
    assert done.set_something
    assert "Ticket type value: Round trip -> One way" in done.summary


def test_new_controls_and_a_new_address_are_reported() -> None:
    before = observation((TRIGGER,))
    after = observation((TRIGGER, *OPTIONS)).model_copy(update={"url": "https://example.test/b"})
    done = effect(before, after)
    assert done.set_something
    assert done.summary.startswith("went to https://example.test/b; showed 2 controls: One way, Round trip")
    assert effect(before, before).summary == "nothing visible changed"


def test_a_page_state_ignores_text_but_not_values() -> None:
    page = observation((TRIGGER,))
    assert state_key(page) == state_key(page.model_copy(update={"viewport_text": "12:01"}))
    assert state_key(page) != state_key(observation((TRIGGER.model_copy(update={"value": "One way"}),)))
