"""FLICA parser tests against synthetic fixtures. No browser, no network.

Fixtures are hand-written to match the structure recorded in
docs/RECON-FINDINGS.md. Real captures stay out of the repo.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from shift_agent.adapters.flica import (
    has_captcha,
    parse_open_shifts,
    parse_position,
    parse_request_statuses,
    parse_schedule,
    parse_selected_base,
    status_outcome,
)
from shift_agent.models import ClaimOutcome

FIXTURES = Path(__file__).parent / "fixtures" / "flica"
NY = ZoneInfo("America/New_York")
# Fixed reference so "14AUG" resolves deterministically regardless of run date.
REF = datetime(2026, 8, 11, 12, 0, tzinfo=NY)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def pot() -> str:
    return fixture("otopentimepot.html")


# --- base ---------------------------------------------------------------------

def test_reads_the_selected_base(pot):
    assert parse_selected_base(pot) == "MCO"


def test_missing_base_selector_returns_none():
    assert parse_selected_base("<html><body>no select</body></html>") is None


def test_every_shift_carries_the_base(pot):
    shifts = parse_open_shifts(pot, reference=REF)
    assert shifts and all(s.meta["base"] == "MCO" for s in shifts)


# --- open time ----------------------------------------------------------------

def test_parses_all_pairings(pot):
    shifts = parse_open_shifts(pot, reference=REF)
    assert [s.id for s in shifts] == ["M4A76", "M7C21", "M2B08", "M9E44"]


def test_times_are_timezone_aware_and_correct(pot):
    shift = parse_open_shifts(pot, reference=REF)[0]
    local = shift.start.astimezone(NY)
    assert local.hour == 9 and local.minute == 0        # report 0900
    assert shift.start.tzinfo is not None
    assert shift.end.astimezone(NY).hour == 16          # arrive 1600


def test_premium_flag_read_from_prem_column(pot):
    shifts = {s.id: s for s in parse_open_shifts(pot, reference=REF)}
    assert shifts["M4A76"].meta["premium"] is True
    assert shifts["M2B08"].meta["premium"] is False     # blank Prem cell


def test_multi_day_pairing_ends_on_a_later_day(pot):
    shifts = {s.id: s for s in parse_open_shifts(pot, reference=REF)}
    two_day = shifts["M9E44"]
    assert (two_day.end - two_day.start).days >= 1


def test_detail_link_is_captured_for_enrichment(pot):
    shift = parse_open_shifts(pot, reference=REF)[0]
    assert shift.meta["pairing_id"] == "M4A76"
    assert "RBCPair.cgi" in shift.meta["detail_url"]


def test_layover_becomes_location(pot):
    shifts = {s.id: s for s in parse_open_shifts(pot, reference=REF)}
    assert shifts["M9E44"].location == "DEN"
    assert shifts["M4A76"].location is None            # "-" is not a layover


def test_header_and_chrome_rows_are_skipped(pot):
    ids = [s.id for s in parse_open_shifts(pot, reference=REF)]
    assert "Pairing" not in ids


def test_garbage_html_yields_no_shifts():
    assert parse_open_shifts("<html><body><p>error</p></body></html>") == []


def test_dates_without_a_year_resolve_forward():
    """FLICA omits the year; a January pairing seen in December is next year."""
    html = fixture("otopentimepot.html").replace("14AUG", "05JAN")
    december = datetime(2026, 12, 20, tzinfo=NY)
    shifts = parse_open_shifts(html, reference=december)
    assert shifts[0].start.astimezone(NY).year == 2027


# --- position -----------------------------------------------------------------

def test_reads_the_open_position():
    assert parse_position(fixture("RBCPair.html")) == "E"


def test_position_is_none_when_absent():
    assert parse_position("<html><body>no position here</body></html>") is None


def test_crew_complement_alone_is_not_treated_as_the_offer():
    """FA01FB01... describes the whole crew, not the seat being offered.

    Guessing from it would risk putting her in a position she declined.
    """
    html = "<html><body>Base/Equip: MCO/4FA FA01FB01FC01FD01FE01</body></html>"
    assert parse_position(html) is None


def test_unambiguous_single_position_complement_is_used():
    html = "<html><body>FE01FE01</body></html>"
    assert parse_position(html) == "E"


# --- schedule -----------------------------------------------------------------

def test_parses_assigned_trips():
    shifts = parse_schedule(fixture("cmschedules.html"), reference=REF)
    assert [s.id for s in shifts] == ["M1X09", "M3Z55", "M8Q12"]
    assert all(s.meta.get("assigned") for s in shifts)


def test_assigned_times_are_aware():
    shift = parse_schedule(fixture("cmschedules.html"), reference=REF)[0]
    assert shift.start.tzinfo is not None
    assert shift.start.astimezone(NY).hour == 8


def test_summary_rows_are_ignored():
    ids = [s.id for s in parse_schedule(fixture("cmschedules.html"), reference=REF)]
    assert "Block" not in ids


# --- request statuses ---------------------------------------------------------

def test_parses_request_statuses():
    statuses = parse_request_statuses(fixture("otrequest.html"))
    assert statuses["M4A76"] == "Pending"
    assert statuses["M7C21"] == "Unable"
    assert statuses["M2B08"] == "Awarded"


def test_header_row_is_not_a_status():
    assert "Status" not in parse_request_statuses(fixture("otrequest.html"))


@pytest.mark.parametrize("status,expected", [
    ("Awarded", ClaimOutcome.CLAIMED),
    ("awarded", ClaimOutcome.CLAIMED),
    ("Unable", ClaimOutcome.REJECTED),
    ("Cancelled", ClaimOutcome.REJECTED),
    ("Pending", ClaimOutcome.ERROR),
    ("", ClaimOutcome.ERROR),
])
def test_status_maps_to_outcome(status, expected):
    assert status_outcome(status) is expected


# --- captcha ------------------------------------------------------------------

@pytest.mark.parametrize("markup", [
    '<div class="g-recaptcha"></div>',
    '<script src="https://www.google.com/recaptcha/api.js"></script>',
    '<div class="cf-turnstile"></div>',
])
def test_captcha_detected(markup):
    assert has_captcha(markup)


def test_clean_page_has_no_captcha(pot):
    assert not has_captcha(pot)


# --- the base lock, at the adapter boundary -----------------------------------

def test_parser_never_emits_a_base_it_was_not_given(pot):
    """Base comes from the portal's own selector, never from config.

    If the adapter invented or defaulted a base, the poller's domicile lock
    would pass on shifts that are not actually at her base.
    """
    swapped = pot.replace('value="MCO" selected', 'value="DEN" selected')
    shifts = parse_open_shifts(swapped, reference=REF)
    assert all(s.meta["base"] == "DEN" for s in shifts)


def test_adapter_module_does_not_write_the_base_selector():
    """Guards the rule that the agent must never change domicile."""
    source = (Path(__file__).parents[1] / "src" / "shift_agent" / "adapters" / "flica.py").read_text()
    for forbidden in ("select_option", 'fill("baseList', "baseList\", "):
        assert forbidden not in source
