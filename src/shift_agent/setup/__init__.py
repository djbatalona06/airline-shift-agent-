"""First-run setup/picker window.

Reuses the same serve-then-show mechanism the dashboard window already uses
(`main._open_window` / `main._show`) rather than pywebview's `js_api` bridge,
so every fallback `_show()` already has (pywebview -> Edge/Chrome app mode ->
default browser) keeps working for this window too - a page relying on
`js_api` would go dead the moment pywebview itself can't render, and
docs/INSTALL.md already documents that as a real scenario (missing WebView2).
`index.html` talks to Python over plain `fetch()` against the `api/setup/*`
routes added to `dashboard/server.py`.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

from ..dashboard.server import DashboardServer
from .api import SetupAPI

log = logging.getLogger(__name__)

# How long open_setup_window() waits, once the window is showing, for a
# profile to be chosen or saved. Generous - filling in a first-run form is not
# a race. Only matters for the Edge/Chrome/webbrowser fallback path: the
# pywebview path has already fully blocked inside _show() by the time this
# wait starts, so it returns immediately there regardless of the timeout.
_WAIT_TIMEOUT_S = 1800.0


def template_path() -> Path:
    """Locate index.html in both source and PyInstaller builds."""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidate = Path(bundled) / "shift_agent" / "setup" / "index.html"
        if candidate.is_file():
            return candidate
        candidate = Path(bundled) / "index.html"
        if candidate.is_file():
            return candidate
    return Path(__file__).with_name("index.html")


def open_setup_window() -> str | None:
    """Show the setup/picker window; block until a profile is chosen/saved or
    the wait times out. Returns the chosen profile's slug, or None.

    Serves a copy of index.html from a scratch directory rather than the
    package directory itself, matching how the dashboard serves a rendered
    *output* directory rather than its own source tree.
    """
    from ..main import _show  # lazy: main.py imports this module lazily too

    api = SetupAPI()
    with tempfile.TemporaryDirectory(prefix="shift-agent-setup-") as tmp:
        outdir = Path(tmp)
        (outdir / "index.html").write_text(
            template_path().read_text(encoding="utf-8"), encoding="utf-8"
        )
        server = DashboardServer(outdir, setup_api=api)
        url = server.start()
        try:
            _show(url)
            api.done_event.wait(timeout=_WAIT_TIMEOUT_S)
        finally:
            server.stop()
    return api.chosen
