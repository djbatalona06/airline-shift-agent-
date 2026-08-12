"""RFC 5545 calendar output.

Small, but three details decide whether a file imports cleanly or arrives as
garbage, and all three are easy to get silently wrong:

* **Escaping.** `,` `;` and `\\` are field separators inside TEXT values, and a
  literal newline terminates the property. A trip title containing a comma will
  otherwise split into a second, malformed property.
* **Line folding.** Lines must not exceed 75 octets; continuations begin with a
  single space. Long descriptions are the usual casualty.
* **Stable UIDs.** The UID identifies the event across imports. Derived from the
  shift id, a re-import updates the existing entry; randomised, every rebuild
  stacks another duplicate onto her calendar.

CRLF endings are required by the spec, not a Windows habit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable

PRODID = "-//shift-agent//EN"
DOMAIN = "shift-agent.local"
_MAX_OCTETS = 75


def escape_text(value: str) -> str:
    """Escape a TEXT value. Backslash first, or later escapes get double-escaped."""
    out = value.replace("\\", "\\\\")
    out = out.replace(";", "\\;").replace(",", "\\,")
    out = out.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return out


def fold(line: str) -> str:
    """Fold to 75 octets per line, splitting on encoded length, not characters.

    A multi-byte character split across the boundary would corrupt the file, so
    the walk is byte-wise but only ever breaks between characters.
    """
    if len(line.encode("utf-8")) <= _MAX_OCTETS:
        return line

    pieces: list[str] = []
    current = bytearray()
    limit = _MAX_OCTETS
    for char in line:
        char_bytes = char.encode("utf-8")
        if len(current) + len(char_bytes) > limit:
            pieces.append(current.decode("utf-8"))
            current = bytearray()
            limit = _MAX_OCTETS - 1  # continuation lines carry a leading space
        current += char_bytes
    if current:
        pieces.append(current.decode("utf-8"))
    return "\r\n ".join(pieces)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def make_uid(shift_id: str, namespace: str = DOMAIN) -> str:
    safe = "".join(c if c.isalnum() or c in "-._" else "-" for c in str(shift_id))
    return f"shift-{safe}@{namespace}"


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def build_event(shift: dict[str, Any], now: datetime) -> list[str] | None:
    start = _parse(shift.get("start"))
    end = _parse(shift.get("end"))
    if start is None or end is None:
        return None

    summary = shift.get("title") or "Shift"
    grade = shift.get("grade")
    if grade:
        summary = f"{summary} ({grade})"
    if shift.get("dry_run"):
        summary = f"[DRY RUN] {summary}"

    description_bits = [shift.get("verdict_label") or "", shift.get("detail") or ""]
    description = " - ".join(b for b in description_bits if b)

    return [
        "BEGIN:VEVENT",
        fold(f"UID:{make_uid(shift['id'])}"),
        f"DTSTAMP:{_stamp(now)}",
        f"DTSTART:{_stamp(start)}",
        f"DTEND:{_stamp(end)}",
        fold(f"SUMMARY:{escape_text(summary)}"),
        *([fold(f"DESCRIPTION:{escape_text(description)}")] if description else []),
        f"STATUS:{'CONFIRMED' if shift.get('claimed') and not shift.get('dry_run') else 'TENTATIVE'}",
        "END:VEVENT",
    ]


def build_calendar(
    shifts: Iterable[dict[str, Any]],
    name: str = "Shift agent",
    now: datetime | None = None,
) -> str:
    """Render a VCALENDAR containing every shift that has usable times."""
    now = now or datetime.now(UTC)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        fold(f"X-WR-CALNAME:{escape_text(name)}"),
    ]
    for shift in shifts:
        event = build_event(shift, now)
        if event:
            lines.extend(event)
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def build_markdown(shifts: Iterable[dict[str, Any]], timezone: str = "UTC") -> str:
    """Markdown table for pasting into Obsidian or Notion."""
    rows = [
        "| Date | Time | Shift | Grade | Status |",
        "| --- | --- | --- | --- | --- |",
    ]
    for shift in shifts:
        start, end = _parse(shift.get("start")), _parse(shift.get("end"))
        if start is None:
            continue
        when = f"{start:%H:%M}" + (f"-{end:%H:%M}" if end else "")
        status = shift.get("verdict_label") or ""
        if shift.get("claimed"):
            status = "Claimed (dry run)" if shift.get("dry_run") else "Claimed"
        title = (shift.get("title") or "Shift").replace("|", "\\|")
        rows.append(
            f"| {start:%Y-%m-%d} | {when} | {title} | {shift.get('grade') or ''} | {status} |"
        )
    if len(rows) == 2:
        rows.append("| _no shifts yet_ | | | | |")
    return "\n".join(rows) + f"\n\nTimes shown in {timezone}.\n"
