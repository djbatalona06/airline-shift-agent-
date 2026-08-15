"""End-to-end pipeline tests: poll -> evaluate -> offer -> claim -> persist.

Runs with no portal, no network, no credentials. Several of these are
regression tests for bugs found while wiring the poller — they are marked as
such, because each would have failed silently in production rather than loudly.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from shift_agent.adapters.mock import MockAdapter
from shift_agent.config import ClaimMode, UserConfig
from shift_agent.models import AuthState, ClaimOutcome, MatchVerdict, Shift
from shift_agent.notify.console import ConsoleNotifier
from shift_agent.poller import Poller
from shift_agent.store import Store

ALL_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def make_config(**over) -> UserConfig:
    base = {
        "name": "tester",
        "portal": {"adapter": "mock"},
        # 00:00 -> 00:00 crosses midnight, so each day covers a full 24h and
        # consecutive days merge into continuous availability. Ending at 23:59
        # instead leaves a one-minute hole at midnight, which made these tests
        # silently time-of-day dependent: a shift generated late in the evening
        # crosses midnight and falls through the gap.
        "availability": {
            "timezone": "America/New_York",
            "slots": [{"day": d, "start": "00:00", "end": "00:00"} for d in ALL_DAYS],
        },
        "rules": {"min_rest_hours": 0},
        "claim_mode": "confirm",
        "dry_run": False,
    }
    base.update(over)
    return UserConfig.model_validate(base)


def future_shift(days: int = 2, hours: float = 6, sid: str = "S1") -> Shift:
    start = datetime.now(UTC) + timedelta(days=days)
    return Shift(id=sid, start=start, end=start + timedelta(hours=hours), title="Trip")


def build(tmp_path, config=None, *, confirm=True, shifts=None, **adapter_kw):
    config = config or make_config()
    adapter = MockAdapter(config, open_shifts=shifts if shifts is not None else [future_shift()], **adapter_kw)
    notifier = ConsoleNotifier(auto_confirm=confirm)
    store = Store(tmp_path / "state.db")
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    poller = Poller(config, adapter, notifier, store, sleep=fake_sleep)
    poller.slept = slept
    return poller, adapter, notifier, store


# --- happy path --------------------------------------------------------------

async def test_matching_shift_is_offered_and_claimed(tmp_path):
    poller, adapter, notifier, store = build(tmp_path)
    report = await poller.run_once()

    assert report.matched == 1
    assert report.offered == 1
    assert report.claimed == 1
    assert adapter.claim_calls == ["S1"]
    assert store.already_claimed("tester", "S1")


async def test_declined_offer_is_not_claimed(tmp_path):
    poller, adapter, _, store = build(tmp_path, confirm=False)
    report = await poller.run_once()

    assert report.offered == 1
    assert report.claimed == 0
    assert adapter.claim_calls == []
    assert not store.already_claimed("tester", "S1")


async def test_notify_only_never_claims(tmp_path):
    config = make_config(claim_mode=ClaimMode.NOTIFY_ONLY)
    poller, adapter, notifier, _ = build(tmp_path, config, confirm=True)
    report = await poller.run_once()

    assert report.matched == 1
    assert report.claimed == 0
    assert adapter.claim_calls == []
    assert any(kind == "info" for kind, _ in notifier.sent)


async def test_shift_outside_availability_not_offered(tmp_path):
    config = make_config(
        availability={
            "timezone": "America/New_York",
            "slots": [{"day": "Monday", "start": "08:00", "end": "09:00"}],
        }
    )
    poller, adapter, _, _ = build(tmp_path, config)
    report = await poller.run_once()

    assert report.matched == 0
    assert report.offered == 0
    assert adapter.claim_calls == []


# --- regression: dry-run must not poison the real claim ----------------------

async def test_dry_run_does_not_call_portal(tmp_path):
    config = make_config(dry_run=True)
    poller, adapter, _, store = build(tmp_path, config)
    report = await poller.run_once()

    assert report.claimed == 1
    assert adapter.claim_calls == []           # nothing sent to the portal
    assert not store.already_claimed("tester", "S1")  # dry runs are not real claims


async def test_real_claim_still_possible_after_dry_run_of_same_shift(tmp_path):
    """Regression: the unique index once blocked this, silently.

    A week-long dry run would have poisoned every shift it evaluated, so the
    first real run after go-live could never claim any of them.
    """
    shift = future_shift()
    store = Store(tmp_path / "state.db")

    dry_cfg = make_config(dry_run=True)
    dry = Poller(dry_cfg, MockAdapter(dry_cfg, open_shifts=[shift]),
                 ConsoleNotifier(auto_confirm=True), store)
    await dry.run_once()

    live_cfg = make_config(dry_run=False)
    live_adapter = MockAdapter(live_cfg, open_shifts=[shift])
    live = Poller(live_cfg, live_adapter, ConsoleNotifier(auto_confirm=True), store)
    store.db.execute("UPDATE seen_shifts SET offered_at = NULL")  # simulate re-offer window

    report = await live.run_once()
    assert report.claimed == 1
    assert live_adapter.claim_calls == ["S1"]
    assert store.already_claimed("tester", "S1")


# --- regression: re-evaluate every cycle, dedupe only notification -----------

async def test_shift_reevaluated_after_availability_widened(tmp_path):
    """Regression: gating evaluation on 'already seen' froze the first verdict.

    A shift rejected under a narrow window stayed rejected forever, even after
    the user edited her schedule to include it.
    """
    shift = future_shift()
    store = Store(tmp_path / "state.db")

    narrow = make_config(
        availability={
            "timezone": "America/New_York",
            "slots": [{"day": "Monday", "start": "08:00", "end": "08:30"}],
        }
    )
    first = Poller(narrow, MockAdapter(narrow, open_shifts=[shift]),
                   ConsoleNotifier(auto_confirm=True), store)
    assert (await first.run_once()).matched == 0

    wide_adapter = MockAdapter(make_config(), open_shifts=[shift])
    second = Poller(make_config(), wide_adapter, ConsoleNotifier(auto_confirm=True), store)
    report = await second.run_once()

    assert report.matched == 1
    assert wide_adapter.claim_calls == ["S1"]


async def test_shift_not_reoffered_after_being_declined(tmp_path):
    shift = future_shift()
    poller, adapter, _, store = build(tmp_path, confirm=False, shifts=[shift])

    first = await poller.run_once()
    second = await poller.run_once()

    assert first.offered == 1
    assert second.offered == 0
    assert second.matched == 1  # still matches; just not re-asked


# --- regression: stale listings ---------------------------------------------

async def test_shift_already_started_is_rejected(tmp_path):
    start = datetime.now(UTC) - timedelta(hours=1)
    stale = Shift(id="OLD", start=start, end=start + timedelta(hours=4))
    poller, adapter, _, _ = build(tmp_path, shifts=[stale])

    report = await poller.run_once()
    assert report.verdicts.get(MatchVerdict.TOO_SOON.value) == 1
    assert report.offered == 0
    assert adapter.claim_calls == []


async def test_min_lead_minutes_enforced(tmp_path):
    config = make_config(rules={"min_rest_hours": 0, "min_lead_minutes": 240})
    start = datetime.now(UTC) + timedelta(hours=1)
    soon = Shift(id="SOON", start=start, end=start + timedelta(hours=4))
    poller, adapter, _, _ = build(tmp_path, config, shifts=[soon])

    report = await poller.run_once()
    assert report.verdicts.get(MatchVerdict.TOO_SOON.value) == 1
    assert adapter.claim_calls == []


# --- gating ------------------------------------------------------------------

async def test_paused_agent_does_nothing(tmp_path):
    poller, adapter, _, _ = build(tmp_path)
    poller.pause("testing")
    report = await poller.run_once()

    assert report.skipped == "paused"
    assert adapter.claim_calls == []


async def test_resume_clears_pause(tmp_path):
    poller, _, _, _ = build(tmp_path)
    poller.pause("testing")
    poller.resume()
    assert not poller.paused
    assert (await poller.run_once()).skipped is None


async def test_quiet_hours_skip_cycle(tmp_path):
    now_local = datetime.now(UTC).astimezone(make_config().availability.tz)
    config = make_config(
        poll={
            "interval_seconds": 45,
            "quiet_hours": {
                "start": (now_local - timedelta(hours=1)).time().replace(microsecond=0),
                "end": (now_local + timedelta(hours=1)).time().replace(microsecond=0),
            },
        }
    )
    poller, adapter, _, _ = build(tmp_path, config)
    report = await poller.run_once()

    assert report.skipped == "quiet_hours"
    assert adapter.claim_calls == []


def test_quiet_hours_window_crossing_midnight(tmp_path):
    config = make_config(
        poll={"interval_seconds": 45,
              "quiet_hours": {"start": "22:00", "end": "07:00"}}
    )
    poller, _, _, _ = build(tmp_path, config)
    tz = config.availability.tz

    at = lambda h: datetime(2026, 1, 5, h, tzinfo=tz).astimezone(UTC)
    assert poller.in_quiet_hours(at(23))
    assert poller.in_quiet_hours(at(3))
    assert not poller.in_quiet_hours(at(12))


# --- auth handoff ------------------------------------------------------------

async def test_captcha_pauses_and_notifies_instead_of_solving(tmp_path):
    poller, adapter, notifier, _ = build(tmp_path, auth_state=AuthState.NEEDS_HUMAN)
    report = await poller.run_once()

    assert report.skipped == "needs_human"
    assert poller.paused
    assert any(kind == "needs-human" for kind, _ in notifier.sent)
    assert adapter.claim_calls == []


async def test_failed_login_raises_for_backoff(tmp_path):
    poller, _, _, _ = build(tmp_path, auth_state=AuthState.FAILED)
    with pytest.raises(RuntimeError, match="login failed"):
        await poller.run_once()


# --- claim outcomes ----------------------------------------------------------

async def test_lost_race_is_recorded_not_raised(tmp_path):
    poller, adapter, notifier, store = build(
        tmp_path, claim_outcome=ClaimOutcome.LOST_RACE
    )
    report = await poller.run_once()

    assert report.claimed == 0
    assert adapter.claim_calls == ["S1"]
    assert not store.already_claimed("tester", "S1")


async def test_only_one_claim_per_cycle(tmp_path):
    shifts = [future_shift(days=2, sid="A"), future_shift(days=4, sid="B")]
    poller, adapter, _, _ = build(tmp_path, shifts=shifts)
    report = await poller.run_once()

    assert report.claimed == 1
    assert len(adapter.claim_calls) == 1


async def test_double_claim_blocked_by_store(tmp_path):
    shift = future_shift()
    poller, adapter, _, store = build(tmp_path, shifts=[shift])
    await poller.run_once()

    store.db.execute("UPDATE seen_shifts SET offered_at = NULL")
    report = await poller.run_once()

    assert report.claimed == 0
    assert len(adapter.claim_calls) == 1  # not re-attempted


# --- backoff -----------------------------------------------------------------

def test_backoff_grows_then_caps(tmp_path):
    poller, _, _, _ = build(tmp_path)
    poller.consecutive_failures = 1
    first = poller.next_delay()
    poller.consecutive_failures = 3
    third = poller.next_delay()
    poller.consecutive_failures = 50

    assert third > first
    assert poller.next_delay() <= 900.0


def test_jittered_delay_stays_in_band(tmp_path):
    poller, _, _, _ = build(tmp_path)
    for _ in range(50):
        d = poller.next_delay()
        assert 30.0 <= d <= 60.0


async def test_alert_after_three_consecutive_failures(tmp_path):
    """Generic cycle failures drive backoff and an alert.

    Uses a fetch fault rather than a login one: repeated login failures are
    intercepted earlier by the account-lockout circuit breaker, which is
    covered in test_login_security.py.
    """
    poller, adapter, notifier, _ = build(tmp_path)

    async def boom():
        raise RuntimeError("portal exploded")

    adapter.fetch_open_shifts = boom

    await poller.run_forever(max_cycles=3)

    # "system", not "alert": this is the agent's own health, and Telegram marks
    # the two differently so a stalled poller is not mistaken for a shift alert.
    assert any(kind == "system" for kind, _ in notifier.sent)
    assert poller.consecutive_failures == 3
    # Backoff was applied between cycles rather than the flat poll interval.
    assert poller.slept == [90.0, 180.0]


async def test_successful_cycle_resets_backoff(tmp_path):
    poller, _, _, _ = build(tmp_path)
    poller.consecutive_failures = 4
    await poller.run_once()
    assert poller.consecutive_failures == 0
