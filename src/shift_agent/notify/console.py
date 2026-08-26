"""Console notifier — dry runs, tests, and first-boot before Telegram is linked.

`auto_confirm` exists for tests and for the week-long dry run described in the
plan. It defaults to False so that an operator who wires this up in a real
process cannot accidentally get silent auto-claiming from the fallback
transport.
"""

from __future__ import annotations

import sys
from datetime import tzinfo

from ..models import ClaimResult, Shift
from .base import Notifier, describe


class ConsoleNotifier(Notifier):
    def __init__(self, tz: tzinfo | None = None, auto_confirm: bool = False) -> None:
        self.tz = tz
        self.auto_confirm = auto_confirm
        self.sent: list[tuple[str, str]] = []

    def _emit(self, kind: str, text: str) -> None:
        self.sent.append((kind, text))
        print(f"[{kind}] {text}", file=sys.stderr, flush=True)

    async def info(self, text: str) -> None:
        self._emit("info", text)

    async def alert(self, text: str) -> None:
        self._emit("alert", text)

    async def needs_human(self, reason: str, url: str | None = None) -> None:
        self._emit("needs-human", f"{reason}{f' -> {url}' if url else ''}")

    async def system(self, text: str) -> None:
        self._emit("system", text)

    async def offer(self, shift: Shift, timeout_minutes: int) -> bool:
        self._emit("offer", describe(shift, self.tz))
        return self.auto_confirm

    async def claim_outcome(self, shift: Shift, result: ClaimResult, dry_run: bool) -> None:
        prefix = "DRY-RUN " if dry_run else ""
        self._emit("claim", f"{prefix}{result.outcome.value}: {describe(shift, self.tz)}")
