"""Job-agnostic domain types.

Nothing here may reference a specific employer, portal, or industry. An airline
trip and a CNA hospital shift both reduce to the same shape: an identified block
of time you either can or cannot take. Portal-specific detail lives in `meta`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any


class AuthState(str, Enum):
    OK = "ok"
    NEEDS_HUMAN = "needs_human"
    FAILED = "failed"


class ClaimOutcome(str, Enum):
    CLAIMED = "claimed"
    LOST_RACE = "lost_race"
    REJECTED = "rejected"
    ERROR = "error"


class MatchVerdict(str, Enum):
    MATCH = "match"
    OUTSIDE_AVAILABILITY = "outside_availability"
    EXCLUDED_DATE = "excluded_date"
    CONFLICTS_ASSIGNED = "conflicts_assigned"
    INSUFFICIENT_REST = "insufficient_rest"
    EXCEEDS_WEEKLY_CAP = "exceeds_weekly_cap"
    ALREADY_SEEN = "already_seen"
    TOO_SOON = "too_soon"
    GRADE_NOTIFY_ONLY = "grade_notify_only"
    MAX_ATTEMPTS_REACHED = "max_attempts_reached"
    WRONG_BASE = "wrong_base"
    NOT_PREMIUM = "not_premium"


@dataclass(frozen=True, slots=True)
class Shift:
    """An offered or assigned block of work.

    `start`/`end` MUST be timezone-aware. Naive datetimes are rejected at
    construction rather than silently misinterpreted, because a naive datetime
    from a portal in a different timezone is the single most likely source of a
    wrong claim.
    """

    id: str
    start: datetime
    end: datetime
    title: str = ""
    location: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (("start", self.start), ("end", self.end)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"Shift.{name} must be timezone-aware, got {value!r}")
        if self.end <= self.start:
            raise ValueError(f"Shift.end must be after start (got {self.start} -> {self.end})")

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def overlaps(self, other: Shift) -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True, slots=True)
class AuthResult:
    state: AuthState
    detail: str = ""
    challenge_url: str | None = None

    @property
    def ok(self) -> bool:
        return self.state is AuthState.OK


@dataclass(frozen=True, slots=True)
class ClaimResult:
    outcome: ClaimOutcome
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is ClaimOutcome.CLAIMED


@dataclass(frozen=True, slots=True)
class MatchResult:
    shift: Shift
    verdict: MatchVerdict
    detail: str = ""

    @property
    def matched(self) -> bool:
        return self.verdict is MatchVerdict.MATCH
