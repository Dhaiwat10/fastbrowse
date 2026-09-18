"""Login values from a Bitwarden vault item, read through the `bw` CLI the user has unlocked.

The item's own saved URIs decide where it may be used: a login is released only for a start origin one
of them covers, the way Bitwarden's default base-domain match would autofill it.
"""

import subprocess
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError


class BitwardenError(RuntimeError):
    """The vault is locked, the item is missing, or it does not belong to the start origin."""


class _Uri(BaseModel):
    model_config = ConfigDict(extra="ignore")
    uri: str | None = None


class _Login(BaseModel):
    model_config = ConfigDict(extra="ignore")
    username: str | None = None
    password: str | None = None
    uris: list[_Uri] | None = None


class _Item(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    login: _Login | None = None


def _host(uri: str) -> str:
    # Vault URIs are often saved bare ("amazon.com"), which urlsplit reads as a path, not a host.
    host = (urlsplit(uri if "://" in uri else f"//{uri}").hostname or "").lower()
    return host.removeprefix("www.")


def covers(uri: str, origin: str) -> bool:
    """True when `origin`'s host is the URI's host or a subdomain of it."""
    saved, visited = _host(uri), _host(origin)
    return bool(saved) and (visited == saved or visited.endswith(f".{saved}"))


def login_values(item_json: str, origin: str) -> dict[str, str]:
    """The item's username and password, keyed by the secret names the agent sees."""
    try:
        item = _Item.model_validate_json(item_json)
    except ValidationError:
        # Pydantic's message quotes the input, which here holds the password.
        raise BitwardenError("bw returned something other than a vault item") from None
    if item.login is None:
        raise BitwardenError(f"Bitwarden item {item.name!r} is not a login")
    uris = [u.uri for u in item.login.uris or () if u.uri]
    if not any(covers(uri, origin) for uri in uris):
        raise BitwardenError(f"Bitwarden item {item.name!r} is not saved for {origin}")
    values = {"username": item.login.username, "password": item.login.password}
    return {name: value for name, value in values.items() if value}


def bitwarden_login(item: str, origin: str) -> dict[str, str]:
    """Read `item` (a name or id) from the unlocked vault; `BW_SESSION` must be in the environment."""
    try:
        done = subprocess.run(
            ["bw", "get", "item", item, "--nointeraction"], capture_output=True, text=True, timeout=30, check=False
        )
    except FileNotFoundError:
        raise BitwardenError("the Bitwarden CLI (bw) is not installed") from None
    if done.returncode != 0:
        # bw reports a locked vault and a missing item on stderr; neither carries a secret.
        raise BitwardenError(f"bw get item failed: {done.stderr.strip() or 'no output'}")
    return login_values(done.stdout, origin)
