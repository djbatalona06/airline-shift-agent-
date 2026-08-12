"""Dashboard assembly.

Renders the template with a JSON payload and writes the output set atomically.
A half-written page must never be openable, and a build failure must never be
allowed to stop shift monitoring — a broken dashboard is an annoyance, a stopped
agent means missed shifts.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..config import UserConfig
from ..store import Store
from .data import build_payload
from .ical import build_calendar, build_markdown

log = logging.getLogger(__name__)

PLACEHOLDER = "{{DATA}}"
INDEX = "index.html"
ICS = "shifts.ics"
MARKDOWN = "shifts.md"


def template_path() -> Path:
    """Locate template.html in both source and PyInstaller builds."""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidate = Path(bundled) / "shift_agent" / "dashboard" / "template.html"
        if candidate.is_file():
            return candidate
        candidate = Path(bundled) / "template.html"
        if candidate.is_file():
            return candidate
    return Path(__file__).with_name("template.html")


def _embed(payload: dict[str, Any]) -> str:
    """Serialise for embedding inside a <script> element.

    `</` is escaped because a literal `</script>` anywhere in the data — a shift
    title, a portal error message — would terminate the script element early and
    break the page. `<\\/` is equivalent inside JSON and inert to the parser.
    """
    return json.dumps(payload, default=str).replace("</", "<\\/")


def render(payload: dict[str, Any], template: str | None = None) -> str:
    html = template if template is not None else template_path().read_text(encoding="utf-8")
    if PLACEHOLDER not in html:
        raise ValueError("dashboard template is missing its {{DATA}} placeholder")
    return html.replace(PLACEHOLDER, _embed(payload))


def _write_atomic(path: Path, content: str) -> None:
    """Write via a temp file in the same directory, then replace.

    Same directory because os.replace is only atomic within one filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def build_dashboard(store: Store, config: UserConfig, outdir: str | Path) -> Path:
    """Write index.html, shifts.ics and shifts.md. Returns the index path."""
    outdir = Path(outdir)
    payload = build_payload(store, config)

    calendar = build_calendar(payload["shifts"], name=f"Shift agent — {config.name}")
    markdown = build_markdown(payload["shifts"], timezone=payload["timezone"])

    # Carried in the payload so the copy button works with no network call.
    payload["markdown"] = markdown
    payload["ics_url"] = ICS

    _write_atomic(outdir / ICS, calendar)
    _write_atomic(outdir / MARKDOWN, markdown)
    index = outdir / INDEX
    _write_atomic(index, render(payload))
    return index


def try_build_dashboard(store: Store, config: UserConfig, outdir: str | Path) -> Path | None:
    """Build, swallowing any failure.

    Called from the poll loop, where raising would take shift monitoring down
    with it. The exception is logged rather than lost.
    """
    try:
        return build_dashboard(store, config, outdir)
    except Exception:
        log.exception("dashboard build failed; polling continues")
        return None
