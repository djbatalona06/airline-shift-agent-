"""Availability matching.

Design note — everything is compared in UTC. Recurring windows are authored in
the user's local timezone ("Mondays 08:00-17:00"), but a local wall-clock
comparison breaks in three ways this project will actually hit: DST transitions
shift real duration by an hour, the portal may report shifts in a different
timezone than the user lives in, and windows that cross midnight span two local
dates. Materialising local windows into absolute UTC instants once, up front,
makes every downstream comparison plain arithmetic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Iterable, NamedTuple

from .config import Availability, Rules
from .models import MatchResult, MatchVerdict, Shift


class Interval(NamedTuple):
    start: datetime
    end: datetime


def _merge(intervals: Iterable[Interval]) -> list[Interval]:
    """Union overlapping or exactly-adjacent intervals.

    Adjacency matters: consecutive config slots (08:00-12:00, 12:00-17:00) must
    become one 08:00-17:00 window, or a shift spanning the seam is wrongly
    rejected.
    """
    ordered = sorted(intervals)
    merged: list[Interval] = []
    for iv in ordered:
        if merged and iv.start <= merged[-1].end:
            if iv.end > merged[-1].end:
                merged[-1] = Interval(merged[-1].start, iv.end)
        else:
            merged.append(iv)
    return merged


class ScheduleEngine:
    def __init__(self, availability: Availability, rules: Rules | None = None) -> None:
        self.availability = availability
        self.rules = rules or Rules()
        self._tz = availability.tz

    def windows_covering(self, start: datetime, end: datetime) -> list[Interval]:
        """Concrete UTC availability windows overlapping [start, end]."""
        local_start = start.astimezone(self._tz)
        local_end = end.astimezone(self._tz)

        # One day of slack each side: a window opening the previous local day can
        # cross midnight into this shift, and one opening on the final day can
        # extend past it.
        day = local_start.date() - timedelta(days=1)
        last = local_end.date() + timedelta(days=1)

        out: list[Interval] = []
        while day <= last:
            for slot in self.availability.slots:
                if slot.day != day.weekday():
                    continue
                w_start = datetime.combine(day, slot.start, tzinfo=self._tz)
                end_day = day + timedelta(days=1) if slot.crosses_midnight else day
                w_end = datetime.combine(end_day, slot.end, tzinfo=self._tz)
                out.append(Interval(w_start.astimezone(UTC), w_end.astimezone(UTC)))
            day += timedelta(days=1)
        return _merge(out)

    def is_within_availability(self, shift: Shift) -> bool:
        s, e = shift.start.astimezone(UTC), shift.end.astimezone(UTC)
        return any(w.start <= s and e <= w.end for w in self.windows_covering(s, e))

    def touches_excluded_date(self, shift: Shift) -> bool:
        """True if any local date the shift touches is excluded.

        A shift running into an excluded date is treated as excluded — "I'm off
        Dec 25" should block something that lands you at work on Dec 25.
        """
        if not self.availability.excluded_dates:
            return False
        excluded = set(self.availability.excluded_dates)
        day = shift.start.astimezone(self._tz).date()
        final = shift.end.astimezone(self._tz).date()
        while day <= final:
            if day in excluded:
                return True
            day += timedelta(days=1)
        return False

    def rest_gap(self, candidate: Shift, other: Shift) -> timedelta | None:
        """Idle time between two non-overlapping shifts; None if they overlap."""
        if candidate.overlaps(other):
            return None
        if candidate.start >= other.end:
            return candidate.start - other.end
        return other.start - candidate.end

    def _week_bounds(self, moment: datetime) -> Interval:
        local = moment.astimezone(self._tz)
        monday = local.date() - timedelta(days=local.weekday())
        start = datetime.combine(monday, datetime.min.time(), tzinfo=self._tz)
        end = start + timedelta(days=7)
        return Interval(start.astimezone(UTC), end.astimezone(UTC))

    def evaluate(
        self,
        shift: Shift,
        assigned: Iterable[Shift] = (),
        seen_ids: Iterable[str] = (),
    ) -> MatchResult:
        """Decide whether `shift` is claimable. Order is cheapest-check-first."""
        if shift.id in set(seen_ids):
            return MatchResult(shift, MatchVerdict.ALREADY_SEEN)

        if self.touches_excluded_date(shift):
            return MatchResult(shift, MatchVerdict.EXCLUDED_DATE)

        if not self.is_within_availability(shift):
            local = shift.start.astimezone(self._tz)
            return MatchResult(
                shift,
                MatchVerdict.OUTSIDE_AVAILABILITY,
                f"starts {local:%a %Y-%m-%d %H:%M %Z}, outside configured windows",
            )

        assigned = list(assigned)
        for other in assigned:
            if shift.overlaps(other):
                return MatchResult(
                    shift,
                    MatchVerdict.CONFLICTS_ASSIGNED,
                    f"overlaps assigned shift {other.id}",
                )

        min_rest = timedelta(hours=self.rules.min_rest_hours)
        if min_rest:
            for other in assigned:
                gap = self.rest_gap(shift, other)
                if gap is not None and gap < min_rest:
                    hours = gap.total_seconds() / 3600
                    return MatchResult(
                        shift,
                        MatchVerdict.INSUFFICIENT_REST,
                        f"{hours:.1f}h rest vs {self.rules.min_rest_hours}h required "
                        f"(against shift {other.id})",
                    )

        capped = self._check_weekly_caps(shift, assigned)
        if capped is not None:
            return capped

        return MatchResult(shift, MatchVerdict.MATCH)

    def _check_weekly_caps(self, shift: Shift, assigned: list[Shift]) -> MatchResult | None:
        rules = self.rules
        if rules.max_weekly_hours is None and rules.max_shifts_per_week is None:
            return None

        week = self._week_bounds(shift.start)
        in_week = [s for s in assigned if week.start <= s.start < week.end]

        if rules.max_shifts_per_week is not None:
            if len(in_week) + 1 > rules.max_shifts_per_week:
                return MatchResult(
                    shift,
                    MatchVerdict.EXCEEDS_WEEKLY_CAP,
                    f"would be shift {len(in_week) + 1} of max {rules.max_shifts_per_week}",
                )

        if rules.max_weekly_hours is not None:
            total = sum(s.duration.total_seconds() for s in in_week)
            total += shift.duration.total_seconds()
            hours = total / 3600
            if hours > rules.max_weekly_hours:
                return MatchResult(
                    shift,
                    MatchVerdict.EXCEEDS_WEEKLY_CAP,
                    f"would total {hours:.1f}h vs {rules.max_weekly_hours}h cap",
                )
        return None
