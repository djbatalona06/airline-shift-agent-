"""The polling loop.

Every shift is re-evaluated on every cycle; only *notification* is deduplicated.
This is deliberate — see `Store.offered_ids`. A shift the user declined or
ignored is not re-offered, because a bot that re-asks every 45 seconds gets
muted, and a muted bot is a broken bot.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import httpx

from .adapters.base import PortalAdapter
from .config import ClaimMode, UserConfig
from .logging_safe import scrub
from .models import AuthState, ClaimOutcome, ClaimResult, MatchResult, MatchVerdict
from .notify.base import Notifier, describe
from .schedule import ScheduleEngine
from .store import (
    LAST_CYCLE_KEY,
    LAST_DIGEST_KEY,
    PAUSE_REASON_KEY,
    PAUSED_KEY,
    Store,
)

log = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 900.0
FAILURES_BEFORE_ALERT = 3
HEARTBEAT_SECONDS = 300.0

# Verdicts that mean "tell her, but never act on it".
_ALERT_VERDICTS = frozenset(
    {MatchVerdict.GRADE_NOTIFY_ONLY, MatchVerdict.MAX_ATTEMPTS_REACHED}
)


@dataclass(slots=True)
class CycleReport:
    skipped: str | None = None
    evaluated: int = 0
    matched: int = 0
    offered: int = 0
    claimed: int = 0
    alerted: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    def note(self, verdict: MatchVerdict) -> None:
        self.verdicts[verdict.value] = self.verdicts.get(verdict.value, 0) + 1


def _within(now: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # window crosses midnight


class Poller:
    def __init__(
        self,
        config: UserConfig,
        adapter: PortalAdapter,
        notifier: Notifier,
        store: Store,
        engine: ScheduleEngine | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        dashboard_dir: "Path | None" = None,
    ) -> None:
        self.config = config
        self.adapter = adapter
        self.notifier = notifier
        self.store = store
        self.dashboard_dir = dashboard_dir
        self.engine = engine or ScheduleEngine(config.availability, config.rules)
        # Injectable so tests can exercise multi-cycle backoff without actually
        # waiting out the exponential delay.
        self.sleep = sleep or asyncio.sleep
        self.consecutive_failures = 0
        self.login_failures = 0
        self._tz = config.availability.tz

    # --- gating --------------------------------------------------------------

    @property
    def paused(self) -> bool:
        return bool(self.store.get(PAUSED_KEY, False))

    def pause(self, reason: str = "") -> None:
        self.store.set(PAUSED_KEY, True)
        self.store.set(PAUSE_REASON_KEY, reason)

    def resume(self) -> None:
        self.store.set(PAUSED_KEY, False)
        self.store.set(PAUSE_REASON_KEY, "")

    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        qh = self.config.poll.quiet_hours
        if qh is None:
            return False
        local = (now or datetime.now(UTC)).astimezone(self._tz).time()
        return _within(local, qh.start, qh.end)

    # --- one cycle -----------------------------------------------------------

    async def run_once(self) -> CycleReport:
        report = CycleReport()

        if self.paused:
            report.skipped = "paused"
            return report
        if self.in_quiet_hours():
            report.skipped = "quiet_hours"
            return report

        if not await self.adapter.is_authenticated():
            auth = await self.adapter.login()
            if auth.state is AuthState.NEEDS_HUMAN:
                self.pause(auth.detail or "challenge")
                await self.notifier.needs_human(
                    auth.detail or "The portal is asking for verification.",
                    auth.challenge_url,
                )
                report.skipped = "needs_human"
                return report
            if not auth.ok:
                self.login_failures += 1
                limit = self.config.rules.max_login_failures
                if self.login_failures >= limit:
                    # Circuit breaker. If her password changed, retrying forever
                    # locks the account out - a far worse outcome than any missed
                    # shift. Stop and hand it to a human.
                    self.pause(f"{self.login_failures} failed logins")
                    await self.notifier.alert(
                        f"Sign-in failed {self.login_failures} times, so I've stopped to avoid "
                        f"locking your account. Check the password, then send /resume."
                    )
                    report.skipped = "login_locked_out"
                    return report
                raise RuntimeError(f"login failed: {auth.detail}")
            self.login_failures = 0

        open_shifts = await self.adapter.fetch_open_shifts()
        assigned = await self.adapter.fetch_my_schedule()
        offered = self.store.offered_ids(self.config.name)
        user = self.config.name

        cutoff = datetime.now(UTC) + timedelta(minutes=self.config.rules.min_lead_minutes)

        # Phase 1 - classify everything. Side-effect free apart from recording,
        # so the verdict counts in the report (and therefore the daily digest)
        # describe the whole offer list, not just the prefix before a claim
        # happened to succeed.
        candidates: list = []
        alerts: list[MatchResult] = []
        for shift in open_shifts:
            result = await self._classify(shift, assigned, cutoff, user)
            shift = result.shift          # enrichment may have returned a new object
            self.store.record_seen(user, result)
            report.evaluated += 1
            report.note(result.verdict)
            if result.matched:
                report.matched += 1
                candidates.append(shift)
            elif result.verdict in _ALERT_VERDICTS:
                alerts.append(result)

        # Phase 1.5 - shifts she should know about but the agent must not claim.
        for result in alerts:
            if result.shift.id in offered:
                continue
            self.store.mark_offered(user, result.shift.id)
            report.alerted += 1
            await self.notifier.info(
                f"Heads up - not claiming this one:\n{describe(result.shift, self._tz)}\n{result.detail}"
            )

        # Phase 2 - act. Stops after the first success because `assigned` is
        # then stale, and any further conflict or rest check this pass would be
        # computed against an out-of-date schedule.
        for shift in candidates:
            if shift.id in offered or self.store.already_claimed(user, shift.id):
                continue

            self.store.mark_offered(user, shift.id)
            report.offered += 1

            if not await self._should_claim(shift):
                continue

            claim = await self._attempt_claim(shift)
            if claim.ok:
                report.claimed += 1
                break

        self.consecutive_failures = 0
        self.store.set(
            LAST_CYCLE_KEY,
            {
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                "evaluated": report.evaluated,
                "matched": report.matched,
                "claimed": report.claimed,
            },
        )
        if self.dashboard_dir is not None:
            # Failure here is swallowed by design: a broken dashboard must never
            # take shift monitoring down with it.
            from .dashboard import try_build_dashboard

            try_build_dashboard(self.store, self.config, self.dashboard_dir)

        await self._maybe_send_digest()
        return report

    async def _maybe_send_digest(self) -> None:
        """Send at most one digest per local day, at or after the configured hour.

        Keyed on the local date rather than an interval so a restart cannot
        cause a second digest, and a downtime window cannot cause a burst.
        """
        hour = self.config.notify.daily_digest_hour
        if hour is None:
            return
        local = datetime.now(UTC).astimezone(self._tz)
        if local.hour < hour:
            return
        today = local.date().isoformat()
        if self.store.get(LAST_DIGEST_KEY) == today:
            return
        self.store.set(LAST_DIGEST_KEY, today)

        since = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        claims = self.store.claims_since(self.config.name, since)
        real = [c for c in claims if c["outcome"] == "claimed" and not c["dry_run"]]
        dry = [c for c in claims if c["outcome"] == "claimed" and c["dry_run"]]

        lines = [f"Daily summary for {local:%a %d %b}"]
        lines.append(f"Claimed: {len(real)}")
        if dry:
            lines.append(f"Would have claimed (dry run): {len(dry)}")
        last = self.store.get(LAST_CYCLE_KEY) or {}
        if last:
            lines.append(f"Last check {last.get('at', '?')}, saw {last.get('evaluated', 0)} shifts")
        await self.notifier.digest(lines)

    def _classify_cheap(self, shift, assigned, cutoff) -> MatchResult:
        """Local checks only — no network, no extra requests.

        Everything here is decided from data already in hand, so a shift that
        fails costs nothing. Order is by cost and by how informative the answer
        is: a trip she cannot work at all should report the schedule reason
        rather than a base or grade one.
        """
        if shift.start <= cutoff:
            return MatchResult(shift, MatchVerdict.TOO_SOON, "starts too soon or already began")

        result = self.engine.evaluate(shift, assigned=assigned)
        if not result.matched:
            return result

        # Wrong domicile is refused outright and *silently* — the pot is full of
        # out-of-base trips and alerting on each would be constant noise.
        base = shift.meta.get("base")
        if not self.config.home_base.allows(base):
            return MatchResult(
                shift,
                MatchVerdict.WRONG_BASE,
                f"base {base or 'unknown'} is not {self.config.home_base.code}",
            )

        if self.config.rules.premium_only and not shift.meta.get("premium"):
            return MatchResult(shift, MatchVerdict.NOT_PREMIUM, "not flagged premium")

        return result

    async def _classify(self, shift, assigned, cutoff, user: str) -> MatchResult:
        """Full classification, including anything that costs a request.

        The cheap stage runs first so `enrich()` is only ever called for shifts
        that already passed base, schedule and premium — on FLICA that turns a
        per-poll detail fetch for the whole pot into a handful of requests.
        """
        result = self._classify_cheap(shift, assigned, cutoff)
        if not result.matched:
            return result

        try:
            shift = await self.adapter.enrich(shift)
        except Exception as exc:
            # Fail closed. An unreachable detail page means an unknown grade,
            # and an unknown grade must never be claimed.
            # Scrubbed: portal error text routinely carries session tokens.
            log.warning("enrich failed for %s: %s", shift.id, scrub(exc))
            return MatchResult(
                shift, MatchVerdict.GRADE_NOTIFY_ONLY, "could not read position - alerting you instead"
            )

        grade = shift.meta.get("grade")
        if not self.config.grades.should_pursue(grade):
            label = f"grade {grade}" if grade else "no grade found"
            return MatchResult(
                shift, MatchVerdict.GRADE_NOTIFY_ONLY, f"{label} - alerting you instead of claiming"
            )

        attempts = self.store.failed_attempts(user, shift.id)
        limit = self.config.rules.max_claim_attempts
        if attempts >= limit:
            return MatchResult(
                shift,
                MatchVerdict.MAX_ATTEMPTS_REACHED,
                f"{attempts} failed attempts (limit {limit}) - not trying again",
            )

        return MatchResult(shift, MatchVerdict.MATCH, result.detail)

    async def _should_claim(self, shift) -> bool:
        mode = self.config.claim_mode
        if mode is ClaimMode.NOTIFY_ONLY:
            await self.notifier.info(f"Open shift matching your schedule:\n{describe(shift, self._tz)}")
            return False
        if mode is ClaimMode.AUTO:
            return True
        return await self.notifier.offer(shift, self.config.notify.confirm_timeout_minutes)

    async def _attempt_claim(self, shift) -> ClaimResult:
        dry = self.config.dry_run
        if dry:
            result = ClaimResult(ClaimOutcome.CLAIMED, "dry run - no request sent")
        else:
            result = await self.adapter.claim(shift.id)

        self.store.record_claim(self.config.name, shift.id, result, dry_run=dry)
        await self.notifier.claim_outcome(shift, result, dry)
        return result

    async def _ping_healthcheck(self) -> None:
        """Dead-man's switch.

        The one failure the agent can never report itself is being dead. An
        external watcher noticing the absence of these pings is what covers a
        powered-off laptop on the free tier.
        """
        url = self.config.healthcheck_url
        if not url:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.get(url)
        except httpx.HTTPError as exc:
            log.warning("healthcheck ping failed: %s", exc)

    # --- loop ----------------------------------------------------------------

    def next_delay(self) -> float:
        poll = self.config.poll
        if self.consecutive_failures:
            backoff = poll.interval_seconds * (2 ** self.consecutive_failures)
            return min(backoff, MAX_BACKOFF_SECONDS)
        jitter = random.uniform(-poll.jitter_seconds, poll.jitter_seconds)
        return max(1.0, poll.interval_seconds + jitter)

    async def _heartbeat_loop(self) -> None:
        """Ping the dead-man's switch on its own clock.

        Deliberately NOT tied to cycle completion: a confirm-mode offer blocks
        the poll loop for up to the confirmation timeout, and a heartbeat tied
        to cycles would go silent for that whole window and raise a false alarm
        that the agent had died.
        """
        while True:
            await self._ping_healthcheck()
            await asyncio.sleep(HEARTBEAT_SECONDS)

    async def run_forever(self, max_cycles: int | None = None) -> None:
        heartbeat = (
            asyncio.create_task(self._heartbeat_loop())
            if self.config.healthcheck_url
            else None
        )
        try:
            await self._loop(max_cycles)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()

    async def _loop(self, max_cycles: int | None) -> None:
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            try:
                report = await self.run_once()
                log.info("cycle: %s", report)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.consecutive_failures += 1
                log.exception("poll cycle failed (%d consecutive)", self.consecutive_failures)
                if self.consecutive_failures == FAILURES_BEFORE_ALERT:
                    await self.notifier.alert(
                        f"Shift agent has failed {self.consecutive_failures} times in a row. "
                        f"Latest error: {exc}"
                    )
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                await self.sleep(self.next_delay())
