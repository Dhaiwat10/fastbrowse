"""Gates code owns: irreversible actions need authorization, and secrets never leave their origins or reach logs.

Models only ever see secret names. Values are resolved here, at dispatch time, for the origin being acted on.
"""

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from fastbrowse.jev import NoulQuestion
from fastbrowse.models import Authorization, Operation, SecretRef, SecretResolver
from fastbrowse.page import Control

# Deliberately broad: a false flag costs one extra Jev question, a missed one can buy something.
_IRREVERSIBLE_WORDS = re.compile(
    r"\b(buy|purchase|pay|checkout|place order|order now|confirm|submit|send|delete|remove|destroy|transfer|"
    r"book|reserve|subscribe|unsubscribe|cancel|publish|post|donate|sign up|register|agree|accept|withdraw|"
    r"close account|deactivate|archive|merge|approve)\b",
    re.IGNORECASE,
)
_DISPATCHING = frozenset({Operation.CLICK, Operation.ENTER})


def may_be_irreversible(operation: Operation, control: Control | None) -> bool:
    """Cheap first pass; a flagged action is then asked about concretely before it can run."""
    if operation not in _DISPATCHING or control is None:
        return False
    return (
        bool(_IRREVERSIBLE_WORDS.search(control.label))
        or control.input_type == "submit"
        or (operation is Operation.ENTER and control.submit_semantics is not None)
    )


def irreversible_question(task: str, operation: Operation, control: Control) -> NoulQuestion:
    return NoulQuestion(
        instructions=(
            f"The agent is about to {operation.value} the element labelled {control.label!r} while doing this task: "
            f"{task}\n"
            + (f"Enter submits this form: {control.submit_semantics}\n" if operation is Operation.ENTER else "")
            + "Would doing so commit something that cannot be undone, such as spending money, sending a "
            "message, submitting an application, or deleting or publishing data?"
        ),
        true="It commits an irreversible or externally visible change.",
        false="It only navigates, filters, reveals or edits a draft that can still be changed.",
    )


def is_authorized(authorization: Authorization) -> bool:
    return authorization.irreversible_actions


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def secret_allowed(ref: SecretRef, origin: str) -> bool:
    return origin.lower() in {o.lower().rstrip("/") for o in ref.origins}


async def resolve_secret(resolver: SecretResolver, name: str, origin: str) -> str | None:
    """Only a secret declared for this origin resolves; anything else is treated as missing."""
    ref = next((r for r in resolver.available() if r.name == name), None)
    if ref is None or not secret_allowed(ref, origin):
        return None
    return await resolver.resolve(name, origin)


class Redactor:
    """Replaces every resolved secret value with its name in anything written out of the process."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def register(self, name: str, value: str) -> None:
        if value:
            self._values[value] = name

    def redact(self, text: str) -> str:
        # Longest first, so a secret containing another secret is replaced whole.
        for value in sorted(self._values, key=len, reverse=True):
            text = text.replace(value, f"[secret:{self._values[value]}]")
        return text

    def mask(self, text: str) -> str:
        """Blank secret values at equal length, so offsets into the text (capture blocks) stay valid."""
        for value in sorted(self._values, key=len, reverse=True):
            text = text.replace(value, "•" * len(value))
        return text

    def reveals(self, text: str) -> bool:
        return any(value in text for value in self._values)

    def redact_all(self, texts: Iterable[str]) -> tuple[str, ...]:
        return tuple(self.redact(t) for t in texts)
