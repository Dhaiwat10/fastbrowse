"""Gates code owns: irreversible actions need authorization, and secrets never leave their origins or reach logs.

Models only ever see secret names. Values are resolved here, at dispatch time, for the origin being acted on.
"""

import json
import re
from collections.abc import Mapping
from urllib.parse import quote, quote_plus, urlsplit

from fastbrowse.jev import NoulQuestion
from fastbrowse.models import Operation, SecretRef, SecretResolver
from fastbrowse.page import Control

# A link can still commit through its label ("Delete"), so the words are checked on every control.
_IRREVERSIBLE_WORDS = re.compile(
    r"\b(buy|purchase|pay|checkout|order|confirm|submit|send|delete|remove|erase|wipe|destroy|transfer|"
    r"book|reserve|subscribe|unsubscribe|cancel|publish|post|donate|sign up|register|agree|accept|withdraw|"
    r"close account|deactivate|archive|merge|approve)\b",
    re.IGNORECASE,
)
_DISPATCHING = frozenset({Operation.CLICK, Operation.ENTER})


def may_be_irreversible(operation: Operation, control: Control | None) -> bool:
    """Whether Jev is asked before this dispatches. Only plain navigation is exempt.

    A label list cannot be complete ("Place your order", "Erase"), so every button is asked about: a button
    runs script, and script can commit anything. A link with an href navigates, which is exempt unless its
    label says otherwise.
    """
    if operation not in _DISPATCHING or control is None:
        return False
    if _IRREVERSIBLE_WORDS.search(control.label) or control.input_type == "submit":
        return True
    if operation is Operation.ENTER:
        return control.submit_semantics is not None
    return control.href is None


def irreversible_question(task: str, operation: Operation, control: Control) -> NoulQuestion:
    return NoulQuestion(
        instructions=(
            f"The agent is about to {operation.value} the element labelled {control.label!r} while doing this task: "
            f"{task}\n"
            + (f"It sits under {control.context!r} on the page.\n" if control.context else "")
            + (f"Enter submits this form: {control.submit_semantics}\n" if operation is Operation.ENTER else "")
            + "Would doing so commit something that cannot be undone, such as spending money, sending a "
            "message, submitting an application, or deleting or publishing data?"
        ),
        true="It commits an irreversible or externally visible change.",
        false="It only navigates, filters, reveals or edits a draft that can still be changed.",
    )


_DEFAULT_PORTS = {"http": 80, "https": 443}
# A declared origin whose host starts with this covers that host and everything under it.
_WILDCARD = "*."


def origin_of(url: str) -> str:
    """The web origin, with a port the scheme implies dropped rather than carried.

    `https://shop.example.com` and `https://shop.example.com:443` are one origin, and a secret declared for one
    must be typed on the other: a browser writes the port back either way after a navigation, and comparing the
    two as strings dropped the secret and ended the run at needs_login. Credentials in the URL are dropped too,
    so `https://user@host` cannot pass itself off as another origin.
    """
    parts = urlsplit(url.strip())
    try:
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        # A port that is not a number: not an origin this can normalize, and never one a secret is declared for.
        return f"{parts.scheme}://{parts.netloc}".lower()
    if not host:
        return f"{parts.scheme}://{parts.netloc}".lower()
    if port == _DEFAULT_PORTS.get(parts.scheme.lower()):
        port = None
    return f"{parts.scheme.lower()}://{host}" + (f":{port}" if port else "")


def secret_allowed(ref: SecretRef, origin: str) -> bool:
    """Whether this secret may be typed on `origin`.

    A declared origin is an exact one, or one whose host starts with `*.`, for a login that is the same login
    across a site's hosts: `https://*.example.com` covers `www.example.com`, `accounts.example.com` and
    `example.com` itself. The scheme and port must still match, and the wildcard only ever stands for whole
    labels, so it does not cover `example.com.evil.test`, which merely ends with the same letters.
    """
    here = urlsplit(origin_of(origin))
    host = here.hostname or ""
    for declared in ref.origins:
        pattern = urlsplit(origin_of(declared.rstrip("/")))
        # The scheme and port are never wildcarded: a secret for https is not for http, whatever the host.
        if (pattern.scheme, pattern.port) != (here.scheme, here.port):
            continue
        covered = pattern.hostname or ""
        if covered == host:
            return True
        if not covered.startswith(_WILDCARD) or not (suffix := covered[len(_WILDCARD) :]):
            continue
        if host == suffix or host.endswith(f".{suffix}"):
            return True
    return False


async def resolve_secret(resolver: SecretResolver, name: str, origin: str) -> str | None:
    """Only a secret declared for this origin resolves; anything else is treated as missing."""
    ref = next((r for r in resolver.available() if r.name == name), None)
    if ref is None or not secret_allowed(ref, origin):
        return None
    return await resolver.resolve(name, origin)


class ScopedSecrets:
    """Secret values held in this process, each usable only on one origin: the `SecretResolver` for a run.

    The origin may be a `*.` pattern, and the same rule decides here as everywhere: a resolver that answered
    on a wider origin than it declared would put the gate in two places with two answers.
    """

    def __init__(self, values: Mapping[str, str], origin: str) -> None:
        self._values = dict(values)
        self._origin = origin

    def available(self) -> tuple[SecretRef, ...]:
        return tuple(SecretRef(name=name, origins=(self._origin,)) for name in self._values)

    async def resolve(self, name: str, origin: str) -> str | None:
        if name not in self._values or not secret_allowed(SecretRef(name=name, origins=(self._origin,)), origin):
            return None
        return self._values[name]


class Redactor:
    """Replaces every resolved secret value with its name in anything written out of the process."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def register(self, name: str, value: str) -> None:
        # Pages and logs carry a value encoded as often as raw: in a query string, or escaped inside JSON.
        forms = {
            value,
            quote(value, safe=""),
            quote_plus(value),
            json.dumps(value)[1:-1],
            json.dumps(value, ensure_ascii=False)[1:-1],
        }
        for form in forms:
            if form:
                self._values[form] = name

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
