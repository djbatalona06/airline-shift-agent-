"""Schedule engine tests.

These run with no portal access and are the acceptance gate for the matcher.
The DST and cross-timezone cases are the ones that would silently claim a wrong
shift if the UTC-normalisation approach in schedule.py were replaced with naive
local-time comparison.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from shift_agent.config import Availability, Rules, Slot
from shift_agent.models import MatchVerdict, Shift
from shift_agent.schedule import ScheduleEngine

NY = ZoneInfo("America/New_York")


def avail(*slots: tuple[str, str, str], tz: str = "America/New_York", excluded=()) -> Availability:
    return Availability(
        timezone=tz,
        slots=tuple(Slot(day=d, start=s, end=e) for d, s, e in slots),
        excluded_dates=excluded,
    )


def ny(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=NY)


def shift(start: datetime, end: datetime, sid: str = "S1") -> Shift:
    return Shift(id=sid, start=start, end=end, title="test")


# --- basic containment -------------------------------------------------------

def test_shift_inside_window_matches():
    eng = ScheduleEngine(avail(("Monday", "08:00", "17:00")))
    res = eng.evaluate(shift(ny(2026, 1, 5, 9), ny(2026, 1, 5, 15)))
    assert res.verdict is MatchVerdict.MATCH


def test_shift_extending_past_window_rejected():
    eng = ScheduleEngine(avail(("Monday", "08:00", "17:00")))
    res = eng.evaluate(shift(ny(2026, 1, 5, 15), ny(2026, 1, 5, 19)))
    assert res.verdict is MatchVerdict.OUTSIDE_AVAILABILITY


def test_shift_on_unavailable_weekday_rejected():
    eng = ScheduleEngine(avail(("Monday", "08:00", "17:00")))
    res = eng.evaluate(shift(ny(2026, 1, 6, 9), ny(2026, 1, 6, 15)))
    assert res.verdict is MatchVerdict.OUTSIDE_AVAILABILITY


def test_adjacent_slots_merge_so_shift_can_span_the_seam():
    eng = ScheduleEngine(avail(("Monday", "08:00", "12:00"), ("Monday", "12:00", "17:00")))
    res = eng.evaluate(shift(ny(2026, 1, 5, 11), ny(2026, 1, 5, 13)))
    assert res.verdict is MatchVerdict.MATCH


def test_gap_between_slots_is_not_bridged():
    eng = ScheduleEngine(avail(("Monday", "08:00", "11:00"), ("Monday", "13:00", "17:00")))
    res = eng.evaluate(shift(ny(2026, 1, 5, 10), ny(2026, 1, 5, 14)))
    assert res.verdict is MatchVerdict.OUTSIDE_AVAILABILITY


# --- windows crossing midnight ----------------------------------------------

def test_night_window_crossing_midnight_accepts_overnight_shift():
    eng = ScheduleEngine(avail(("Friday", "22:00", "06:00")))
    res = eng.evaluate(shift(ny(2026, 1, 9, 23), ny(2026, 1, 10, 2)))
    assert res.verdict is MatchVerdict.MATCH


def test_night_window_does_not_accept_shift_past_its_end():
    eng = ScheduleEngine(avail(("Friday", "22:00", "06:00")))
    res = eng.evaluate(shift(ny(2026, 1, 9, 23), ny(2026, 1, 10, 8)))
    assert res.verdict is MatchVerdict.OUTSIDE_AVAILABILITY


def test_full_day_slots_merge_into_continuous_availability():
    """00:00 -> 00:00 on every day must cover time continuously.

    Load-bearing for the poller test fixtures. Writing 23:59 as the end instead
    leaves a one-minute hole at midnight, and any overnight shift falls through
    it — which made those tests silently depend on the hour they ran at.
    """
    eng = ScheduleEngine(avail(*[(d, "00:00", "00:00") for d in
                                 ("Monday", "Tuesday", "Wednesday", "Thursday",
                                  "Friday", "Saturday", "Sunday")]))
    res = eng.evaluate(shift(ny(2026, 1, 5, 21), ny(2026, 1, 6, 3)))
    assert res.verdict is MatchVerdict.MATCH


def test_all_day_slots_ending_at_2359_leave_a_midnight_gap():
    """The trap this fixture change avoids, pinned so nobody reintroduces it."""
    eng = ScheduleEngine(avail(*[(d, "00:00", "23:59") for d in ("Monday", "Tuesday")]))
    res = eng.evaluate(shift(ny(2026, 1, 5, 21), ny(2026, 1, 6, 3)))
    assert res.verdict is MatchVerdict.OUTSIDE_AVAILABILITY


def test_slot_crossing_midnight_is_detected():
    assert Slot(day="Friday", start="22:00", end="06:00").crosses_midnight
    assert not Slot(day="Friday", start="08:00", end="17:00").crosses_midnight


# --- DST ---------------------------------------------------------------------

def test_spring_forward_window_is_one_real_hour_shorter():
    """2026-03-08 America/New_York: 00:00-12:00 local is 11 actual hours.

    A naive local-time implementation reports 12 and would accept a shift an
    hour longer than the user is actually free.
    """
    eng = ScheduleEngine(avail(("Sunday", "00:00", "12:00")))
    start = datetime(2026, 3, 8, tzinfo=NY).astimezone(UTC)
    windows = eng.windows_covering(start, start + timedelta(hours=12))
    day_window = next(w for w in windows if w.start == start)
    assert day_window.end - day_window.start == timedelta(hours=11)


def test_fall_back_window_is_one_real_hour_longer():
    eng = ScheduleEngine(avail(("Sunday", "00:00", "12:00")))
    start = datetime(2026, 11, 1, tzinfo=NY).astimezone(UTC)
    windows = eng.windows_covering(start, start + timedelta(hours=14))
    day_window = next(w for w in windows if w.start == start)
    assert day_window.end - day_window.start == timedelta(hours=13)


def test_shift_across_spring_forward_still_matches():
    eng = ScheduleEngine(avail(("Sunday", "00:00", "12:00")))
    res = eng.evaluate(shift(ny(2026, 3, 8, 1), ny(2026, 3, 8, 5)))
    assert res.verdict is MatchVerdict.MATCH


# --- cross-timezone ----------------------------------------------------------

def test_portal_reporting_utc_matches_local_window():
    """Portal reports 13:00-17:00Z; user is free Mondays 08:00-17:00 New York."""
    eng = ScheduleEngine(avail(("Monday", "08:00", "17:00")))
    res = eng.evaluate(
        shift(
            datetime(2026, 1, 5, 13, tzinfo=UTC),
            datetime(2026, 1, 5, 17, tzinfo=UTC),
        )
    )
    assert res.verdict is MatchVerdict.MATCH


def test_utc_shift_outside_local_window_rejected():
    eng = ScheduleEngine(avail(("Monday", "08:00", "17:00")))
    res = eng.evaluate(
        shift(
            datetime(2026, 1, 5, 3, tzinfo=UTC),
            datetime(2026, 1, 5, 7, tzinfo=UTC),
        )
    )
    assert res.verdict is MatchVerdict.OUTSIDE_AVAILABILITY


# --- excluded dates ----------------------------------------------------------

def test_excluded_date_blocks_shift():
    eng = ScheduleEngine(
        avail(("Friday", "08:00", "17:00"), excluded=(date(2026, 12, 25),))
    )
    res = eng.evaluate(shift(ny(2026, 12, 25, 9), ny(2026, 12, 25, 15)))
    assert res.verdict is MatchVerdict.EXCLUDED_DATE


def test_overnight_shift_running_into_excluded_date_blocked():
    eng = ScheduleEngine(
        avail(("Thursday", "20:00", "08:00"), excluded=(date(2026, 12, 25),))
    )
    res = eng.evaluate(shift(ny(2026, 12, 24, 22), ny(2026, 12, 25, 6)))
    assert res.verdict is MatchVerdict.EXCLUDED_DATE


# --- conflicts and rest ------------------------------------------------------

def test_overlap_with_assigned_shift_rejected():
    eng = ScheduleEngine(avail(("Monday", "00:00", "23:59")))
    assigned = [shift(ny(2026, 1, 5, 10), ny(2026, 1, 5, 14), "A1")]
    res = eng.evaluate(shift(ny(2026, 1, 5, 12), ny(2026, 1, 5, 16)), assigned=assigned)
    assert res.verdict is MatchVerdict.CONFLICTS_ASSIGNED


def test_insufficient_rest_after_assigned_shift_rejected():
    eng = ScheduleEngine(avail(("Monday", "00:00", "23:59")), Rules(min_rest_hours=10))
    assigned = [shift(ny(2026, 1, 5, 0), ny(2026, 1, 5, 8), "A1")]
    res = eng.evaluate(shift(ny(2026, 1, 5, 14), ny(2026, 1, 5, 18)), assigned=assigned)
    assert res.verdict is MatchVerdict.INSUFFICIENT_REST
    assert "6.0h rest" in res.detail


def test_sufficient_rest_accepted():
    eng = ScheduleEngine(avail(("Monday", "00:00", "23:59")), Rules(min_rest_hours=4))
    assigned = [shift(ny(2026, 1, 5, 0), ny(2026, 1, 5, 8), "A1")]
    res = eng.evaluate(shift(ny(2026, 1, 5, 14), ny(2026, 1, 5, 18)), assigned=assigned)
    assert res.verdict is MatchVerdict.MATCH


def test_rest_measured_before_an_upcoming_assigned_shift_too():
    eng = ScheduleEngine(avail(("Monday", "00:00", "23:59")), Rules(min_rest_hours=10))
    assigned = [shift(ny(2026, 1, 5, 20), ny(2026, 1, 5, 23), "A1")]
    res = eng.evaluate(shift(ny(2026, 1, 5, 8), ny(2026, 1, 5, 14)), assigned=assigned)
    assert res.verdict is MatchVerdict.INSUFFICIENT_REST


# --- weekly caps -------------------------------------------------------------

def test_weekly_hours_cap_enforced():
    eng = ScheduleEngine(
        avail(*[(d, "00:00", "23:59") for d in ("Monday", "Tuesday", "Wednesday")]),
        Rules(min_rest_hours=0, max_weekly_hours=20),
    )
    assigned = [
        shift(ny(2026, 1, 5, 0), ny(2026, 1, 5, 8), "A1"),
        shift(ny(2026, 1, 6, 0), ny(2026, 1, 6, 8), "A2"),
    ]
    res = eng.evaluate(shift(ny(2026, 1, 7, 0), ny(2026, 1, 7, 8)), assigned=assigned)
    assert res.verdict is MatchVerdict.EXCEEDS_WEEKLY_CAP


def test_weekly_shift_count_cap_enforced():
    eng = ScheduleEngine(
        avail(*[(d, "00:00", "23:59") for d in ("Monday", "Tuesday", "Wednesday")]),
        Rules(min_rest_hours=0, max_shifts_per_week=2),
    )
    assigned = [
        shift(ny(2026, 1, 5, 0), ny(2026, 1, 5, 4), "A1"),
        shift(ny(2026, 1, 6, 0), ny(2026, 1, 6, 4), "A2"),
    ]
    res = eng.evaluate(shift(ny(2026, 1, 7, 0), ny(2026, 1, 7, 4)), assigned=assigned)
    assert res.verdict is MatchVerdict.EXCEEDS_WEEKLY_CAP


def test_prior_week_assignments_do_not_count_against_cap():
    eng = ScheduleEngine(
        avail(("Monday", "00:00", "23:59")),
        Rules(min_rest_hours=0, max_shifts_per_week=1),
    )
    assigned = [shift(ny(2025, 12, 29, 0), ny(2025, 12, 29, 4), "A1")]
    res = eng.evaluate(shift(ny(2026, 1, 5, 0), ny(2026, 1, 5, 4)), assigned=assigned)
    assert res.verdict is MatchVerdict.MATCH


# --- misc --------------------------------------------------------------------

def test_already_seen_short_circuits():
    eng = ScheduleEngine(avail(("Monday", "08:00", "17:00")))
    res = eng.evaluate(shift(ny(2026, 1, 5, 9), ny(2026, 1, 5, 15)), seen_ids=["S1"])
    assert res.verdict is MatchVerdict.ALREADY_SEEN


def test_empty_availability_matches_nothing():
    eng = ScheduleEngine(avail())
    res = eng.evaluate(shift(ny(2026, 1, 5, 9), ny(2026, 1, 5, 15)))
    assert res.verdict is MatchVerdict.OUTSIDE_AVAILABILITY


def test_naive_datetime_rejected_at_construction():
    with pytest.raises(ValueError, match="timezone-aware"):
        Shift(id="X", start=datetime(2026, 1, 5, 9), end=datetime(2026, 1, 5, 15))


def test_backwards_shift_rejected():
    with pytest.raises(ValueError, match="after start"):
        Shift(id="X", start=ny(2026, 1, 5, 15), end=ny(2026, 1, 5, 9))
