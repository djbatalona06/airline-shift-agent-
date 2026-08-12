"""Login security and secret containment.

The circuit breaker is the most consequential thing in this file. If her portal
password changes and the agent keeps retrying, it will lock her account — a far
worse outcome than any missed shift, and one she would have to phone crew
support to undo.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from shift_agent.adapters.mock import MockAdapter
from shift_agent.config import UserConfig
from shift_agent.models import AuthState, MatchVerdict, Shift
from shift_agent.notify.console import ConsoleNotifier
from shift_agent.poller import Poller
from shift_agent.store import PAUSED_KEY, Store

ALL_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
PASSWORD = "hunter2-not-in-logs"
TOKEN = "000000007F98011B01DD29FD01B3169E"


def make_config(**over) -> UserConfig:
    base = {
        "name": "tester",
        "portal": {"adapter": "mock"},
        "availability": {
            "timezone": "America/New_York",
            "slots": [{"day": d, "start": "00:00", "end": "00:00"} for d in ALL_DAYS],
        },
        "rules": {"min_rest_hours": 0},
        "dry_run": False,
    }
    base.update(over)
    return UserConfig.model_validate(base)


def build(tmp_path, config=None, **adapter_kw):
    config = config or make_config()
    start = datetime.now(UTC) + timedelta(days=2)
    shifts = [Shift(id="S1", start=start, end=start + timedelta(hours=6), title="Trip")]
    adapter = MockAdapter(config, open_shifts=shifts, **adapter_kw)
    notifier = ConsoleNotifier(auto_confirm=True)
    store = Store(tmp_path / "state.db")

    async def no_sleep(_):
        return None

    return Poller(config, adapter, notifier, store, sleep=no_sleep), adapter, notifier, store


# --- account lockout circuit breaker -----------------------------------------

async def test_breaker_stops_after_configured_failures(tmp_path):
    poller, adapter, notifier, store = build(tmp_path, auth_state=AuthState.FAILED)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="login failed"):
            await poller.run_once()

    report = await poller.run_once()          # third failure trips the breaker

    assert report.skipped == "login_locked_out"
    assert store.get(PAUSED_KEY) is True
    assert any("locking your account" in text for _, text in notifier.sent)


async def test_no_further_login_attempted_once_tripped(tmp_path):
    """The whole point: stop touching the account, not just stop claiming."""
    poller, adapter, _, _ = build(tmp_path, auth_state=AuthState.FAILED)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await poller.run_once()
    await poller.run_once()
    calls_when_tripped = adapter.login_calls

    for _ in range(5):
        await poller.run_once()

    assert adapter.login_calls == calls_when_tripped


async def test_breaker_threshold_is_configurable(tmp_path):
    config = make_config(rules={"min_rest_hours": 0, "max_login_failures": 1})
    poller, _, _, store = build(tmp_path, config, auth_state=AuthState.FAILED)

    report = await poller.run_once()
    assert report.skipped == "login_locked_out"
    assert store.get(PAUSED_KEY) is True


async def test_successful_login_resets_the_counter(tmp_path):
    poller, adapter, _, _ = build(tmp_path, auth_state=AuthState.FAILED)
    with pytest.raises(RuntimeError):
        await poller.run_once()
    assert poller.login_failures == 1

    adapter._auth_state = AuthState.OK
    await poller.run_once()
    assert poller.login_failures == 0


async def test_captcha_does_not_count_toward_lockout(tmp_path):
    """A captcha is not a bad password; it must not burn the retry budget."""
    poller, _, _, _ = build(tmp_path, auth_state=AuthState.NEEDS_HUMAN)
    report = await poller.run_once()

    assert report.skipped == "needs_human"
    assert poller.login_failures == 0


# --- secret containment ------------------------------------------------------

async def test_store_never_holds_credentials(tmp_path):
    poller, _, _, store = build(tmp_path)
    await poller.run_once()

    dumped = "".join(
        str(row) for row in store.db.execute(
            "SELECT * FROM kv UNION ALL SELECT shift_id, verdict, detail FROM seen_shifts"
        )
    ).lower()
    assert PASSWORD.lower() not in dumped
    assert TOKEN.lower() not in dumped
    for word in ("password", "cookie"):
        assert word not in dumped


async def test_secrets_do_not_reach_log_records(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="shift_agent")
    config = make_config()
    poller, adapter, _, _ = build(tmp_path, config)

    async def failing_enrich(shift):
        raise RuntimeError(f"portal rejected token={TOKEN} for password={PASSWORD}")

    adapter.enrich = failing_enrich
    await poller.run_once()

    emitted = " ".join(r.getMessage() for r in caplog.records)
    assert "enrich failed" in emitted           # the failure was logged at all
    assert PASSWORD not in emitted
    assert TOKEN not in emitted


def test_dashboard_payload_excludes_secrets(tmp_path):
    from shift_agent.dashboard.data import build_payload

    store = Store(tmp_path / "state.db")
    store.set("telegram_linked_chat_id", 4242)
    blob = str(build_payload(store, make_config()))
    assert PASSWORD not in blob and TOKEN not in blob


# --- the scrubber itself -----------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "password=hunter2",
        "Password: hunter2",
        "token=000000007F98011B01DD29FD01B3169E",
        "sessionId: abc123def456abc123def456",
        "api_key = sk-abcdef",
        "Authorization: Bearer abc123",
    ],
)
def test_scrub_removes_labelled_secrets(raw):
    from shift_agent.logging_safe import scrub

    cleaned = scrub(raw)
    assert "[REDACTED]" in cleaned or "[HEX-TOKEN]" in cleaned


def test_scrub_removes_unlabelled_hex_and_jwt():
    from shift_agent.logging_safe import scrub

    assert TOKEN not in scrub(f"rejected {TOKEN}")
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.abcdefghijklmnop"
    assert jwt not in scrub(f"bad {jwt}")


def test_scrub_keeps_the_useful_part_of_the_message():
    from shift_agent.logging_safe import scrub

    cleaned = scrub("HTTP 500 from otrequest.cgi with token=DEADBEEFDEADBEEFDEADBEEF")
    assert "HTTP 500" in cleaned and "otrequest.cgi" in cleaned


def test_filter_scrubs_args_not_just_message():
    from shift_agent.logging_safe import SecretScrubbingFilter

    record = logging.LogRecord(
        "x", logging.WARNING, __file__, 1, "failed: %s", (f"token={TOKEN}",), None
    )
    SecretScrubbingFilter().filter(record)
    assert TOKEN not in record.getMessage()


def test_filter_never_drops_records():
    """A leak is bad; losing the fact an error happened is worse."""
    from shift_agent.logging_safe import SecretScrubbingFilter

    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "password=abc", None, None)
    assert SecretScrubbingFilter().filter(record) is True
