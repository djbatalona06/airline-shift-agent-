"""Regression tests for defects found in the untested browser layer.

Each of these pinned a behaviour the docs promised and the code did not do.
They are grouped by the defect rather than by module, because the interesting
part is the promise, not the function.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shift_agent.adapters.flica import (
    CAPTCHA_MARKERS,
    _parse_premium,
    has_captcha,
    parse_open_shifts,
)
from shift_agent.adapters.mock import MockAdapter
from shift_agent.config import UserConfig
from shift_agent.models import AuthState, ClaimOutcome, ClaimResult, Shift
from shift_agent.notify.console import ConsoleNotifier
from shift_agent.poller import CHALLENGE_PROBE_START, Poller
from shift_agent.store import CHALLENGE_PAUSE_KEY, PAUSED_KEY, Store

ALL_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def make_config(**over) -> UserConfig:
    raw = {
        "name": "tester",
        "portal": {"adapter": "mock"},
        "availability": {
            "timezone": "UTC",
            "slots": [{"day": d, "start": "00:00", "end": "23:59"} for d in ALL_DAYS],
        },
        "rules": {"min_rest_hours": 0},
        "claim_mode": "auto",
        "dry_run": False,
    }
    raw.update(over)
    return UserConfig.model_validate(raw)


def build(tmp_path, config=None, **adapter_kw):
    config = config or make_config()
    store = Store(tmp_path / "state.db")
    adapter = MockAdapter(config, None, {}, **adapter_kw)
    notifier = ConsoleNotifier(auto_confirm=True)

    async def no_sleep(_):
        return None

    clock = {"now": 0.0}
    poller = Poller(config, adapter, notifier, store, sleep=no_sleep)
    poller._monotonic = lambda: clock["now"]
    return poller, adapter, notifier, store, clock


def future_shift(sid="S1", hours_ahead=48) -> Shift:
    start = datetime.now(UTC) + timedelta(hours=hours_ahead)
    return Shift(id=sid, start=start, end=start + timedelta(hours=5), title="Trip", meta={})


# --------------------------------------------------------------------------
# 3. premium must fail closed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["P", "PREM", "premium", "Y", "yes", "*", " p "])
def test_known_premium_markers_read_as_premium(value):
    assert _parse_premium(value) is True


@pytest.mark.parametrize("value", ["", " ", "-", "N", "no", "–", "—"])
def test_known_blank_markers_read_as_not_premium(value):
    assert _parse_premium(value) is False


@pytest.mark.parametrize("value", ["?", "TBD", "X", "✓", "1"])
def test_unrecognised_premium_marker_fails_closed(value):
    """config.py promises a flag that cannot be read is skipped rather than
    assumed premium. The old rule did the opposite."""
    assert _parse_premium(value) is False


def test_unknown_premium_flag_in_a_real_row_is_not_premium():
    html = """
    <table>
      <tr><td>Pairing</td><td>Dates</td><td>Days</td><td>Report</td><td>Depart</td>
          <td>Arrive</td><td>Blk</td><td>Credit</td><td>OT</td><td>Layover</td><td>Prem</td></tr>
      <tr><td><a href="RBCPair.cgi?PID=M4A76">M4A76</a></td><td>14AUG</td><td>1</td>
          <td>0900</td><td>1000</td><td>1600</td><td>6.0</td><td>6.0</td><td>0</td>
          <td>-</td><td>?</td></tr>
    </table>
    """
    shifts = parse_open_shifts(html, "UTC")
    assert len(shifts) == 1
    assert shifts[0].meta["premium"] is False


# --------------------------------------------------------------------------
# 4. captcha markers must match recon's richer list
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "markup",
    [
        '<div class="g-recaptcha"></div>',
        '<script src="https://www.google.com/recaptcha/api.js"></script>',
        '<div class="h-captcha"></div>',
        '<script src="https://hcaptcha.com/1/api.js"></script>',
        '<div class="cf-turnstile"></div>',
        '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>',
        '<script src="https://client-api.arkoselabs.com/v2/api.js"></script>',
    ],
)
def test_every_challenge_vendor_is_detected(markup):
    assert has_captcha(f"<html><body>{markup}</body></html>") is True


def test_a_clean_page_is_not_a_challenge():
    assert has_captcha("<html><body><table><tr><td>M4A76</td></tr></table></body></html>") is False


def test_adapter_markers_cover_recon_signatures():
    """The two lists drifted once. Keeping them in step is the point."""
    from shift_agent.recon import CAPTCHA_SIGNATURES

    every = {s for group in CAPTCHA_SIGNATURES.values() for s in group}
    assert every.issubset(set(CAPTCHA_MARKERS))


# --------------------------------------------------------------------------
# 2. submitted requests must be reconciled, so three strikes works
# --------------------------------------------------------------------------


async def test_pending_request_is_queued_for_reconciliation(tmp_path):
    poller, adapter, _, store, _ = build(tmp_path)
    adapter._open = [future_shift("S1")]
    await poller.run_once()
    assert store.pending_claims("tester") == ["S1"]


async def test_dry_run_claims_are_never_queued(tmp_path):
    poller, adapter, _, store, _ = build(tmp_path, config=make_config(dry_run=True))
    adapter._open = [future_shift("S1")]
    await poller.run_once()
    assert store.pending_claims("tester") == []


async def test_rejection_becomes_a_failed_attempt(tmp_path):
    """Before this, claim() returned CLAIMED optimistically and the outcome was
    never re-read, so failed_attempts could not rise and the three-strikes rule
    did nothing on the real adapter."""
    poller, adapter, notifier, store, _ = build(tmp_path)
    adapter._open = [future_shift("S1")]
    await poller.run_once()
    assert store.failed_attempts("tester", "S1") == 0

    async def rejected(shift_id):
        return ClaimResult(ClaimOutcome.REJECTED, "Rest requirement not met")

    adapter.check_outcome = rejected
    adapter._open = []
    await poller.run_once()

    assert store.failed_attempts("tester", "S1") == 1
    assert store.pending_claims("tester") == []
    assert any("rejected" in text.lower() for _, text in notifier.sent)


async def test_reconciling_replaces_rather_than_appends(tmp_path):
    """One real attempt must cost one strike, not two."""
    poller, adapter, _, store, _ = build(tmp_path)
    adapter._open = [future_shift("S1")]
    await poller.run_once()

    async def rejected(shift_id):
        return ClaimResult(ClaimOutcome.REJECTED, "Unable")

    adapter.check_outcome = rejected
    adapter._open = []
    await poller.run_once()

    rows = store.db.execute(
        "SELECT COUNT(*) AS n FROM claims WHERE user='tester' AND shift_id='S1'"
    ).fetchone()
    assert rows["n"] == 1
    assert store.failed_attempts("tester", "S1") == 1


async def test_still_pending_is_neither_a_win_nor_a_strike(tmp_path):
    poller, adapter, _, store, _ = build(tmp_path)
    adapter._open = [future_shift("S1")]
    await poller.run_once()

    async def undecided(shift_id):
        return None

    adapter.check_outcome = undecided
    adapter._open = []
    await poller.run_once()

    assert store.failed_attempts("tester", "S1") == 0
    assert store.pending_claims("tester") == ["S1"]  # still queued for next time


async def test_a_failing_outcome_check_leaves_the_claim_queued(tmp_path):
    poller, adapter, _, store, _ = build(tmp_path)
    adapter._open = [future_shift("S1")]
    await poller.run_once()

    async def boom(shift_id):
        raise RuntimeError("portal blip")

    adapter.check_outcome = boom
    adapter._open = []
    await poller.run_once()

    assert store.pending_claims("tester") == ["S1"]


# --------------------------------------------------------------------------
# 5. a challenge pause must be able to clear itself
# --------------------------------------------------------------------------


async def test_challenge_pause_is_marked_recoverable(tmp_path):
    poller, adapter, _, store, _ = build(tmp_path, auth_state=AuthState.NEEDS_HUMAN)
    await poller.run_once()
    assert store.get(PAUSED_KEY) is True
    assert store.get(CHALLENGE_PAUSE_KEY) is True


async def test_agent_resumes_itself_once_the_challenge_clears(tmp_path):
    poller, adapter, notifier, store, clock = build(tmp_path, auth_state=AuthState.NEEDS_HUMAN)
    report = await poller.run_once()
    assert report.skipped == "needs_human"

    # Someone clears it in the browser, without ever sending /resume.
    adapter._auth_state = AuthState.OK
    adapter._authed = True
    clock["now"] += CHALLENGE_PROBE_START + 1
    await poller.run_once()

    assert store.get(PAUSED_KEY) is False
    assert any("challenge is gone" in text.lower() for _, text in notifier.sent)


async def test_probe_is_not_run_on_every_wake(tmp_path):
    """Re-probing every 45s against a portal that just challenged us is exactly
    the traffic that earns another challenge."""
    poller, adapter, _, store, clock = build(tmp_path, auth_state=AuthState.NEEDS_HUMAN)
    await poller.run_once()
    before = adapter.login_calls

    for _ in range(5):
        clock["now"] += 1
        assert (await poller.run_once()).skipped == "paused"

    assert adapter.login_calls == before  # never even tried


async def test_probe_interval_widens_while_unattended(tmp_path):
    poller, _, _, _, clock = build(tmp_path, auth_state=AuthState.NEEDS_HUMAN)
    await poller.run_once()
    first = poller._probe_backoff

    clock["now"] += first + 1
    await poller.run_once()
    assert poller._probe_backoff > first


async def test_login_failure_pause_never_self_resumes(tmp_path):
    """Retrying a bad password is how an account gets locked, so that pause
    stays put until a human intervenes."""
    config = make_config(rules={"min_rest_hours": 0, "max_login_failures": 1})
    poller, adapter, _, store, clock = build(tmp_path, config=config, auth_state=AuthState.FAILED)
    await poller.run_once()
    assert store.get(PAUSED_KEY) is True
    assert store.get(CHALLENGE_PAUSE_KEY) is False

    adapter._auth_state = AuthState.OK
    adapter._authed = True
    clock["now"] += 10_000
    assert (await poller.run_once()).skipped == "paused"


async def test_manual_pause_is_not_undone_by_a_stale_challenge_flag(tmp_path):
    """A human pausing must outrank the agent's own recovery logic."""
    poller, adapter, _, store, clock = build(tmp_path, auth_state=AuthState.NEEDS_HUMAN)
    await poller.run_once()  # sets the challenge flag

    adapter._auth_state = AuthState.OK
    adapter._authed = True
    poller.resume()

    # Now a deliberate pause, the way /pause does it.
    store.set(PAUSED_KEY, True)
    store.set(CHALLENGE_PAUSE_KEY, False)
    clock["now"] += 10_000

    assert (await poller.run_once()).skipped == "paused"
    assert store.get(PAUSED_KEY) is True
