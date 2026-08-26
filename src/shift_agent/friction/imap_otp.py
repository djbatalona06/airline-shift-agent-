"""IMAP-based one-time-code retrieval.

Splits the same way `adapters/flica.py` splits parsing from browser work:
`extract_code` is a pure function tested against fixture email bodies with no
network; `fetch_latest_otp` does the actual IMAP polling and is never
exercised by a test that runs in CI by default. Stdlib only (`imaplib`,
`email`) - no new dependency for this piece.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

DEFAULT_CODE_PATTERN = r"\b(\d{4,8})\b"


@dataclass(frozen=True)
class ImapConfig:
    host: str
    username: str
    port: int = 993
    mailbox: str = "INBOX"
    use_ssl: bool = True


@dataclass(frozen=True)
class OtpSearch:
    sender_contains: str | None = None
    subject_contains: str | None = None
    since_minutes: int = 10
    code_pattern: str = DEFAULT_CODE_PATTERN


def extract_code(text: str, pattern: str = DEFAULT_CODE_PATTERN) -> str | None:
    """Pull the first code matching `pattern` out of an already-decoded
    subject/body string. Pure - no I/O, so this is what the tests exercise."""
    match = re.search(pattern, text)
    return match.group(1) if match else None


def fetch_latest_otp(
    cfg: ImapConfig,
    password: str,
    search: OtpSearch,
    *,
    poll_interval_s: float = 5.0,
    timeout_s: float = 120.0,
) -> str | None:
    """Poll `cfg.mailbox` for a recent message matching `search` and return
    its extracted code, or None on timeout. Real network/IMAP I/O.

    One connection for the whole call, not one per poll. Reconnecting and
    re-authenticating every few seconds is a pattern Gmail and Outlook throttle
    and can temporarily lock on - and locking the mailbox mid-login is exactly
    the situation this function is called in.
    """
    import imaplib
    from datetime import UTC, datetime, timedelta

    deadline = time.monotonic() + timeout_s
    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=search.since_minutes)
    # IMAP SINCE is date-granular, so it can only narrow the fetch to a day.
    # It is a prefilter; `_is_recent` below is what actually enforces the
    # window. Backdated one day so a cutoff just after local midnight, or a
    # server on a different date, does not exclude the message being waited for.
    since = (cutoff - timedelta(days=1)).strftime("%d-%b-%Y")

    conn = (
        imaplib.IMAP4_SSL(cfg.host, cfg.port)
        if cfg.use_ssl
        else imaplib.IMAP4(cfg.host, cfg.port)
    )
    try:
        conn.login(cfg.username, password)
        while True:
            # Re-selected each pass: SELECT is what refreshes the mailbox view,
            # so without it a long-lived connection never sees new mail.
            conn.select(cfg.mailbox)
            _, data = conn.search(None, f"(SINCE {since})")
            for num in reversed((data[0] or b"").split()):
                _, msg_data = conn.fetch(num, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue
                msg = _parse_message(msg_data[0][1])
                sender = str(msg.get("From", ""))
                subject = str(msg.get("Subject", ""))
                if search.sender_contains and search.sender_contains.lower() not in sender.lower():
                    continue
                if search.subject_contains and search.subject_contains.lower() not in subject.lower():
                    continue
                # The whole point of an OTP is that it expires. Returning a code
                # from a message hours old looks like success and then fails the
                # login with no diagnostic, so age is checked before the regex.
                if not _is_recent(msg.get("Date"), cutoff):
                    continue
                body = _extract_body(msg)
                code = extract_code(f"{subject}\n{body}", search.code_pattern)
                if code:
                    return code

            if time.monotonic() + poll_interval_s >= deadline:
                return None
            time.sleep(poll_interval_s)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _is_recent(raw_date: object, cutoff) -> bool:
    """Whether a message's Date header is at or after `cutoff`.

    An unparseable or absent Date is treated as NOT recent. Failing closed here
    means a stale code is skipped and the caller keeps waiting, which is
    recoverable; the alternative hands back a code that will be rejected.
    """
    from email.utils import parsedate_to_datetime

    if not raw_date:
        return False
    try:
        sent = parsedate_to_datetime(str(raw_date))
    except (TypeError, ValueError):
        return False
    if sent is None:
        return False
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=cutoff.tzinfo)
    return sent >= cutoff


def _parse_message(raw: bytes):
    import email

    return email.message_from_bytes(raw)


def _extract_body(msg) -> str:
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
        return "\n".join(parts)
    payload = msg.get_payload(decode=True)
    if payload:
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return str(msg.get_payload())
