import pytest

from fastbrowse.browser.page import _capped
from fastbrowse.models import Operation
from fastbrowse.page import Control, pages_forward


def link(label: str, *, next_page: bool | None = None, i: int = 0) -> Control:
    return Control(
        id=f"l{i}",
        frame_id=None,
        role="link",
        label=label,
        operations=frozenset({Operation.CLICK}),
        href=f"/{i}",
        next_page=next_page,
    )


@pytest.mark.parametrize(
    "label", ["next", "Next", "Next page", "Next \u203a", "\u203a", "\u00bb", "  next\n", "Older posts", "More results"]
)
def test_labels_that_turn_the_page(label: str) -> None:
    assert pages_forward(link(label))


@pytest.mark.parametrize("label", ["Nextel", "Book 3", "previous", "Next steps for the team", "Home"])
def test_labels_that_do_not(label: str) -> None:
    assert not pages_forward(link(label))


def test_a_link_the_page_marks_rel_next_turns_the_page_whatever_it_says() -> None:
    assert pages_forward(link("Suivant", next_page=True))


def test_the_cap_keeps_the_first_controls_and_every_pager() -> None:
    controls = [link(f"Book {i}", i=i) for i in range(10)] + [link("next", i=10)]
    kept = _capped(controls, 4)
    assert [c.label for c in kept] == ["Book 0", "Book 1", "Book 2", "next"]


def test_the_cap_leaves_a_short_list_alone() -> None:
    controls = [link(f"Book {i}", i=i) for i in range(3)]
    assert _capped(controls, 5) == controls


def test_the_cap_keeps_document_order_and_never_more_than_the_limit_unless_pagers_alone_exceed_it() -> None:
    controls = [link("next", i=0), *(link(f"Book {i}", i=i) for i in range(1, 6)), link("next", i=6)]
    assert [c.id for c in _capped(controls, 3)] == ["l0", "l1", "l6"]
    assert len(_capped([link("next", i=i) for i in range(5)], 2)) == 5
