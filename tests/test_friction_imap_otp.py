"""IMAP OTP extraction - pure regex tests against fixture email bodies."""

from __future__ import annotations

from pathlib import Path

from shift_agent.friction.imap_otp import extract_code

FIXTURES = Path(__file__).parent / "fixtures" / "friction"


def test_extracts_numeric_code():
    text = (FIXTURES / "otp_numeric.txt").read_text()
    assert extract_code(text) == "482913"


def test_extracts_alnum_code_with_custom_pattern():
    text = (FIXTURES / "otp_alnum.txt").read_text()
    assert extract_code(text, pattern=r"\b([A-Z0-9]{6})\b") == "7K9QXZ"


def test_no_match_returns_none():
    text = (FIXTURES / "otp_no_match.txt").read_text()
    assert extract_code(text) is None


def test_picks_first_match_in_range():
    assert extract_code("code: 1234, backup: 999999999999") == "1234"


# --- age filtering -----------------------------------------------------------
# IMAP's SINCE is date-granular, so it cannot enforce a minutes-wide window.
# _is_recent is what actually does, and getting it wrong means returning an
# expired code that fails the login with no diagnostic.

from datetime import UTC, datetime, timedelta

from shift_agent.friction.imap_otp import _is_recent


def _cutoff(minutes: int = 10):
    return datetime.now(UTC) - timedelta(minutes=minutes)


def test_a_message_inside_the_window_is_recent():
    sent = datetime.now(UTC) - timedelta(minutes=2)
    assert _is_recent(sent.strftime("%a, %d %b %Y %H:%M:%S +0000"), _cutoff())


def test_a_message_from_earlier_today_is_not_recent():
    """The exact case SINCE lets through and this has to catch."""
    sent = datetime.now(UTC) - timedelta(hours=6)
    assert not _is_recent(sent.strftime("%a, %d %b %Y %H:%M:%S +0000"), _cutoff())


def test_a_missing_date_fails_closed():
    assert not _is_recent(None, _cutoff())


def test_an_unparseable_date_fails_closed():
    assert not _is_recent("last Tuesday-ish", _cutoff())


def test_a_non_utc_offset_is_compared_correctly():
    """A server stamping +0900 is still recent if the instant is recent."""
    sent = (datetime.now(UTC) - timedelta(minutes=1)).astimezone(
        __import__("datetime").timezone(timedelta(hours=9))
    )
    assert _is_recent(sent.strftime("%a, %d %b %Y %H:%M:%S %z"), _cutoff())
