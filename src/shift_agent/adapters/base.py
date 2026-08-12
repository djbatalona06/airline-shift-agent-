"""The one interface a new job/portal must implement.

Everything above this line in the stack — scheduling, polling, notification,
storage, deployment — is job-agnostic. Adding a second job (a CNA portal, a
different airline) should mean writing exactly one subclass of `PortalAdapter`
and nothing else.

Auth strategy: `login()` may drive a real browser, because login is rare and
often JS-heavy. `fetch_open_shifts()` is called constantly and should use the
cheap HTTP client wherever the portal allows it. Adapters that cannot reuse
cookies outside the browser may ignore `http` and drive Playwright throughout,
at roughly 10x the memory and latency.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, ClassVar

import httpx

from ..config import UserConfig
from ..models import AuthResult, ClaimResult, Shift

_REGISTRY: dict[str, type["PortalAdapter"]] = {}


def register(name: str) -> Callable[[type["PortalAdapter"]], type["PortalAdapter"]]:
    def decorate(cls: type[PortalAdapter]) -> type[PortalAdapter]:
        if name in _REGISTRY:
            raise ValueError(f"adapter {name!r} already registered by {_REGISTRY[name]!r}")
        cls.adapter_name = name
        _REGISTRY[name] = cls
        return cls

    return decorate


def get_adapter(name: str) -> type[PortalAdapter]:
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none registered)"
        raise LookupError(f"unknown adapter {name!r}. Known adapters: {known}") from None


def available_adapters() -> list[str]:
    return sorted(_REGISTRY)


class PortalAdapter(ABC):
    adapter_name: ClassVar[str] = ""

    def __init__(self, config: UserConfig, http: httpx.AsyncClient, secrets: dict[str, Any]):
        self.config = config
        self.http = http
        self.secrets = secrets

    @abstractmethod
    async def is_authenticated(self) -> bool:
        """Cheap liveness probe. Must not perform a full login."""

    @abstractmethod
    async def login(self) -> AuthResult:
        """Establish a session.

        Return `AuthState.NEEDS_HUMAN` with a `challenge_url` when blocked by a
        captcha, MFA prompt, or anything else requiring the user. Do not attempt
        to defeat such a challenge; the poller will hand off to the human.
        """

    @abstractmethod
    async def fetch_open_shifts(self) -> list[Shift]:
        """Shifts currently offered. Called on every poll — keep it cheap.

        **Never change the portal's base / domicile selector.** Read whatever it
        is already set to and report it as `Shift.meta["base"]`. Picking up a
        trip from the wrong base means being rostered out of a city the user
        does not live in — the single most damaging mistake available here. The
        poller enforces the lock, but an adapter that silently switched bases
        would defeat it by making every shift look correct.
        """

    async def enrich(self, shift: Shift) -> Shift:
        """Fetch detail that needs an extra request, for one shift.

        Called ONLY for shifts that already passed every cheap local check —
        base, schedule fit, premium flag. On FLICA the position grade sits
        behind the flight-number link, so enriching the whole open-time pot
        every poll would multiply request volume against a portal that already
        runs edge bot-protection. Filtering first keeps that cost near zero.

        Detail pages are immutable, so implementations should cache by id.
        Raising is safe: the poller treats a failed enrichment as an unknown
        grade and falls back to alert-only rather than claiming.
        """
        return shift

    @abstractmethod
    async def fetch_my_schedule(self) -> list[Shift]:
        """Shifts already assigned to the user, for conflict and rest checking.

        Return an empty list only if the portal genuinely exposes nothing; a
        wrong empty list disables conflict detection silently.
        """

    @abstractmethod
    async def claim(self, shift_id: str) -> ClaimResult:
        """Attempt to take a shift.

        Losing a race is normal, not exceptional — return `LOST_RACE` rather than
        raising when another user got there first.
        """

    async def close(self) -> None:
        """Release browser/session resources. Override if needed."""
        return None
