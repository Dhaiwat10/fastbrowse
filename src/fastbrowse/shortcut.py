"""A direct address for the task on the start site, proposed while the start page loads.

Most steps of a lookup only move between pages whose addresses follow from the task: a package page, a
repository, a site's search results for the task's query. Skim (Wong et al., 2026) found two thirds of
web-agent steps are navigation of this kind and cut median latency by a third by synthesizing the
destination instead of clicking to it. Here a small model proposes one address, code accepts it only on
the start page's own origin, and the start page stays one BACK away when the guess is not useful.
"""

from urllib.parse import urlsplit

from pydantic import Field

from fastbrowse.llm import Generation, LLMClient, Message
from fastbrowse.models import Frozen, LLMPurpose
from fastbrowse.safety import origin_of
from fastbrowse.telemetry import Ledger


class Shortcut(Frozen):
    url: str | None = Field(
        description=(
            "An absolute URL on the start page's site that shows the page where this task's answer is or its work "
            "happens, built from the task alone. Null unless you are confident the site serves that exact address."
        )
    )


_INSTRUCTIONS = Message(
    role="system",
    content=(
        "# Shortcut\nA browser agent is about to open the start page and click its way to what the task needs. "
        "If the site has a well-known address for that destination, give it so the agent can go straight there: "
        "a package, repository or article page whose address follows from names in the task, or the site's own "
        "search results URL with the task's query when the task asks to search.\n\n"
        "Give null when the destination depends on anything you cannot see: a sign-in, a form to fill, a cart, "
        "the current state of a listing, or an address you would have to guess. A wrong address costs a wasted "
        "page load, so prefer null to a guess.\n\n"
        "# Trust\nThe task is from the user. Stay on the start page's site."
    ),
)


async def propose_shortcut(
    llm: LLMClient, task: str, start: str, *, ledger: Ledger | None = None
) -> Generation[Shortcut]:
    return await llm.generate(
        LLMPurpose.SHORTCUT,
        [_INSTRUCTIONS, Message(role="user", content=f"# Task\n{task}\n\n# Start page\n{start}")],
        Shortcut,
        max_output_tokens=200,
        ledger=ledger,
    )


def accept(proposed: str | None, start: str) -> str | None:
    """The proposal if it is a different page on the start page's own origin, which is the only scope the task
    and its secrets were given; anything else is dropped."""
    if proposed is None:
        return None
    parts = urlsplit(proposed)
    if parts.scheme not in {"http", "https"} or origin_of(proposed) != origin_of(start):
        return None
    if proposed.rstrip("/") == start.rstrip("/"):
        return None
    return proposed
