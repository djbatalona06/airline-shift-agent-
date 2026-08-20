"""Structured actions a vision model can take against a browser page.

Every step of `vision_loop.run_vision_loop` asks a vision model "what do I do
next", and the reply has to become something `Page.mouse`/`Page.keyboard` can
execute. This module is the seam: `parse_action` turns a model's raw reply into
one validated `Action`, or raises. Pure function, no I/O - this is the main
CI-tested surface for the vision-response schema.

How much the validation actually buys, stated precisely rather than implied:

* `key` is checked against an allow-list, because it reaches
  `page.keyboard.press()`, which interprets its argument as a chord. An
  unconstrained string there is a way to reach browser-level shortcuts
  (devtools, downloads, navigation) rather than the page the loop is looking at.
* `x`/`y` are bounded to a plausible viewport.
* `text` is **shape-validated only** - it is free text typed into whatever has
  focus, and nothing here can know whether that is the right field. The loop is
  therefore only ever pointed at a page where that is acceptable; see
  `bench_recaptcha.py` and docs/FRICTION_TOOLKIT.md's boundary section.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ActionType(str, Enum):
    CLICK = "click"
    TYPE = "type"
    KEY = "key"
    WAIT = "wait"
    DONE = "done"
    FAIL = "fail"


class ActionParseError(ValueError):
    """Raised when a vision model's reply cannot be turned into an Action."""


# Keys the loop is willing to press, as Playwright names them. Everything a
# challenge page plausibly needs to be driven with, and nothing that reaches the
# browser chrome around it.
ALLOWED_KEYS = frozenset(
    {
        "Enter", "Tab", "Escape", "Backspace", "Delete", "Insert", "Space",
        "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
        "Home", "End", "PageUp", "PageDown",
    }
    | {f"F{n}" for n in range(1, 13)}
)

# Only Shift composes with the above. Control/Meta/Alt chords are the ones that
# reach the browser rather than the page, so they are refused outright.
ALLOWED_MODIFIERS = frozenset({"Shift"})

# Guards against a model returning a coordinate off in the millions, which
# Playwright would either reject noisily or clamp silently.
MAX_COORDINATE = 10_000


def validate_key(key: str) -> str:
    """Normalise and check one key name, or raise ValueError.

    Accepts a single printable character (typed as itself), one allow-listed
    named key, or `Shift+<allowed>`.
    """
    cleaned = (key or "").strip()
    if not cleaned:
        raise ValueError("key must not be empty")

    parts = [part.strip() for part in cleaned.split("+")]
    modifiers, base = parts[:-1], parts[-1]

    for modifier in modifiers:
        if modifier not in ALLOWED_MODIFIERS:
            raise ValueError(
                f"modifier {modifier!r} is not allowed; permitted: {sorted(ALLOWED_MODIFIERS)}"
            )
    if len(base) == 1 and base.isprintable():
        return cleaned
    if base not in ALLOWED_KEYS:
        raise ValueError(f"key {base!r} is not in the allow-list")
    return cleaned


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ActionType
    x: int | None = Field(default=None, ge=0, le=MAX_COORDINATE)
    y: int | None = Field(default=None, ge=0, le=MAX_COORDINATE)
    text: str | None = None
    key: str | None = None
    reason: str = ""

    @model_validator(mode="after")
    def _check_required_fields(self) -> Self:
        if self.type is ActionType.CLICK and (self.x is None or self.y is None):
            raise ValueError("click requires x and y")
        if self.type is ActionType.TYPE and not self.text:
            raise ValueError("type requires text")
        if self.type is ActionType.KEY:
            if not self.key:
                raise ValueError("key requires key")
            validate_key(self.key)
        return self


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def parse_action(raw_model_reply: str) -> Action:
    """Extract and validate one JSON action from a vision model's text reply.

    Models routinely wrap JSON in prose or a markdown fence even when told
    not to ("Here's what I'll do:\n```json\n{...}\n```"). This tolerates
    that rather than failing the whole loop over formatting - it takes the
    first `{...}` block found and validates it against `Action`.
    """
    match = _JSON_OBJECT.search(raw_model_reply)
    if not match:
        raise ActionParseError(f"no JSON object found in reply: {raw_model_reply!r}")
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ActionParseError(f"invalid JSON in reply: {exc}") from exc
    try:
        return Action.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError, deliberately caught broadly
        raise ActionParseError(f"reply did not match the action schema: {exc}") from exc
