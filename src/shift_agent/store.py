"""Operational state — SQLite.

Deliberately holds NO secrets. Portal passwords and session cookies go to the OS
keychain via `secrets.py`; this file is a plain unencrypted database sitting in
the user's profile, and anything in it should be safe to read. Keeping the split
strict means a stray backup or support screenshare of the DB leaks nothing.

Sync sqlite3 inside an async poller is intentional: writes are sub-millisecond
local operations and a connection pool would be more machinery than the workload
justifies.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import ClaimResult, MatchResult, Shift

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_shifts (
    user        TEXT NOT NULL,
    shift_id    TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    verdict     TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    starts_at   TEXT,
    ends_at     TEXT,
    title       TEXT NOT NULL DEFAULT '',
    offered_at  TEXT,
    PRIMARY KEY (user, shift_id)
);

CREATE TABLE IF NOT EXISTS claims (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user        TEXT NOT NULL,
    shift_id    TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    dry_run     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- Dry-run rows are excluded: a dry-run "claim" must never occupy the slot that
-- blocks a later real claim of the same shift, or the week-long dry run would
-- poison every shift it evaluated before go-live.
CREATE UNIQUE INDEX IF NOT EXISTS claims_success_unique
    ON claims (user, shift_id) WHERE outcome = 'claimed' AND dry_run = 0;

CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


# Shared kv keys. They live here rather than in poller.py because the notifier
# reads and writes the same state (/pause, /status) and importing them from the
# poller would be circular.
PAUSED_KEY = "paused"
PAUSE_REASON_KEY = "pause_reason"
LAST_CYCLE_KEY = "last_cycle"
LAST_DIGEST_KEY = "last_digest_date"
TELEGRAM_OFFSET_KEY = "telegram_update_offset"
TELEGRAM_CHAT_KEY = "telegram_linked_chat_id"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        # WAL + synchronous=FULL so a hard kill (Windows Update reboot mid-poll)
        # cannot lose an already-recorded claim and cause a duplicate on restart.
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- seen shifts ---------------------------------------------------------

    def seen_ids(self, user: str) -> set[str]:
        rows = self.db.execute("SELECT shift_id FROM seen_shifts WHERE user = ?", (user,))
        return {r["shift_id"] for r in rows}

    def offered_ids(self, user: str) -> set[str]:
        """Shifts already presented to the user for a decision.

        Notification is gated on this rather than on `seen_ids`, because every
        shift must be RE-evaluated on every poll: if it were gated on seen, a
        shift rejected under an old availability window would stay rejected
        forever after the user edits her schedule.
        """
        rows = self.db.execute(
            "SELECT shift_id FROM seen_shifts WHERE user = ? AND offered_at IS NOT NULL",
            (user,),
        )
        return {r["shift_id"] for r in rows}

    def mark_offered(self, user: str, shift_id: str) -> None:
        self.db.execute(
            "UPDATE seen_shifts SET offered_at = ? WHERE user = ? AND shift_id = ?",
            (_now(), user, shift_id),
        )

    def record_seen(self, user: str, result: MatchResult) -> None:
        s: Shift = result.shift
        now = _now()
        self.db.execute(
            """
            INSERT INTO seen_shifts
                (user, shift_id, first_seen, last_seen, verdict, detail, starts_at, ends_at, title)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user, shift_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                verdict   = excluded.verdict,
                detail    = excluded.detail
            """,
            (
                user, s.id, now, now, result.verdict.value, result.detail,
                s.start.isoformat(), s.end.isoformat(), s.title,
            ),
        )

    # --- claims --------------------------------------------------------------

    def already_claimed(self, user: str, shift_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM claims "
            "WHERE user = ? AND shift_id = ? AND outcome = 'claimed' AND dry_run = 0",
            (user, shift_id),
        ).fetchone()
        return row is not None

    def record_claim(
        self, user: str, shift_id: str, result: ClaimResult, dry_run: bool = False
    ) -> bool:
        """Persist a claim attempt.

        Returns False if a successful claim for this shift already existed — the
        unique partial index makes double-claiming impossible even if the poller
        is restarted mid-flight or two processes race.
        """
        try:
            self.db.execute(
                """
                INSERT INTO claims (user, shift_id, outcome, detail, dry_run, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user, shift_id, result.outcome.value, result.detail, int(dry_run), _now()),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def failed_attempts(self, user: str, shift_id: str) -> int:
        """Count real, unsuccessful claim attempts for one shift.

        Dry runs are excluded deliberately: a simulated claim never actually
        asked the portal for anything, so the planned dry-run week must not
        silently burn the retry budget before the agent goes live.
        """
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM claims "
            "WHERE user = ? AND shift_id = ? AND outcome != 'claimed' AND dry_run = 0",
            (user, shift_id),
        ).fetchone()
        return int(row["n"]) if row else 0

    def claims_since(self, user: str, since: datetime) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT * FROM claims WHERE user = ? AND created_at >= ? ORDER BY created_at",
                (user, since.astimezone(UTC).isoformat()),
            )
        )

    # --- key/value -----------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.db.execute(
            """
            INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, json.dumps(value), _now()),
        )
