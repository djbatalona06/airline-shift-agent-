"""Fixture-driven adapter.

Exists so the full pipeline — poll, evaluate, offer, claim, persist — can be run
and tested with no portal, no credentials, and no network. It is also the
template to copy when writing a real adapter, and the harness for reproducing a
production misbehaviour as a test case.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import AuthResult, AuthState, ClaimOutcome, ClaimResult, Shift
from .base import PortalAdapter, register


def _parse_shift(raw: dict[str, Any], default_tz) -> Shift:
    def when(value: Any) -> datetime:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return dt.replace(tzinfo=default_tz) if dt.tzinfo is None else dt

    return Shift(
        id=str(raw["id"]),
        start=when(raw["start"]),
        end=when(raw["end"]),
        title=raw.get("title", ""),
        location=raw.get("location"),
        meta=raw.get("meta", {}),
    )


@register("mock")
class MockAdapter(PortalAdapter):
    def __init__(
        self,
        config,
        http=None,
        secrets: dict[str, Any] | None = None,
        *,
        open_shifts: list[Shift] | None = None,
        assigned: list[Shift] | None = None,
        auth_state: AuthState | None = None,
        claim_outcome: ClaimOutcome | None = None,
    ) -> None:
        super().__init__(config, http, secrets or {})
        opts = config.portal.options
        tz = config.availability.tz

        self._open = (
            open_shifts
            if open_shifts is not None
            else [_parse_shift(r, tz) for r in opts.get("open_shifts", [])]
        )
        self._assigned = (
            assigned
            if assigned is not None
            else [_parse_shift(r, tz) for r in opts.get("assigned", [])]
        )
        self._auth_state = auth_state or AuthState(opts.get("auth", "ok"))
        self._claim_outcome = claim_outcome or ClaimOutcome(opts.get("claim_outcome", "claimed"))

        self.login_calls = 0
        self.claim_calls: list[str] = []
        self._authed = self._auth_state is AuthState.OK

    async def is_authenticated(self) -> bool:
        return self._authed

    async def login(self) -> AuthResult:
        self.login_calls += 1
        if self._auth_state is AuthState.NEEDS_HUMAN:
            return AuthResult(
                AuthState.NEEDS_HUMAN,
                "simulated captcha",
                challenge_url="https://example.invalid/challenge",
            )
        if self._auth_state is AuthState.FAILED:
            return AuthResult(AuthState.FAILED, "simulated bad credentials")
        self._authed = True
        return AuthResult(AuthState.OK)

    async def fetch_open_shifts(self) -> list[Shift]:
        return list(self._open)

    async def fetch_my_schedule(self) -> list[Shift]:
        return list(self._assigned)

    async def claim(self, shift_id: str) -> ClaimResult:
        self.claim_calls.append(shift_id)
        if self._claim_outcome is ClaimOutcome.CLAIMED:
            self._open = [s for s in self._open if s.id != shift_id]
            self._assigned = [*self._assigned, *(s for s in self._open if s.id == shift_id)]
            return ClaimResult(ClaimOutcome.CLAIMED, f"claimed {shift_id}")
        return ClaimResult(self._claim_outcome, f"simulated {self._claim_outcome.value}")
