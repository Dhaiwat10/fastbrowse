"""What an action visibly did, told back to the policy so that a no-op cannot pass for progress."""

import hashlib
import json
from dataclasses import dataclass

from fastbrowse.page import Control, Observation

_SHOWN = 4
_TEXT = 60

SETTING_ROLES = frozenset({"option", "checkbox", "radio", "switch", "menuitemradio", "menuitemcheckbox"})
"""Clicking one of these is choosing a value, so it should leave some value, selection or address changed."""


@dataclass(frozen=True, slots=True)
class Effect:
    summary: str
    set_something: bool
    """The address, or a surviving control's label, value, checked or selected state, changed."""


def state_key(observation: Observation) -> str:
    """Identify a page state by what can be done on it, ignoring text that changes on its own (clocks, ads)."""
    controls = sorted(
        json.dumps([c.role, c.label, c.context, c.value, c.checked, c.selected, c.expanded])
        for c in observation.controls
    )
    return hashlib.sha256(json.dumps([observation.url, controls]).encode()).hexdigest()


def _short(value: object) -> str:
    text = "empty" if value in (None, "") else str(value)
    return text if len(text) <= _TEXT else text[: _TEXT - 1] + "…"


def _listed(controls: list[Control]) -> str:
    count = f"{len(controls)} control{'' if len(controls) == 1 else 's'}"
    names = ", ".join(_short(c.label) for c in controls[:_SHOWN])
    return f"{count}: {names}" + (f" and {len(controls) - _SHOWN} more" if len(controls) > _SHOWN else "")


def effect(before: Observation, after: Observation) -> Effect:
    old = {c.id: c for c in before.controls}
    new = {c.id: c for c in after.controls}
    parts: list[str] = []
    navigated = before.url != after.url
    if navigated:
        parts.append(f"went to {_short(after.url)}")
    changes: list[str] = []
    for key, now in new.items():
        was = old.get(key)
        if was is None:
            continue
        for name in ("label", "value", "checked", "selected", "expanded"):
            a, b = getattr(was, name), getattr(now, name)
            if a != b:
                changes.append(f"{_short(was.label)} {name}: {_short(a)} -> {_short(b)}")
    setting = [c for c in changes if " expanded: " not in c]
    if changes:
        parts.append(
            "changed "
            + "; ".join(changes[:_SHOWN])
            + (f" and {len(changes) - _SHOWN} more" if len(changes) > _SHOWN else "")
        )
    shown = [c for key, c in new.items() if key not in old]
    hidden = [c for key, c in old.items() if key not in new]
    if shown:
        parts.append(f"showed {_listed(shown)}")
    if hidden:
        parts.append(f"removed {_listed(hidden)}")
    return Effect(summary="; ".join(parts) or "nothing visible changed", set_something=navigated or bool(setting))
