import pytest

from fastbrowse.models import Operation
from fastbrowse.page import Control
from fastbrowse.safety import Redactor, may_be_irreversible


def control(label: str, *, role: str = "button", href: str | None = None, input_type: str | None = None) -> Control:
    return Control(
        id="c",
        frame_id=None,
        role=role,
        label=label,
        operations=frozenset({Operation.CLICK}),
        href=href,
        input_type=input_type,
    )


@pytest.mark.parametrize(
    ("target", "asked"),
    [
        # No keyword and no submit type: a button is still asked about, because its script can commit anything.
        (control("Place your order"), True),
        (control("Erase"), True),
        (control("Next"), True),
        (control("Search", input_type="submit"), True),
        (control("Delete account", role="link", href="/account/delete"), True),
        (control("Documentation", role="link", href="/docs"), False),
    ],
)
def test_every_button_is_asked_about_and_plain_navigation_is_not(target: Control, asked: bool) -> None:
    assert may_be_irreversible(Operation.CLICK, target) is asked


def test_a_secret_is_caught_in_every_encoding_a_page_or_log_carries_it_in() -> None:
    redactor = Redactor()
    redactor.register("password", 'p@ss w"ord')
    for text in ('p@ss w"ord', "p%40ss%20w%22ord", "p%40ss+w%22ord", '{"v": "p@ss w\\"ord"}'):
        assert redactor.reveals(text), text
        assert "ss" not in redactor.redact(text).replace("[secret:password]", ""), text
