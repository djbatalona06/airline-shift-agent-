"""Build the public demo dashboard from synthetic data.

Runs in CI to publish GitHub Pages. It builds through the real dashboard code
rather than shipping a hand-made copy, so the demo cannot drift from what the
app actually renders.

Every shift here is invented. Nothing reads a real store, a real config, or
anything on the developer's disk — that is what keeps a public page from ever
carrying somebody's roster.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from shift_agent.config import UserConfig
from shift_agent.dashboard import build_dashboard
from shift_agent.models import ClaimOutcome, ClaimResult, MatchResult, MatchVerdict, Shift
from shift_agent.store import LAST_CYCLE_KEY, Store

USER = "demo"

CONFIG = {
    "name": USER,
    "portal": {"adapter": "mock"},
    "availability": {
        "timezone": "America/New_York",
        "slots": [
            {"day": d, "start": "06:00", "end": "22:00"}
            for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
        ]
        + [{"day": "Saturday", "start": "08:00", "end": "18:00"}],
        "excluded_dates": ["2026-12-25"],
    },
    "rules": {
        "min_rest_hours": 10,
        "max_claim_attempts": 3,
        "premium_only": True,
        "max_weekly_hours": 40,
    },
    "home_base": {"code": "MCO"},
    "grades": {"pursue": ["E", "D", "B"], "notify_only": ["A", "C"]},
    "poll": {"interval_seconds": 45, "quiet_hours": {"start": "23:00", "end": "06:00"}},
    "claim_mode": "confirm",
    "dry_run": False,
}

# (id, title, days ahead, hours, grade, verdict, detail, claim)
SHIFTS = [
    ("M4A76", "MCO-ATL-MCO", 2, 6.0, "E", MatchVerdict.MATCH, "", ("claimed", False)),
    ("M7C21", "MCO-DFW-MCO", 4, 5.7, "D", MatchVerdict.MATCH, "", ("claimed", False)),
    ("M2B08", "MCO-LAS", 5, 5.5, "B", MatchVerdict.MATCH, "", None),
    ("M9E44", "MCO-DEN overnight", 6, 6.5, "A", MatchVerdict.GRADE_NOTIFY_ONLY,
     "grade A - alerting you instead of claiming", None),
    ("M3K19", "MCO-PHX", 7, 7.0, "C", MatchVerdict.GRADE_NOTIFY_ONLY,
     "grade C - alerting you instead of claiming", None),
    ("M5T02", "DEN-SEA", 3, 6.0, "E", MatchVerdict.WRONG_BASE, "base DEN is not MCO", None),
    ("M8W77", "MCO-MIA late", 8, 5.0, "D", MatchVerdict.OUTSIDE_AVAILABILITY,
     "starts 23:40, outside configured windows", None),
    ("M1P55", "MCO-TPA", 9, 4.5, "E", MatchVerdict.CONFLICTS_ASSIGNED,
     "overlaps assigned shift M0Q11", None),
    ("M6R30", "MCO-CVG", 10, 6.0, "B", MatchVerdict.INSUFFICIENT_REST,
     "7.5h rest vs 10.0h required", None),
    ("M0Z88", "MCO-SJU", 11, 8.0, "E", MatchVerdict.MAX_ATTEMPTS_REACHED,
     "3 failed attempts (limit 3) - not trying again", ("rejected", False)),
    ("M2N14", "MCO-CLE", 12, 5.0, None, MatchVerdict.NOT_PREMIUM, "not flagged premium", None),
    ("M4V61", "MCO-ATL", 13, 6.0, "D", MatchVerdict.MATCH, "", ("claimed", True)),
]


def seed(store: Store) -> None:
    now = datetime.now(UTC)
    for sid, title, days, hours, grade, verdict, detail, claim in SHIFTS:
        start = (now + timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
        shift = Shift(
            id=sid,
            start=start,
            end=start + timedelta(hours=hours),
            title=title,
            meta={"base": "MCO", "grade": grade, "premium": True},
        )
        text = detail or (f"grade {grade}" if grade else "")
        store.record_seen(USER, MatchResult(shift, verdict, text))
        if claim:
            outcome, dry = claim
            store.record_claim(USER, sid, ClaimResult(ClaimOutcome(outcome), ""), dry_run=dry)

    store.set(
        LAST_CYCLE_KEY,
        {
            "at": now.isoformat(timespec="seconds"),
            "evaluated": len(SHIFTS),
            "matched": 4,
            "claimed": 2,
        },
    )
    store.set("telegram_linked_chat_id", 1)


def main(outdir: Path) -> int:
    config = UserConfig.model_validate(CONFIG)
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "demo.db")
        try:
            seed(store)
            index = build_dashboard(store, config, outdir)
        finally:
            store.close()

    html = index.read_text(encoding="utf-8")
    if "{{DATA}}" in html:
        print("ERROR: placeholder not replaced", file=sys.stderr)
        return 1
    print(f"demo dashboard: {index} ({len(html) // 1024} KB)")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("site/demo")
    raise SystemExit(main(target))
