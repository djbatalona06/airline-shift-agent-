"""Shape store rows into the JSON the dashboard renders.

Pure apart from reading the store, so it is testable without a browser, a
server, or a portal.

Two invariants this module enforces:

* **No secrets, ever.** Passwords, cookies, and the Telegram token live in the
  OS keychain and must never reach the page. The natural next step after
  building a dashboard is emailing it to someone.
* **Dry-run claims stay labelled as dry runs** all the way to the screen. During
  the planned dry-run week every claim is simulated, and a page that showed them
  as real would tell her she holds shifts she never got.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..config import UserConfig
from ..models import MatchVerdict
from ..store import LAST_CYCLE_KEY, PAUSE_REASON_KEY, PAUSED_KEY, Store

_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

VERDICT_LABELS = {
    MatchVerdict.MATCH: "Matched",
    MatchVerdict.OUTSIDE_AVAILABILITY: "Outside your hours",
    MatchVerdict.EXCLUDED_DATE: "Excluded date",
    MatchVerdict.CONFLICTS_ASSIGNED: "Clashes with a trip",
    MatchVerdict.INSUFFICIENT_REST: "Not enough rest",
    MatchVerdict.EXCEEDS_WEEKLY_CAP: "Over weekly cap",
    MatchVerdict.ALREADY_SEEN: "Already seen",
    MatchVerdict.TOO_SOON: "Starts too soon",
    MatchVerdict.GRADE_NOTIFY_ONLY: "Alert only",
    MatchVerdict.MAX_ATTEMPTS_REACHED: "Gave up",
    MatchVerdict.WRONG_BASE: "Different base",
    MatchVerdict.NOT_PREMIUM: "Not premium",
}

# Verdicts worth surfacing on the calendar and in the shift catalog.
_INTERESTING = {
    MatchVerdict.MATCH.value,
    MatchVerdict.GRADE_NOTIFY_ONLY.value,
    MatchVerdict.MAX_ATTEMPTS_REACHED.value,
}


def _label(verdict: str) -> str:
    """Human label for a verdict, degrading rather than raising.

    KeyError is caught as well as ValueError: adding a verdict to the enum and
    forgetting a label here must not take the whole dashboard down. A slightly
    unpolished label is a cosmetic problem; an exception is a blank page.
    """
    try:
        return VERDICT_LABELS[MatchVerdict(verdict)]
    except (ValueError, KeyError):
        return str(verdict).replace("_", " ").capitalize()


def _iso(value: Any) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).astimezone(UTC).isoformat()
    except (TypeError, ValueError):
        return None


def build_status(store: Store, config: UserConfig) -> dict[str, Any]:
    paused = bool(store.get(PAUSED_KEY, False))
    last = store.get(LAST_CYCLE_KEY) or {}
    return {
        "paused": paused,
        "pause_reason": store.get(PAUSE_REASON_KEY) or "",
        "dry_run": config.dry_run,
        "claim_mode": config.claim_mode.value,
        "last_cycle_at": last.get("at"),
        "last_evaluated": last.get("evaluated", 0),
        "last_matched": last.get("matched", 0),
    }


def build_settings(store: Store, config: UserConfig) -> dict[str, Any]:
    """Read-only view of configuration. Deliberately excludes every secret."""
    a = config.availability
    return {
        "profile": config.name,
        "timezone": a.timezone,
        "portal": config.portal.adapter,
        "claim_mode": config.claim_mode.value,
        "dry_run": config.dry_run,
        "min_rest_hours": config.rules.min_rest_hours,
        "min_lead_minutes": config.rules.min_lead_minutes,
        "max_claim_attempts": config.rules.max_claim_attempts,
        "max_weekly_hours": config.rules.max_weekly_hours,
        "max_shifts_per_week": config.rules.max_shifts_per_week,
        "grades_enabled": config.grades.enabled,
        "grades_pursue": list(config.grades.pursue),
        "grades_notify": list(config.grades.notify_only),
        "poll_seconds": config.poll.interval_seconds,
        "quiet_hours": (
            f"{config.poll.quiet_hours.start:%H:%M}-{config.poll.quiet_hours.end:%H:%M}"
            if config.poll.quiet_hours
            else None
        ),
        "availability": [
            {
                "day": _DAY_NAMES[s.day],
                "start": f"{s.start:%H:%M}",
                "end": f"{s.end:%H:%M}",
                "overnight": s.crosses_midnight,
            }
            for s in a.slots
        ],
        "excluded_dates": [str(d) for d in a.excluded_dates],
        "telegram_linked": store.get("telegram_linked_chat_id") is not None,
    }


def build_shifts(store: Store, config: UserConfig, limit: int = 200) -> list[dict[str, Any]]:
    rows = store.db.execute(
        "SELECT * FROM seen_shifts WHERE user = ? ORDER BY COALESCE(starts_at, last_seen) DESC LIMIT ?",
        (config.name, limit),
    ).fetchall()

    claims = {}
    for row in store.db.execute(
        "SELECT shift_id, outcome, dry_run, created_at FROM claims WHERE user = ? ORDER BY id",
        (config.name,),
    ):
        claims[row["shift_id"]] = row

    out = []
    for row in rows:
        claim = claims.get(row["shift_id"])
        out.append(
            {
                "id": row["shift_id"],
                "title": row["title"] or "",
                "start": _iso(row["starts_at"]),
                "end": _iso(row["ends_at"]),
                "verdict": row["verdict"],
                "verdict_label": _label(row["verdict"]),
                "detail": row["detail"] or "",
                "grade": _grade_from(row),
                "interesting": row["verdict"] in _INTERESTING,
                "claimed": bool(claim and claim["outcome"] == "claimed"),
                "claim_outcome": claim["outcome"] if claim else None,
                # Carried all the way to the screen; the page must render these
                # differently from real claims.
                "dry_run": bool(claim["dry_run"]) if claim else False,
            }
        )
    return out


def _grade_from(row) -> str | None:
    """Recover the grade from the recorded detail text.

    `seen_shifts` has no grade column — grade lives in `Shift.meta`, which is not
    persisted. Rather than migrate the schema for a display nicety, the poller
    writes it into the verdict detail and it is parsed back out here. Returns
    None when absent, and the page simply omits the badge.
    """
    detail = (row["detail"] or "").lower()
    marker = "grade "
    if marker not in detail:
        return None
    tail = detail.split(marker, 1)[1].strip()
    return tail[:1].upper() if tail and tail[0].isalpha() else None


def build_metrics(shifts: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "seen": len(shifts),
        "matched": sum(1 for s in shifts if s["verdict"] == MatchVerdict.MATCH.value),
        "claimed": sum(1 for s in shifts if s["claimed"] and not s["dry_run"]),
        "dry_run_claims": sum(1 for s in shifts if s["claimed"] and s["dry_run"]),
        "alerts": sum(1 for s in shifts if s["verdict"] == MatchVerdict.GRADE_NOTIFY_ONLY.value),
        "lost": sum(1 for s in shifts if s["claim_outcome"] == "lost_race"),
    }


def build_verdict_breakdown(shifts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for s in shifts:
        counts[s["verdict"]] = counts.get(s["verdict"], 0) + 1
    return [
        {"verdict": v, "label": _label(v), "count": n}
        for v, n in sorted(counts.items(), key=lambda kv: -kv[1])
    ]


def build_payload(store: Store, config: UserConfig) -> dict[str, Any]:
    shifts = build_shifts(store, config)
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "timezone": config.availability.timezone,
        "status": build_status(store, config),
        "settings": build_settings(store, config),
        "metrics": build_metrics(shifts),
        "verdicts": build_verdict_breakdown(shifts),
        "shifts": shifts,
    }
