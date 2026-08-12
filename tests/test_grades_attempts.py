"""Grade routing and the claim-attempt cap.

The fail-closed tests matter most here. If FLICA changes its markup and the
grade stops parsing, the agent must alert rather than claim — an A or C slipping
through as auto-claimed puts her in a seat she did not ask for.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shift_agent.adapters.mock import MockAdapter
from shift_agent.config import Grades, UserConfig
from shift_agent.models import ClaimOutcome, ClaimResult, MatchVerdict, Shift
from shift_agent.notify.console import ConsoleNotifier
from shift_agent.poller import Poller
from shift_agent.store import Store

ALL_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def make_config(**over) -> UserConfig:
    base = {
        "name": "tester",
        "portal": {"adapter": "mock"},
        # 00:00 -> 00:00 crosses midnight, so each day covers a full 24h and
        # consecutive days merge into continuous availability. Using 23:59 as
        # the end instead leaves a one-minute hole at midnight that any
        # overnight shift falls straight through.
        "availability": {
            "timezone": "America/New_York",
            "slots": [{"day": d, "start": "00:00", "end": "00:00"} for d in ALL_DAYS],
        },
        "rules": {"min_rest_hours": 0},
        "grades": {"pursue": ["E", "D", "B"], "notify_only": ["A", "C"]},
        "home_base": {"code": "MCO"},
        "claim_mode": "confirm",
        "dry_run": False,
    }
    base.update(over)
    return UserConfig.model_validate(base)


def graded(grade, sid="S1", days=2, base="MCO") -> Shift:
    start = datetime.now(UTC) + timedelta(days=days)
    meta = {} if grade is None else {"grade": grade}
    if base is not None:
        meta["base"] = base
    return Shift(id=sid, start=start, end=start + timedelta(hours=6), title="Premium", meta=meta)


def build(tmp_path, shifts, config=None, *, confirm=True, store=None, **kw):
    config = config or make_config()
    adapter = MockAdapter(config, open_shifts=shifts, **kw)
    store = store or Store(tmp_path / "state.db")

    async def no_sleep(_):
        return None

    poller = Poller(config, adapter, ConsoleNotifier(auto_confirm=confirm), store, sleep=no_sleep)
    return poller, adapter, store


# --- grade routing -----------------------------------------------------------

@pytest.mark.parametrize("grade", ["E", "D", "B"])
async def test_pursued_grades_are_claimed(tmp_path, grade):
    poller, adapter, _ = build(tmp_path, [graded(grade)])
    report = await poller.run_once()
    assert report.claimed == 1
    assert adapter.claim_calls == ["S1"]


@pytest.mark.parametrize("grade", ["A", "C"])
async def test_notify_only_grades_are_never_claimed(tmp_path, grade):
    poller, adapter, _ = build(tmp_path, [graded(grade)])
    report = await poller.run_once()
    assert report.claimed == 0
    assert report.alerted == 1
    assert adapter.claim_calls == []
    assert report.verdicts.get(MatchVerdict.GRADE_NOTIFY_ONLY.value) == 1


async def test_lowercase_grade_still_routes_correctly(tmp_path):
    poller, adapter, _ = build(tmp_path, [graded("e")])
    assert (await poller.run_once()).claimed == 1
    assert adapter.claim_calls == ["S1"]


# --- fail-closed -------------------------------------------------------------

async def test_missing_grade_alerts_rather_than_claims(tmp_path):
    """Parsing broke, or the portal stopped showing a grade. Never claim."""
    poller, adapter, _ = build(tmp_path, [graded(None)])
    report = await poller.run_once()
    assert adapter.claim_calls == []
    assert report.verdicts.get(MatchVerdict.GRADE_NOTIFY_ONLY.value) == 1


async def test_unknown_grade_alerts_rather_than_claims(tmp_path):
    """A grade nobody configured (say FLICA adds 'F') must not be pursued."""
    poller, adapter, _ = build(tmp_path, [graded("F")])
    report = await poller.run_once()
    assert adapter.claim_calls == []
    assert report.verdicts.get(MatchVerdict.GRADE_NOTIFY_ONLY.value) == 1


async def test_empty_grade_string_alerts(tmp_path):
    poller, adapter, _ = build(tmp_path, [graded("   ")])
    await poller.run_once()
    assert adapter.claim_calls == []


# --- feature is opt-in -------------------------------------------------------

async def test_grades_disabled_by_default_so_other_jobs_still_work(tmp_path):
    """A CNA adapter populates no grade; every shift must still be pursued."""
    config = make_config(grades={})
    assert not config.grades.enabled
    poller, adapter, _ = build(tmp_path, [graded(None)], config)
    assert (await poller.run_once()).claimed == 1
    assert adapter.claim_calls == ["S1"]


def test_grade_cannot_be_both_pursue_and_notify():
    with pytest.raises(ValueError, match="both pursue and notify_only"):
        Grades(pursue=["A", "B"], notify_only=["B"])


def test_grades_are_normalised_to_uppercase():
    g = Grades(pursue=["e", " d "], notify_only=["a"])
    assert g.pursue == ("E", "D") and g.notify_only == ("A",)


# --- alerts are not repeated -------------------------------------------------

async def test_notify_only_shift_alerts_once_not_every_cycle(tmp_path):
    poller, _, _ = build(tmp_path, [graded("A")])
    first = await poller.run_once()
    second = await poller.run_once()
    assert first.alerted == 1
    assert second.alerted == 0


# --- attempt cap -------------------------------------------------------------

async def test_gives_up_after_three_failed_attempts(tmp_path):
    store = Store(tmp_path / "state.db")
    shift = graded("E")
    poller, adapter, _ = build(
        tmp_path, [shift], store=store, claim_outcome=ClaimOutcome.REJECTED
    )

    for _ in range(3):
        store.db.execute("UPDATE seen_shifts SET offered_at = NULL")
        await poller.run_once()
    assert len(adapter.claim_calls) == 3

    store.db.execute("UPDATE seen_shifts SET offered_at = NULL")
    report = await poller.run_once()

    assert len(adapter.claim_calls) == 3          # no fourth attempt
    assert report.verdicts.get(MatchVerdict.MAX_ATTEMPTS_REACHED.value) == 1


async def test_attempt_limit_is_configurable(tmp_path):
    store = Store(tmp_path / "state.db")
    config = make_config(rules={"min_rest_hours": 0, "max_claim_attempts": 1})
    poller, adapter, _ = build(
        tmp_path, [graded("E")], config, store=store, claim_outcome=ClaimOutcome.LOST_RACE
    )
    await poller.run_once()
    store.db.execute("UPDATE seen_shifts SET offered_at = NULL")
    await poller.run_once()
    assert len(adapter.claim_calls) == 1


async def test_dry_run_failures_do_not_burn_the_attempt_budget(tmp_path):
    """The planned dry-run week must not exhaust retries before go-live."""
    store = Store(tmp_path / "state.db")
    store.record_claim("tester", "S1", ClaimResult(ClaimOutcome.REJECTED, "sim"), dry_run=True)
    store.record_claim("tester", "S1", ClaimResult(ClaimOutcome.REJECTED, "sim"), dry_run=True)
    store.record_claim("tester", "S1", ClaimResult(ClaimOutcome.REJECTED, "sim"), dry_run=True)

    assert store.failed_attempts("tester", "S1") == 0

    poller, adapter, _ = build(tmp_path, [graded("E")], store=store)
    assert (await poller.run_once()).claimed == 1
    assert adapter.claim_calls == ["S1"]


async def test_successful_claim_does_not_count_toward_the_cap(tmp_path):
    store = Store(tmp_path / "state.db")
    store.record_claim("tester", "S1", ClaimResult(ClaimOutcome.CLAIMED, "ok"), dry_run=False)
    assert store.failed_attempts("tester", "S1") == 0


async def test_capped_shift_alerts_once(tmp_path):
    store = Store(tmp_path / "state.db")
    for _ in range(3):
        store.record_claim("tester", "S1", ClaimResult(ClaimOutcome.REJECTED, "unable"), dry_run=False)

    poller, adapter, _ = build(tmp_path, [graded("E")], store=store)
    report = await poller.run_once()

    assert adapter.claim_calls == []
    assert report.alerted == 1
    assert report.verdicts.get(MatchVerdict.MAX_ATTEMPTS_REACHED.value) == 1


# --- lazy enrichment ---------------------------------------------------------

async def test_enrich_not_called_for_wrong_base(tmp_path):
    """The whole point of the two-stage classify: cheap checks cost no requests."""
    poller, adapter, _ = build(tmp_path, [graded("E", base="DEN")])
    calls = []

    async def spy(shift):
        calls.append(shift.id)
        return shift

    adapter.enrich = spy
    await poller.run_once()
    assert calls == []


async def test_enrich_not_called_for_shift_outside_availability(tmp_path):
    config = make_config(
        availability={
            "timezone": "America/New_York",
            "slots": [{"day": "Monday", "start": "08:00", "end": "08:30"}],
        }
    )
    poller, adapter, _ = build(tmp_path, [graded("E")], config)
    calls = []

    async def spy(shift):
        calls.append(shift.id)
        return shift

    adapter.enrich = spy
    await poller.run_once()
    assert calls == []


async def test_enrich_called_for_surviving_shift(tmp_path):
    poller, adapter, _ = build(tmp_path, [graded(None)])
    calls = []

    async def spy(shift):
        calls.append(shift.id)
        return Shift(id=shift.id, start=shift.start, end=shift.end,
                     title=shift.title, meta={**shift.meta, "grade": "E"})

    adapter.enrich = spy
    report = await poller.run_once()

    assert calls == ["S1"]
    assert report.claimed == 1           # grade supplied by enrichment


async def test_enrichment_failure_falls_closed_to_alert(tmp_path):
    poller, adapter, _ = build(tmp_path, [graded("E")])

    async def boom(shift):
        raise RuntimeError("detail page unreachable")

    adapter.enrich = boom
    report = await poller.run_once()

    assert adapter.claim_calls == []
    assert report.verdicts.get(MatchVerdict.GRADE_NOTIFY_ONLY.value) == 1


# --- premium filter ----------------------------------------------------------

async def test_non_premium_shift_skipped_when_premium_only(tmp_path):
    config = make_config(rules={"min_rest_hours": 0, "premium_only": True})
    poller, adapter, _ = build(tmp_path, [graded("E")], config)   # no premium flag
    report = await poller.run_once()

    assert adapter.claim_calls == []
    assert report.verdicts.get(MatchVerdict.NOT_PREMIUM.value) == 1


async def test_premium_shift_claimed_when_premium_only(tmp_path):
    config = make_config(rules={"min_rest_hours": 0, "premium_only": True})
    shift = graded("E")
    premium = Shift(id=shift.id, start=shift.start, end=shift.end,
                    title=shift.title, meta={**shift.meta, "premium": True})
    poller, adapter, _ = build(tmp_path, [premium], config)
    assert (await poller.run_once()).claimed == 1


async def test_premium_filter_off_by_default(tmp_path):
    poller, adapter, _ = build(tmp_path, [graded("E")])
    assert (await poller.run_once()).claimed == 1


# --- home base lock ----------------------------------------------------------

@pytest.mark.parametrize("base", ["DEN", "ATL", "MIA", "TTN"])
async def test_shift_from_another_base_is_refused(tmp_path, base):
    """Wrong base means being rostered out of a city she does not live in."""
    poller, adapter, _ = build(tmp_path, [graded("E", base=base)])
    report = await poller.run_once()

    assert adapter.claim_calls == []
    assert report.claimed == 0
    assert report.verdicts.get(MatchVerdict.WRONG_BASE.value) == 1


async def test_missing_base_is_refused_not_assumed(tmp_path):
    poller, adapter, _ = build(tmp_path, [graded("E", base=None)])
    report = await poller.run_once()
    assert adapter.claim_calls == []
    assert report.verdicts.get(MatchVerdict.WRONG_BASE.value) == 1


async def test_home_base_is_accepted_case_insensitively(tmp_path):
    poller, adapter, _ = build(tmp_path, [graded("E", base="mco")])
    assert (await poller.run_once()).claimed == 1
    assert adapter.claim_calls == ["S1"]


async def test_wrong_base_does_not_generate_an_alert(tmp_path):
    """Out-of-base shifts fill the pot; alerting on each would be pure noise."""
    poller, _, _ = build(tmp_path, [graded("E", base="DEN")])
    assert (await poller.run_once()).alerted == 0


async def test_base_checked_before_grade(tmp_path):
    """A wrong-base shift reports the base problem, not the grade one."""
    poller, _, _ = build(tmp_path, [graded("A", base="DEN")])
    report = await poller.run_once()
    assert report.verdicts.get(MatchVerdict.WRONG_BASE.value) == 1
    assert MatchVerdict.GRADE_NOTIFY_ONLY.value not in report.verdicts


async def test_base_lock_disabled_when_unset(tmp_path):
    """Non-airline portals report no base and must be unaffected."""
    config = make_config(home_base={})
    assert not config.home_base.enabled
    poller, adapter, _ = build(tmp_path, [graded("E", base=None)], config)
    assert (await poller.run_once()).claimed == 1


def test_base_code_is_normalised():
    from shift_agent.config import HomeBase

    assert HomeBase(code=" mco ").code == "MCO"
    assert HomeBase(code="").code is None


# --- ordering ----------------------------------------------------------------

async def test_unworkable_shift_reports_schedule_reason_not_grade(tmp_path):
    """An A-grade shift she could not work anyway should not generate an alert."""
    config = make_config(
        availability={
            "timezone": "America/New_York",
            "slots": [{"day": "Monday", "start": "08:00", "end": "08:30"}],
        }
    )
    poller, _, _ = build(tmp_path, [graded("A")], config)
    report = await poller.run_once()

    assert report.alerted == 0
    assert report.verdicts.get(MatchVerdict.OUTSIDE_AVAILABILITY.value) == 1
    assert MatchVerdict.GRADE_NOTIFY_ONLY.value not in report.verdicts
