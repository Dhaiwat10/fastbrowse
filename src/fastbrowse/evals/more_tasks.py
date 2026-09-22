"""Tasks beyond the core suite, split into a dev set and a held-out set.

The core suite found the fixes it now scores well on, so it no longer says whether a change helps browsing in
general. These tasks cover skills it barely touches (pagination, frames, new windows, hover, script-rendered
pages, server-rendered forms) on sites the agent was never tuned against. Agent changes are iterated against
`DEV` only; `HELDOUT` is run before and after a round of changes and never debugged, so its score is the honest
measure of whether a round improved the agent or only its dev score. Each pair of tasks across the split
exercises the same skill.

`STRETCH_DEV` and `STRETCH_HELDOUT` are a harder split, kept because the two above now pass almost every run.
Each stretch task failed at least once in three runs of the unmodified agent, for a reason other than
infrastructure, when it was chosen; a candidate that passed every run was dropped. They cover a multi-step form
with a correction, a date relative to today, a list aggregated across pages, and a filter changed and then undone.

Truth is fixed by a practice site, fetched from a live API at run time as in `live_tasks`, or computed from the
date an attempt runs on.
"""

import re
from datetime import date, timedelta
from urllib.parse import unquote, urlparse

import httpx

from fastbrowse.evals.live_tasks import Category, Check, LiveTask, Outcome, Truth


async def _json(http: httpx.AsyncClient, url: str) -> object:
    response = await http.get(url, headers={"Accept": "application/json"})
    response.raise_for_status()
    return response.json()


async def _constant(value: object) -> object:
    return value


def _fixed(value: object) -> Truth:
    return lambda _: _constant(value)


async def _ruff_release(http: httpx.AsyncClient) -> object:
    body = await _json(http, "https://api.github.com/repos/astral-sh/ruff/releases/latest")
    assert isinstance(body, dict)
    return str(body["tag_name"])


async def _serde_version(http: httpx.AsyncClient) -> object:
    # crates.io refuses API requests that do not say who is asking.
    response = await http.get(
        "https://crates.io/api/v1/crates/serde", headers={"User-Agent": "fastbrowse-evals (github.com/agent-labs-dev)"}
    )
    response.raise_for_status()
    return str(response.json()["crate"]["max_stable_version"])


async def _httpx_requires(http: httpx.AsyncClient) -> object:
    body = await _json(http, "https://pypi.org/pypi/httpx/json")
    assert isinstance(body, dict)
    # ">=3.8" is answered as "3.8", "Python 3.8 or later", and so on: the version number is the fact.
    return str(body["info"]["requires_python"]).lstrip("<>=~! ")


def _flat(text: str) -> str:
    """Case, and the thousands separators and curly quotes a writer may add or drop, carry no meaning here."""
    text = text.casefold().translate({0x2019: "'", 0x201C: '"', 0x201D: '"'})
    return re.sub(r"(?<=\d),(?=\d{3})", "", text)


def _has(*needles: str) -> Check:
    """The answer names every one of the needles."""

    def check(outcome: Outcome, _: object) -> str | None:
        answer = _flat(outcome.answer or "")
        missing = [n for n in needles if _flat(n) not in answer]
        return f"answer lacks {missing}: {outcome.answer!r}" if missing else None

    return check


def _has_truth(outcome: Outcome, truth: object) -> str | None:
    return _has(str(truth))(outcome, truth)


def _exactly(needle: str) -> Check:
    """Case matters: the page's own capitals are the evidence that the answer was read, not echoed from the task."""

    def check(outcome: Outcome, _: object) -> str | None:
        return None if needle in (outcome.answer or "") else f"answer lacks {needle!r}: {outcome.answer!r}"

    return check


def _submitted(outcome: Outcome, truth: object) -> str | None:
    if outcome.final_url is not None and unquote(urlparse(outcome.final_url).path).rstrip("/") != "/post":
        return f"ended on {outcome.final_url}, not on the page the form posts to"
    return _has("large")(outcome, truth)


DEV: tuple[LiveTask, ...] = (
    LiveTask(
        "books-travel-priciest",
        "https://books.toscrape.com/",
        "Which is the most expensive book in the Travel category, and what does it cost?",
        _fixed(None),
        _has("A Year in Provence", "56.88"),
        Category.LOOKUP,
    ),
    LiveTask(
        "hockey-bruins-1990",
        "https://www.scrapethissite.com/pages/forms/",
        "How many games did the Boston Bruins win in the 1990 season?",
        _fixed(None),
        _has("44"),
        Category.LOOKUP,
    ),
    LiveTask(
        "oscars-2012",
        "https://www.scrapethissite.com/pages/ajax-javascript/",
        "Of the 2012 films listed here, which one won Best Picture?",
        _fixed(None),
        _has("Argo"),
        Category.LOOKUP,
    ),
    LiveTask(
        "dynamic-loading",
        "https://the-internet.herokuapp.com/dynamic_loading/2",
        "Start the example and tell me the text that appears when loading finishes.",
        _fixed(None),
        _has("Hello World"),
        Category.WIDGET,
    ),
    LiveTask(
        "nested-frames",
        "https://the-internet.herokuapp.com/nested_frames",
        "What text does the frame in the middle of the top row show?",
        _fixed(None),
        _exactly("MIDDLE"),
        Category.WIDGET,
    ),
    LiveTask(
        "hover-profile",
        "https://the-internet.herokuapp.com/hovers",
        "Which user name is revealed when you hover over the second profile picture?",
        _fixed(None),
        _has("user2"),
        Category.WIDGET,
    ),
    LiveTask(
        "ruff-release",
        "https://github.com/astral-sh/ruff",
        "What is the latest release of ruff on GitHub?",
        _ruff_release,
        _has_truth,
        Category.LOOKUP,
    ),
    LiveTask(
        "pizza-order",
        "https://httpbin.org/forms/post",
        "Order a large pizza with mushroom for Ada Lovelace, telephone 020 7946 0000, email ada@example.com, "
        "and submit it. Tell me which size the server received.",
        _fixed(None),
        _submitted,
        Category.CHECKOUT,
        authorize=True,
    ),
)

HELDOUT: tuple[LiveTask, ...] = (
    LiveTask(
        "books-mystery-cheapest",
        "https://books.toscrape.com/",
        "Which is the cheapest book in the Mystery category, and what does it cost?",
        _fixed(None),
        _has("Tastes Like Fear", "10.69"),
        Category.LOOKUP,
    ),
    LiveTask(
        "quotes-einstein-count",
        "https://quotes.toscrape.com/",
        "How many quotes by Albert Einstein are there across the whole site?",
        _fixed(None),
        _has("10"),
        Category.LOOKUP,
    ),
    LiveTask(
        "countries-mongolia",
        "https://www.scrapethissite.com/pages/simple/",
        "What population does this page list for Mongolia?",
        _fixed(None),
        _has("3086918"),
        Category.LOOKUP,
    ),
    LiveTask(
        "quotes-js-page2",
        "https://quotes.toscrape.com/js/",
        "Who wrote the first quote on the second page?",
        _fixed(None),
        _has("Marilyn Monroe"),
        Category.LOOKUP,
    ),
    LiveTask(
        "crates-serde",
        "https://crates.io/",
        "What is the latest stable version of the serde crate?",
        _serde_version,
        _has_truth,
        Category.LOOKUP,
    ),
    LiveTask(
        "new-window",
        "https://the-internet.herokuapp.com/windows",
        "Follow the link that opens a new window and tell me that window's heading.",
        _fixed(None),
        _has("New Window"),
        Category.WIDGET,
    ),
    LiveTask(
        "table-largest-due",
        "https://the-internet.herokuapp.com/tables",
        "In the first table, whose amount due is the largest?",
        _fixed(None),
        _has("Jason", "Doe"),
        Category.WIDGET,
    ),
    LiveTask(
        "httpx-requires-python",
        "https://pypi.org/",
        "What is the oldest Python version the latest httpx release supports?",
        _httpx_requires,
        _has_truth,
        Category.LOOKUP,
    ),
    LiveTask(
        "quotes-search",
        "https://quotes.toscrape.com/search.aspx",
        "Use the search form to find Albert Einstein's quote tagged success, and tell me what it says.",
        _fixed(None),
        _has("man of success", "man of value"),
        Category.LOOKUP,
    ),
)


# The stretch split. Form and date tasks are graded on the controls of the page the run ended on.


def _controls(outcome: Outcome) -> dict[str, str | None]:
    return {label.strip(): value for label, value in outcome.controls or ()}


async def _next_monday_range(_: httpx.AsyncClient) -> object:
    """Strictly after today: a run that lands on a Monday books the one seven days out, not the same day."""
    today = date.today()
    days_ahead = (0 - today.weekday()) % 7 or 7
    start = today + timedelta(days=days_ahead)
    end = start + timedelta(days=9)
    return {"start": start.isoformat(), "end": end.isoformat(), "nights": "9"}


def _date_range_check(outcome: Outcome, truth: object) -> str | None:
    assert isinstance(truth, dict)
    values = _controls(outcome)
    if not values:
        return _has(f"{truth['nights']} day")(outcome, truth)
    wrong = [
        f"{label}={values.get(label)!r}, expected {truth[key]!r}"
        for label, key in (("Start Date", "start"), ("End Date", "end"))
        if values.get(label) != truth[key]
    ]
    return "; ".join(wrong) or None


async def _next_month_first_friday(_: httpx.AsyncClient) -> object:
    today = date.today()
    first_of_next = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    friday = first_of_next + timedelta(days=(4 - first_of_next.weekday()) % 7)
    return {
        "mdy": f"{friday.month:02d}/{friday.day:02d}/{friday.year}",
        "day_name": "Friday",
        "month_name": f"{friday:%B}",
        "day": str(friday.day),
    }


def _first_friday_check(outcome: Outcome, truth: object) -> str | None:
    assert isinstance(truth, dict)
    picked = _controls(outcome).get("Click to pick a date:")
    if picked is not None:
        return None if picked == truth["mdy"] else f"date input = {picked!r}, expected {truth['mdy']!r}"
    return _has(truth["day_name"], truth["month_name"], truth["day"])(outcome, truth)


STRETCH_DEV: tuple[LiveTask, ...] = (
    LiveTask(
        "stretch-wizard-review",
        "https://qapracticehub.com/#practice-lab",
        "In the Automation Practice Lab section, fill out the Multi-Step Wizard: Full Name 'Ada Lovelace', "
        "Email 'ada.lovelace@example.com', City 'London', ZIP Code 'SW1A 1AA'. Review your details, submit, "
        "and tell me what the page says.",
        _fixed(None),
        _has("Wizard submitted successfully"),
        Category.CHECKOUT,
        authorize=True,
    ),
    LiveTask(
        "stretch-date-range-monday",
        "https://testautomationpractice.blogspot.com/",
        "Find Date Picker 3, the date range picker. Book a stay starting the next Monday that is strictly "
        "after today, for nine nights, then submit and tell me what the page reports the length of the stay "
        "as.",
        _next_monday_range,
        _date_range_check,
        Category.WIDGET,
    ),
    LiveTask(
        "stretch-books-nonfiction-five-star",
        "https://books.toscrape.com/catalogue/category/books/nonfiction_13/index.html",
        "Across every page of the Nonfiction category, which three five-star-rated books are the cheapest, "
        "and what does each cost?",
        _fixed(None),
        _has(
            "Agnostic: A Spirited Manifesto",
            "12.51",
            "Disrupted: My Misadventure in the Start-Up Bubble",
            "15.28",
            "Mother, Can You Not?",
            "16.89",
        ),
        Category.LOOKUP,
    ),
    LiveTask(
        "stretch-bstack-apple-google",
        "https://bstackdemo.com/",
        "Filter the product list to Apple and Google together. Then remove the Apple filter, so only Google "
        "remains. Sort by price lowest to highest, and tell me the two cheapest Google phones and their "
        "prices.",
        _fixed(None),
        _has("Pixel 2", "399", "Pixel 3", "599"),
        Category.WIDGET,
    ),
)

STRETCH_HELDOUT: tuple[LiveTask, ...] = (
    LiveTask(
        "stretch-wizard-correction",
        "https://lab.hakdogan.com/practice/form-multi-step/",
        "In the Live Interactive Form widget, fill First Name 'Priya Sharma', Email "
        "'priya.sharma@example.com', Address '221B Baker Street', City 'Manchester', Language 'Turkish', and "
        "check the QA newsletter box. Reach the Review step, then go back and correct the first name to "
        "'Priya Sharman' before continuing through Submit. Tell me what the confirmation says.",
        _fixed(None),
        _has("Priya Sharman", "submitted successfully"),
        Category.CHECKOUT,
        authorize=True,
    ),
    LiveTask(
        "stretch-calendar-first-friday",
        "https://practice.softwaretestingmentor.com/calendar/",
        "Using the jQuery UI Datepicker (the calendar popup, not the native date input), navigate to next "
        "month and select its first Friday. Tell me the date, day, and month it shows.",
        _next_month_first_friday,
        _first_friday_check,
        Category.WIDGET,
    ),
    LiveTask(
        "stretch-quotes-top-authors",
        "https://quotes.toscrape.com/",
        "Across every page of this site, which three authors have the most quotes attributed to them, and "
        "how many quotes does each have?",
        _fixed(None),
        _has("Albert Einstein", "10", "J.K. Rowling", "9", "Marilyn Monroe", "7"),
        Category.LOOKUP,
    ),
    LiveTask(
        "stretch-bstack-apple-samsung",
        "https://bstackdemo.com/",
        "Filter the product list to Apple and Samsung together, then remove the Apple filter so only Samsung "
        "remains. Sort by price highest to lowest, and tell me the three most expensive phones and their "
        "prices.",
        _fixed(None),
        _has("Galaxy S20 Ultra", "1399", "Galaxy Note 20 Ultra", "1299", "Galaxy S20+", "1199"),
        Category.WIDGET,
    ),
)
