"""What an action did, as the policy and recovery are told it."""

import pytest

from fastbrowse.effects import content_key, effect, holding, move, reversal, state_key
from fastbrowse.models import Operation
from fastbrowse.page import Control, Observation
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


@pytest.mark.parametrize("scope", ["document_key", "context", "frame_id", "frame_origin"])
def test_reversals_do_not_match_a_control_from_another_document_or_context(scope: str) -> None:
    toggle = control("filter", "Direct only", "checkbox", checked=False, context="Outbound")
    off = observation((toggle,)).model_copy(update={"document_key": "doc"})
    on = off.model_copy(update={"controls": (toggle.model_copy(update={"checked": True}),)})
    earlier = move(off, on)
    assert earlier is not None
    changed = []
    for obs in (on, off):
        changed.append(
            obs.model_copy(update={"document_key": "another"})
            if scope == "document_key"
            else obs.model_copy(update={"controls": (obs.controls[0].model_copy(update={scope: "another"}),)})
        )
    later = move(*changed)
    assert later is not None and reversal(later, earlier) is None
    assert move(on, off.model_copy(update={"document_key": "new"})) is None


@pytest.mark.parametrize(
    ("attribute", "before", "after"), [("checked", False, True), ("selected", False, True), ("value", "Price", "Name")]
)
@pytest.mark.parametrize("other_returns", [False, True])
def test_reversal_requires_every_committed_value_to_return(
    attribute: str, before: str | bool, after: str | bool, other_returns: bool
) -> None:
    setting = control("setting", "Setting").model_copy(update={attribute: before})
    other = control("other", "Other", value="original")
    original = observation((setting, other)).model_copy(update={"document_key": "doc"})
    changed = original.model_copy(update={"controls": (setting.model_copy(update={attribute: after}), other)})
    returned = original.model_copy(
        update={"controls": (setting, other if other_returns else other.model_copy(update={"value": "new"}))}
    )
    earlier, later = move(original, changed), move(changed, returned)
    assert earlier is not None and later is not None
    note = reversal(later, earlier)
    assert note == (f"Setting keeps returning to {attribute}={before}" if other_returns else None)


@pytest.mark.parametrize("value", ["Round trip", "One way"])
def test_reopening_controls_with_new_results_is_not_a_value_reversal(value: str) -> None:
    panel = observation((TRIGGER, control("done", "Done"))).model_copy(update={"document_key": "doc"})
    form = observation((TRIGGER.model_copy(update={"expanded": False}), control("search", "Search"))).model_copy(
        update={"document_key": "doc"}
    )
    earlier = move(panel, form)
    later = move(
        form,
        panel.model_copy(
            update={
                "controls": (
                    TRIGGER.model_copy(update={"value": value}),
                    panel.controls[1],
                    control("result", "New result"),
                )
            }
        ),
    )
    assert earlier is not None and later is not None and reversal(later, earlier) is None


def test_a_value_set_on_the_field_an_overlay_stood_in_for_counts() -> None:
    editor = control("editor", "Where from? ", "combobox", value="Lond")
    choice = control("london", "London, United Kingdom", "option")
    field = control("field", "Where from?", "combobox", value="London")
    done = effect(observation((editor, choice)), observation((field,)))
    assert done.set_something
    # The label differs only in spacing, which is not a change.
    assert done.summary.startswith("changed Where from? value: Lond -> London;")


def test_a_chosen_suggestion_that_leaves_the_typed_text_counts() -> None:
    editor = control("editor", "Where from?", "combobox", value="London")
    choice = control("london", "London, United Kingdom", "option")
    field = control("field", "Where from?", "combobox", value="London")
    assert effect(observation((editor, choice)), observation((field,)), choice).set_something


def test_an_option_no_field_shows_afterwards_set_nothing() -> None:
    after = observation((TRIGGER.model_copy(update={"label": "Ticket type. Round trip", "expanded": False}),))
    assert not effect(observation(OPTIONS), after, OPTIONS[0]).set_something


def test_an_action_that_set_nothing_names_the_fields_the_form_still_needs() -> None:
    search = Control(id="s", frame_id=None, role="button", label="Search", operations=frozenset({Operation.CLICK}))
    needed = Control(
        id="r", frame_id=None, role="textbox", label="Return", operations=frozenset({Operation.FILL}), blocking=True
    )
    form = observation((search, needed))
    assert effect(form, form).summary == "nothing visible changed; fields the form still needs: 1 control: Return"
    filled = observation((search, needed.model_copy(update={"value": "Oct 23", "blocking": False})))
    assert "still needs" not in effect(form, filled).summary


def test_a_page_state_keeps_its_address_but_what_a_reader_finds_does_not() -> None:
    """A read is kept by document, not by URL: a site rewrites its own query as a list is paged or filtered."""
    refresh = control("refresh", "Refresh")
    here = observation((refresh,))
    elsewhere = here.model_copy(update={"url": "https://example.test/other"})
    assert state_key(here) != state_key(elsewhere)
    assert content_key(here) == content_key(elsewhere)


def test_text_that_rewrites_itself_is_the_same_page_to_a_reader_but_a_new_control_is_not() -> None:
    refresh = control("refresh", "Refresh")
    here = observation((refresh,))
    ticking = here.model_copy(update={"viewport_text": "updated 4 seconds ago"})
    assert content_key(here) == content_key(ticking)
    assert content_key(here) != content_key(observation((refresh, control("all", "Show all"))))
    assert content_key(here) != content_key(observation((refresh.model_copy(update={"checked": True}),)))


def test_a_document_holds_the_same_state_however_its_results_redraw() -> None:
    """The point of holding: a filter toggled on and off redraws the rows beneath it every time, so the page
    state is always one never seen, and only the committed values say the run has been here before."""
    box = control("stops", "Direct only", "checkbox", checked=False)

    def at(checked: bool, rows: str) -> Observation:
        return observation((box.model_copy(update={"checked": checked}), control("rows", rows))).model_copy(
            update={"document_key": "results"}
        )

    first_on = move(at(False, "0 results"), at(True, "7 results"))
    again_on = move(at(False, "12 results"), at(True, "5 results"))
    back_off = move(at(True, "5 results"), at(False, "12 results"))
    assert first_on is not None and again_on is not None and back_off is not None
    # The rows differ every time, so only the committed values can say the page has held this before.
    assert holding(first_on) == holding(again_on)
    assert holding(back_off) != holding(first_on)
