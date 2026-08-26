"""The real FlicaAdapter, a real Chromium, and a fake FLICA on loopback.

This is as close to a live portal as can exist without a FLICA account, and it
covers the paths docs/VERIFICATION.md lists as unproven: frames, the persistent
profile, challenge detection and recovery, and whether a poll cycle actually
sees new data.

Everything is loopback. No test here may reach the internet.
"""

from __future__ import annotations

import pytest

from shift_agent.adapters.flica import FlicaAdapter
from shift_agent.config import UserConfig
from shift_agent.models import AuthState
from shift_agent.notify.console import ConsoleNotifier
from shift_agent.poller import CHALLENGE_PROBE_START, Poller
from shift_agent.store import CHALLENGE_PAUSE_KEY, PAUSED_KEY, Store

from .conftest import launch_kwargs

pytestmark = pytest.mark.e2e

ALL_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def make_config(url: str, profile, **over) -> UserConfig:
    raw = {
        "name": "e2e",
        "portal": {
            "adapter": "flica",
            "base_url": url,
            "options": {
                "browser_profile": str(profile),
                "headless": True,
                "reload_settle_ms": 300,
            },
        },
        "availability": {
            "timezone": "America/New_York",
            "slots": [{"day": d, "start": "00:00", "end": "23:59"} for d in ALL_DAYS],
        },
        "rules": {"min_rest_hours": 0, "min_lead_minutes": 0},
        "home_base": {"code": "MCO"},
        "grades": {"pursue": [], "notify_only": []},
        "dry_run": True,
    }
    raw.update(over)
    return UserConfig.model_validate(raw)


class _HeadlessAdapter(FlicaAdapter):
    """Headless, and pointed at the sandbox's Chromium.

    The shipped adapter is headed on purpose — a human has to be able to click a
    challenge. That is exactly what a CI box cannot provide, so the test
    overrides the launch and leaves every other code path untouched.
    """

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        options = self.config.portal.options or {}
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(options["browser_profile"]),
            **launch_kwargs(headless=True),
        )
        self._page = (
            self._context.pages[0] if self._context.pages else await self._context.new_page()
        )
        await self._page.goto(self.config.portal.base_url, wait_until="domcontentloaded")


async def open_adapter(url, profile, **over):
    config = make_config(url, profile, **over)
    adapter = _HeadlessAdapter(config, None, {})
    await adapter.start()
    return adapter, config


# --------------------------------------------------------------------------
# frames and parsing, through a real browser
# --------------------------------------------------------------------------


async def test_reads_open_time_out_of_a_nested_frame(portal, browser_profile):
    server, url = portal
    adapter, _ = await open_adapter(url, browser_profile)
    try:
        shifts = await adapter.fetch_open_shifts()
        assert {s.id for s in shifts} == {"M4A76", "M7C21", "M2B08", "M9E44"}
        assert all(s.meta["base"] == "MCO" for s in shifts)
    finally:
        await adapter.close()


async def test_reads_the_assigned_schedule(portal, browser_profile):
    server, url = portal
    adapter, _ = await open_adapter(url, browser_profile)
    try:
        assigned = await adapter.fetch_my_schedule()
        assert assigned, "expected the schedule frame to parse"
        assert all(s.meta.get("assigned") for s in assigned)
    finally:
        await adapter.close()


async def test_enrich_opens_the_detail_page_and_reads_the_grade(portal, browser_profile):
    server, url = portal
    adapter, _ = await open_adapter(url, browser_profile)
    try:
        shifts = await adapter.fetch_open_shifts()
        target = next(s for s in shifts if s.id == "M4A76")
        enriched = await adapter.enrich(target)
        assert enriched.meta.get("grade") == "E"
    finally:
        await adapter.close()


# --------------------------------------------------------------------------
# defect 1: the frame must actually be re-read
# --------------------------------------------------------------------------


async def test_a_second_fetch_sees_changed_data(portal, browser_profile):
    """The adapter used to read whatever DOM was present at start-up, forever.
    A shift posted after launch would never be seen, and the agent would look
    perfectly healthy while doing nothing."""
    server, url = portal
    adapter, _ = await open_adapter(url, browser_profile)
    try:
        first = {s.id for s in await adapter.fetch_open_shifts()}
        assert "M7C21" in first

        server.drop_a_pairing()

        second = {s.id for s in await adapter.fetch_open_shifts()}
        assert "M7C21" not in second
        assert second < first
    finally:
        await adapter.close()


# --------------------------------------------------------------------------
# challenge detection and recovery
# --------------------------------------------------------------------------


async def test_challenge_is_detected_and_handed_over(portal, browser_profile):
    server, url = portal
    adapter, _ = await open_adapter(url, browser_profile)
    try:
        server.state = "challenge"
        await adapter._page.reload(wait_until="domcontentloaded")

        assert await adapter.is_authenticated() is False
        result = await adapter.login()
        assert result.state is AuthState.NEEDS_HUMAN
        assert result.challenge_url
        assert "challenge" in result.detail.lower() or "verification" in result.detail.lower()
    finally:
        await adapter.close()


async def test_session_expiry_is_not_reported_as_authenticated(portal, browser_profile):
    server, url = portal
    adapter, _ = await open_adapter(url, browser_profile)
    try:
        server.state = "expired"
        await adapter._page.reload(wait_until="domcontentloaded")
        assert await adapter.is_authenticated() is False
    finally:
        await adapter.close()


async def test_full_challenge_cycle_pauses_then_self_resumes(portal, browser_profile, tmp_path):
    """The loop docs/VERIFICATION.md says has never been run end to end:
    challenge appears, agent pauses and alerts, a human clears it, agent
    carries on. Here the agent notices by itself."""
    server, url = portal
    adapter, config = await open_adapter(url, browser_profile)
    store = Store(tmp_path / "state.db")
    notifier = ConsoleNotifier()

    async def no_sleep(_):
        return None

    clock = {"now": 0.0}
    poller = Poller(config, adapter, notifier, store, sleep=no_sleep)
    poller._monotonic = lambda: clock["now"]

    try:
        server.state = "challenge"
        await adapter._page.reload(wait_until="domcontentloaded")

        report = await poller.run_once()
        assert report.skipped == "needs_human"
        assert store.get(PAUSED_KEY) is True
        assert store.get(CHALLENGE_PAUSE_KEY) is True
        assert any(kind == "needs-human" for kind, _ in notifier.sent)

        # Still challenged: stays paused, and does not hammer the portal.
        clock["now"] += CHALLENGE_PROBE_START + 1
        assert (await poller.run_once()).skipped == "paused"

        # Someone clears it in the browser. No /resume is ever sent.
        server.state = "ok"
        await adapter._page.goto(url, wait_until="domcontentloaded")
        clock["now"] += CHALLENGE_PROBE_START * 4

        report = await poller.run_once()
        assert store.get(PAUSED_KEY) is False
        assert report.skipped is None
        assert report.evaluated > 0
    finally:
        await adapter.close()
        store.close()


# --------------------------------------------------------------------------
# the persistent profile
# --------------------------------------------------------------------------


async def test_session_survives_a_browser_restart(portal, browser_profile):
    """The persistent profile is what stops the agent asking for a sign-in
    every time it restarts, which on a VPS means a VNC session every time."""
    server, url = portal
    adapter, _ = await open_adapter(url, browser_profile)
    try:
        await adapter._page.evaluate("() => localStorage.setItem('flica-session', 'live')")
    finally:
        await adapter.close()

    adapter, _ = await open_adapter(url, browser_profile)
    try:
        value = await adapter._page.evaluate("() => localStorage.getItem('flica-session')")
        assert value == "live"
        assert await adapter.is_authenticated() is True
    finally:
        await adapter.close()


# --------------------------------------------------------------------------
# defect 2: outcomes must be reconciled
# --------------------------------------------------------------------------


async def test_pending_request_reads_as_undecided(portal, browser_profile):
    server, url = portal
    adapter, _ = await open_adapter(url, browser_profile)
    try:
        # The fixture's first row is Pending.
        assert await adapter.check_outcome("M4A76") is None
    finally:
        await adapter.close()


async def test_rejected_request_is_read_back_from_the_portal(portal, browser_profile):
    server, url = portal
    adapter, _ = await open_adapter(url, browser_profile)
    try:
        server.reject_request()
        outcome = await adapter.check_outcome("M4A76")
        assert outcome is not None
        assert outcome.outcome.value == "rejected"
    finally:
        await adapter.close()


# --------------------------------------------------------------------------
# containment
# --------------------------------------------------------------------------


async def test_nothing_leaves_loopback(portal, browser_profile):
    """If this ever fails, the sandbox has stopped being a sandbox."""
    server, url = portal
    adapter, _ = await open_adapter(url, browser_profile)
    external: list[str] = []
    adapter._page.on(
        "request",
        lambda request: (
            external.append(request.url)
            if not request.url.startswith(("http://127.0.0.1", "about:", "data:", "chrome"))
            else None
        ),
    )
    try:
        await adapter.fetch_open_shifts()
        shifts = await adapter.fetch_open_shifts()
        await adapter.enrich(shifts[0])
        assert external == []
    finally:
        await adapter.close()
