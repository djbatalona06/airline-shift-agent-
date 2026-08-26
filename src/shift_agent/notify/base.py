"""Notification transport interface.

Kept abstract so the poller can be exercised end-to-end with no Telegram token,
no network, and no chat account — see `console.py`. The confirm-first claim flow
means this interface carries a real decision (`offer`), not just messages, so it
has to be testable in isolation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import ClaimResult, MatchResult, Shift


class Notifier(ABC):
    @abstractmethod
    async def info(self, text: str) -> None:
        """Low-priority status. May be batched or suppressed by the transport."""

    @abstractmethod
    async def alert(self, text: str) -> None:
        """Something needs attention but is not blocking."""

    @abstractmethod
    async def needs_human(self, reason: str, url: str | None = None) -> None:
        """A captcha/MFA challenge blocks progress; the user must intervene."""

    @abstractmethod
    async def offer(self, shift: Shift, timeout_minutes: int) -> bool:
        """Present a matched shift and wait for a decision.

        Returns True only on an explicit confirmation. A timeout MUST return
        False — silence is never consent, and a stale yes hours later could
        claim something the user no longer wants.
        """

    @abstractmethod
    async def claim_outcome(self, shift: Shift, result: ClaimResult, dry_run: bool) -> None:
        """Report the result of a claim attempt."""

    async def system(self, text: str) -> None:
        """The agent's own health, not a shift.

        Concrete rather than abstract so existing notifiers keep working: the
        default routes to `alert`, which is the right severity for "monitoring
        has stopped". A transport can override to mark these differently.
        """
        await self.alert(text)

    async def digest(self, lines: list[str]) -> None:
        await self.info("\n".join(lines))

    async def close(self) -> None:
        return None


def describe(shift: Shift, tz=None) -> str:
    start = shift.start.astimezone(tz) if tz else shift.start
    end = shift.end.astimezone(tz) if tz else shift.end
    hours = shift.duration.total_seconds() / 3600
    parts = [f"{start:%a %d %b %H:%M} - {end:%H:%M} ({hours:.1f}h)"]
    if shift.title:
        parts.append(shift.title)
    if shift.location:
        parts.append(shift.location)
    return " | ".join(parts)


def describe_match(result: MatchResult, tz=None) -> str:
    text = describe(result.shift, tz)
    return f"{text}\n{result.detail}" if result.detail else text
