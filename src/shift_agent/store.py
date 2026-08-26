"""Operational state — SQLite.

Deliberately holds NO secrets. Portal passwords and session cookies go to the OS
keychain via `secrets.py`; this file is a plain unencrypted database sitting in
the user's profile, and anything in it should be safe to read. Keeping the split
strict means a stray backup or support screenshare of the DB leaks nothing.

Sync sqlite3 inside an async poller is intentional: writes are sub-millisecond
local operations and a connection pool would be more machinery than the workload
justifies.

**Threading.** The dashboard server answers on its own handler threads, so this
connection is reached from more than the poll loop's thread. sqlite3's default
`check_same_thread` guard only compares against the *creating* thread, which
says nothing about concurrency - so it is turned off and replaced with an
explicit lock around every statement. That is the real guarantee, and it costs
nothing at this workload.
"""

from __future__ import annotations

import json
import sqlite3
import threading
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

-- One conversation per profile, shared by every surface that talks to the
-- agent (the dashboard panel and the Telegram bot). Server-side history is what
-- lets a reopened dashboard resume the thread instead of restarting it, with
-- nothing depending on a browser cookie surviving.
--
-- Subject to this module's no-secrets rule like every other table here: the
-- chat agent's tools return schedule and status data only, never a credential.
CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user        TEXT NOT NULL,
    role        TEXT NOT NULL,      -- user | agent
    source      TEXT NOT NULL,      -- dashboard | telegram | system
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS chat_messages_user_id ON chat_messages (user, id);
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
PENDING_CLAIMS_KEY = "pending_claims"
# True when the current pause is a portal challenge, which the agent may clear
# by itself once the challenge is gone. A login-failure pause is not.
CHALLENGE_PAUSE_KEY = "pause_is_challenge"
# Persisted rather than held on the Poller: the account-lockout breaker must
# survive a restart, or a crash loop defeats it. See `Poller.login_failures`.
LOGIN_FAILURES_KEY = "login_failures"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _Result:
    """Rows already read, plus the bits of a cursor callers actually use.

    Returning a live cursor would leave a hole in the locking: sqlite3 fetches
    rows lazily, so iteration would happen after the lock was released. Rows are
    small and bounded here (a profile's shift history), so reading them eagerly
    costs nothing and makes the guarantee real.
    """

    __slots__ = ("_rows", "lastrowid")

    def __init__(self, rows: list[sqlite3.Row], lastrowid: int | None) -> None:
        self._rows = rows
        self.lastrowid = lastrowid

    def __iter__(self):
        return iter(self._rows)

    def fetchall(self) -> list[sqlite3.Row]:
        return self._rows

    def fetchone(self) -> sqlite3.Row | None:
        return self._rows[0] if self._rows else None


class _Connection:
    """A sqlite3 connection with a lock in front of every statement.

    Wrapping rather than subclassing so `store.db.execute(...)` keeps working
    for the callers that read rows directly (`dashboard/data.py`) while still
    being serialised.
    """

    def __init__(self, raw: sqlite3.Connection, lock: threading.Lock) -> None:
        self._raw = raw
        self._lock = lock

    def execute(self, sql: str, parameters=()) -> _Result:
        with self._lock:
            cursor = self._raw.execute(sql, parameters)
            return _Result(cursor.fetchall(), cursor.lastrowid)

    def executescript(self, sql: str) -> None:
        with self._lock:
            self._raw.executescript(sql)

    def close(self) -> None:
        with self._lock:
            self._raw.close()


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because the dashboard server answers each
        # request on its own thread, and the chat assistant reads this database
        # from there while the poll loop writes to it from the main one. Without
        # it every chat lookup fails with a thread-affinity error and the
        # assistant can answer nothing.
        #
        # Safe here because sqlite3 is compiled serialized and every statement
        # below is a single autocommit call — there are no multi-statement
        # transactions to interleave. It is the smallest change that keeps the
        # "no connection pool" decision in the module docstring honest.
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
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

    # --- awaiting a decision -------------------------------------------------
    #
    # Some portals accept a request and decide later, so a submitted claim is
    # not yet an outcome. These ids are the ones the poller must go back and
    # re-read. Kept in kv rather than as a column on `claims` so no schema
    # migration is needed for a database that may already exist in the field.

    def mark_pending_claim(self, user: str, shift_id: str) -> None:
        pending = self.get(PENDING_CLAIMS_KEY) or {}
        ids = pending.get(user) or []
        if shift_id not in ids:
            ids.append(shift_id)
        pending[user] = ids
        self.set(PENDING_CLAIMS_KEY, pending)

    def pending_claims(self, user: str) -> list[str]:
        return list((self.get(PENDING_CLAIMS_KEY) or {}).get(user) or [])

    def clear_pending_claim(self, user: str, shift_id: str) -> None:
        pending = self.get(PENDING_CLAIMS_KEY) or {}
        ids = [i for i in (pending.get(user) or []) if i != shift_id]
        pending[user] = ids
        self.set(PENDING_CLAIMS_KEY, pending)

    def replace_claim_outcome(self, user: str, shift_id: str, result: ClaimResult) -> None:
        """Overwrite the optimistic row written when the request was submitted.

        Updated rather than appended so `failed_attempts` counts one strike per
        real attempt. Appending would make a single rejected request look like
        two, and burn the retry budget twice as fast.
        """
        self.db.execute(
            """
            UPDATE claims SET outcome = ?, detail = ?
            WHERE id = (
                SELECT id FROM claims
                WHERE user = ? AND shift_id = ? AND dry_run = 0
                ORDER BY id DESC LIMIT 1
            )
            """,
            (result.outcome.value, result.detail, user, shift_id),
        )

    def claims_since(self, user: str, since: datetime) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT * FROM claims WHERE user = ? AND created_at >= ? ORDER BY created_at",
                (user, since.astimezone(UTC).isoformat()),
            )
        )

    # --- chat ----------------------------------------------------------------

    def append_message(self, user: str, role: str, source: str, text: str) -> int:
        """Append one turn and return its id.

        The id is what both surfaces poll against, so it is returned rather than
        discarded - the dashboard uses it as its `after` cursor.
        """
        cursor = self.db.execute(
            """
            INSERT INTO chat_messages (user, role, source, text, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user, role, source, text, _now()),
        )
        return int(cursor.lastrowid)

    def messages_after(self, user: str, after_id: int = 0, limit: int = 200) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT * FROM chat_messages WHERE user = ? AND id > ? ORDER BY id LIMIT ?",
                (user, int(after_id), limit),
            )
        )

    def recent_messages(self, user: str, limit: int = 40) -> list[sqlite3.Row]:
        """The last `limit` turns in chronological order.

        Ordered DESC in SQL to take the *newest* rows, then reversed, so a long
        thread gives the model its most recent context rather than its oldest.
        """
        rows = self.db.execute(
            "SELECT * FROM chat_messages WHERE user = ? ORDER BY id DESC LIMIT ?",
            (user, limit),
        ).fetchall()
        return list(reversed(rows))

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
