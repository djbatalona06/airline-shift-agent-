"""User configuration schema.

Validation is deliberately strict and happens at load time. A malformed
availability window that only surfaces at 3am — as a missed shift or, worse, a
wrong claim — is the failure mode this file exists to prevent.
"""

from __future__ import annotations

from datetime import date, time
from enum import Enum
from pathlib import Path
from typing import Any, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


class ClaimMode(str, Enum):
    NOTIFY_ONLY = "notify_only"
    CONFIRM = "confirm"
    AUTO = "auto"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Slot(_Base):
    """One recurring weekly availability window.

    `end` earlier than or equal to `start` means the window crosses midnight
    (e.g. a 22:00-06:00 night shift), not an error.
    """

    day: int
    start: time
    end: time

    @field_validator("day", mode="before")
    @classmethod
    def _parse_day(cls, v: Any) -> int:
        if isinstance(v, str):
            key = v.strip().lower()
            if key not in _WEEKDAYS:
                raise ValueError(f"unknown weekday {v!r}; use e.g. 'Monday' or 'Mon'")
            return _WEEKDAYS[key]
        if isinstance(v, int) and 0 <= v <= 6:
            return v
        raise ValueError(f"day must be a weekday name or 0-6 (Mon=0), got {v!r}")

    @property
    def crosses_midnight(self) -> bool:
        return self.end <= self.start


class Availability(_Base):
    timezone: str
    slots: tuple[Slot, ...] = ()
    excluded_dates: tuple[date, ...] = ()

    @field_validator("timezone")
    @classmethod
    def _known_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"unknown timezone {v!r}. Use an IANA name like 'America/New_York'."
            ) from exc
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class Rules(_Base):
    """Safety guards applied on top of raw availability.

    These are generic scheduling hygiene, NOT regulatory legality checking. We
    deliberately do not attempt FAA/FAR 117 duty-limit math or state nursing
    ratio rules: the portal enforces its own rules server-side and is
    authoritative, and a half-correct legality model here would be worse than
    none. Confirm-first claim mode keeps a human in the loop for the rest.
    """

    min_rest_hours: float = Field(default=10.0, ge=0, le=48)
    max_weekly_hours: float | None = Field(default=None, gt=0, le=168)
    max_shifts_per_week: int | None = Field(default=None, gt=0, le=21)

    # Guards against a stale portal listing or clock skew offering a shift that
    # has already started. 0 means "must simply be in the future"; raise it to
    # require notice before a shift begins.
    min_lead_minutes: int = Field(default=0, ge=0, le=10080)

    # If she is not legal for a shift the portal rejects it ("unable" / cancelled).
    # Retrying past this is pointless and starts to look like automated hammering.
    max_claim_attempts: int = Field(default=3, ge=1, le=20)

    # Only consider shifts flagged premium (FLICA's "Prem" column). Off by
    # default so portals without the concept are unaffected; when on, a shift
    # whose premium flag cannot be read is skipped rather than assumed premium.
    premium_only: bool = False

    # Consecutive failed logins before the agent stops trying. A changed
    # password must never turn into a retry loop that locks her account out.
    max_login_failures: int = Field(default=3, ge=1, le=10)


class HomeBase(_Base):
    """Domicile lock.

    The portal's open-time view has a base selector (ATL, DEN, MCO, …). Picking
    up a trip from the wrong base does not mean a slightly worse shift — it
    means being rostered out of a city she does not live in. This is the most
    damaging mistake the agent could make, so it is enforced as a hard gate
    rather than a preference.

    The agent must also never *change* the selector; the adapter reads whatever
    base the portal is already set to and this check verifies it.

    Disabled when `code` is unset, so non-airline portals are unaffected.
    """

    code: str | None = None

    @field_validator("code", mode="before")
    @classmethod
    def _normalise(cls, v: Any) -> Any:
        if v is None:
            return None
        cleaned = str(v).strip().upper()
        return cleaned or None

    @property
    def enabled(self) -> bool:
        return self.code is not None

    def allows(self, base: str | None) -> bool:
        """Fail closed: an unknown or missing base is refused, not assumed."""
        if not self.enabled:
            return True
        if not base:
            return False
        return str(base).strip().upper() == self.code


class Grades(_Base):
    """Position-class routing (FLICA A-E letter grades).

    Disabled by default — both lists empty means every shift is pursued as
    normal. This matters: a portal whose adapter does not populate a grade (a
    CNA shift board, say) must not have every shift silently demoted to
    notify-only. The feature only engages once `pursue` is configured.

    When enabled, anything not explicitly in `pursue` is notify-only. That is
    fail-closed by construction: an unparseable, missing, or newly-introduced
    grade alerts her instead of being claimed. A markup change on the portal
    must never promote a grade she did not ask for into something the agent
    goes after.
    """

    pursue: tuple[str, ...] = ()
    notify_only: tuple[str, ...] = ()

    @field_validator("pursue", "notify_only", mode="before")
    @classmethod
    def _normalise(cls, v: Any) -> Any:
        if isinstance(v, (list, tuple)):
            return tuple(str(x).strip().upper() for x in v if str(x).strip())
        return v

    @model_validator(mode="after")
    def _no_overlap(self) -> Self:
        clash = set(self.pursue) & set(self.notify_only)
        if clash:
            raise ValueError(f"grades cannot be both pursue and notify_only: {sorted(clash)}")
        return self

    @property
    def enabled(self) -> bool:
        return bool(self.pursue or self.notify_only)

    def should_pursue(self, grade: str | None) -> bool:
        if not self.enabled:
            return True
        if grade is None:
            return False
        return grade.strip().upper() in set(self.pursue)


class QuietHours(_Base):
    start: time
    end: time


class Poll(_Base):
    interval_seconds: float = Field(default=45.0, ge=5, le=3600)
    jitter_seconds: float = Field(default=15.0, ge=0, le=600)
    quiet_hours: QuietHours | None = None

    @model_validator(mode="after")
    def _jitter_sane(self) -> Self:
        if self.jitter_seconds > self.interval_seconds:
            raise ValueError("jitter_seconds must not exceed interval_seconds")
        return self


class Notify(_Base):
    telegram_chat_id: int | None = None
    confirm_timeout_minutes: int = Field(default=10, ge=1, le=240)
    daily_digest_hour: int | None = Field(default=8, ge=0, le=23)


class Portal(_Base):
    adapter: str
    base_url: str | None = None
    username: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class LlmProvider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI_COMPATIBLE = "openai_compatible"


class ChatMode(str, Enum):
    PROXY = "proxy"
    BROWSER = "browser"


class Llm(_Base):
    """The chat assistant on the dashboard. Off unless a key is stored.

    No key material here. Like the portal password, the key lives in the OS
    keychain and this section only says which model to point it at — a config
    file gets copied, pasted into a support thread and committed by accident far
    more often than a keychain entry does.

    `provider: openai_compatible` with a localhost `base_url` is the option that
    keeps the privacy promise in docs/SECURITY.md intact: nothing about her
    roster leaves the machine. Anthropic is the default because it is the one
    that works without installing anything else.
    """

    provider: LlmProvider = LlmProvider.ANTHROPIC
    model: str = "claude-opus-5"

    # Required for openai_compatible (Ollama, llama.cpp, LM Studio), ignored by
    # the Anthropic client, which knows its own endpoint.
    base_url: str | None = None

    # Chat turns are short and someone is waiting on them, so this trades depth
    # for latency. Raise it for a model that has to reason about a whole roster.
    effort: str = Field(default="medium", pattern="^(low|medium|high|xhigh|max)$")
    max_tokens: int = Field(default=4096, ge=256, le=64000)

    # proxy  - the loopback server holds the key and calls the model. Full tools.
    # browser- the page holds the key and calls the model itself. No tools, since
    #          a file:// page has no way to reach the database.
    mode: ChatMode = ChatMode.PROXY

    # Ceiling on how much of her data one answer may pull in. Guards both the
    # token bill and the blast radius of a prompt injection in a shift title.
    max_shifts_in_context: int = Field(default=60, ge=1, le=200)

    @model_validator(mode="after")
    def _endpoint_present(self) -> Self:
        if self.provider is LlmProvider.OPENAI_COMPATIBLE and not self.base_url:
            raise ValueError(
                "llm.base_url is required for provider 'openai_compatible' "
                "(for example http://127.0.0.1:11434/v1 for Ollama)"
            )
        return self

    @property
    def needs_key(self) -> bool:
        """A local endpoint on loopback needs no key; a hosted one always does."""
        if self.provider is LlmProvider.ANTHROPIC:
            return True
        host = (self.base_url or "").lower()
        return not any(marker in host for marker in ("127.0.0.1", "localhost", "[::1]"))


class UserConfig(_Base):
    name: str
    portal: Portal
    availability: Availability
    rules: Rules = Field(default_factory=Rules)
    home_base: HomeBase = Field(default_factory=HomeBase)
    grades: Grades = Field(default_factory=Grades)
    poll: Poll = Field(default_factory=Poll)
    notify: Notify = Field(default_factory=Notify)
    llm: Llm = Field(default_factory=Llm)
    claim_mode: ClaimMode = ClaimMode.CONFIRM
    healthcheck_url: str | None = None
    dry_run: bool = True

    @classmethod
    def load(cls, path: str | Path) -> UserConfig:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)
