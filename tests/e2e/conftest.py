"""Shared machinery for the end-to-end tier.

These tests drive a real Chromium against a real loopback server. They are
deselected by default (see the `e2e` marker in pyproject.toml) so `pytest -q`
stays browser-free and offline, which is the invariant CI depends on.

Nothing here reaches the internet. The "portal" is a local HTTP server built
from the same fixtures the parser tests use, plus the three pages those fixtures
never had: a login form, a challenge, and a session-expired page.
"""

from __future__ import annotations

import os
import shutil
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "flica"

# Playwright's own download is blocked in some sandboxes, so honour a
# pre-installed browser when one is present rather than failing to start.
_CANDIDATES = (
    os.environ.get("SHIFT_AGENT_CHROMIUM"),
    "/opt/pw-browsers/chromium",
)


def chromium_path() -> str | None:
    for candidate in _CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def launch_kwargs(headless: bool = True) -> dict:
    kwargs: dict = {"headless": headless}
    path = chromium_path()
    if path:
        kwargs["executable_path"] = path
    # Containers run as root with no user namespaces; the real deployment does
    # not need this and does not get it.
    kwargs["args"] = ["--no-sandbox", "--disable-dev-shm-usage"]
    return kwargs


# --------------------------------------------------------------------------
# The fake portal
# --------------------------------------------------------------------------

LOGIN_PAGE = """<!doctype html><html><head><title>FLICA Sign In</title></head>
<body><h1>Sign in</h1>
<form method="post" action="/login">
  <input name="user"><input name="pass" type="password">
  <input type="submit" value="Sign In">
</form></body></html>"""

# A real challenge marker, so `has_captcha` is exercised rather than mocked.
CHALLENGE_PAGE = """<!doctype html><html><head><title>Verify</title></head>
<body><h1>Confirm you are human</h1>
<div class="g-recaptcha" data-sitekey="fake"></div>
<script src="https://www.google.com/recaptcha/api.js" async defer></script>
</body></html>"""

EXPIRED_PAGE = """<!doctype html><html><head><title>Session expired</title></head>
<body><h1>Your session has expired</h1><p>Please sign in again.</p></body></html>"""

FRAMESET = """<!doctype html><html><head><title>FLICA</title></head>
<frameset rows="60,*">
  <frame name="nav" src="/nav.html">
  <frameset cols="50%,50%">
    <frame name="ot" src="/otopentimepot.cgi">
    <frame name="sched" src="/cmschedules.cgi">
  </frameset>
</frameset></html>"""

NAV = "<!doctype html><html><body>FLICA</body></html>"


class FakePortal:
    """A stand-in FLICA: frames, a challenge that can be cleared, live fixtures.

    State is mutated by the test between cycles, which is how the staleness and
    challenge-recovery paths get exercised without a real account.
    """

    def __init__(self) -> None:
        self.state = "ok"  # ok | challenge | expired | login
        self.open_time = (FIXTURES / "otopentimepot.html").read_text(encoding="utf-8")
        self.schedule = (FIXTURES / "cmschedules.html").read_text(encoding="utf-8")
        self.requests_page = (FIXTURES / "otrequest.html").read_text(encoding="utf-8")
        self.pair = (FIXTURES / "RBCPair.html").read_text(encoding="utf-8")
        self.hits: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- content ---------------------------------------------------------

    def body_for(self, path: str) -> tuple[int, str]:
        self.hits.append(path)
        if self.state == "challenge":
            return 200, CHALLENGE_PAGE
        if self.state == "expired":
            return 200, EXPIRED_PAGE
        if self.state == "login":
            return 200, LOGIN_PAGE

        if path in ("/", "/index.html"):
            return 200, FRAMESET
        if path.startswith("/nav"):
            return 200, NAV
        if path.startswith("/otopentimepot.cgi"):
            return 200, self.open_time
        if path.startswith("/cmschedules.cgi"):
            return 200, self.schedule
        if path.startswith("/otrequest.cgi"):
            return 200, self.requests_page
        if path.startswith("/RBCPair.cgi"):
            return 200, self.pair
        return 404, "<html><body>not found</body></html>"

    def drop_a_pairing(self) -> None:
        """Remove one row, so a reload observably differs from the first read."""
        marker = 'href="RBCPair.cgi?PID=M7C21'
        start = self.open_time.find(marker)
        if start == -1:
            raise AssertionError("fixture no longer contains the pairing this test removes")
        row_start = self.open_time.rfind("<tr", 0, start)
        row_end = self.open_time.find("</tr>", start) + len("</tr>")
        self.open_time = self.open_time[:row_start] + self.open_time[row_end:]

    def award_request(self, pairing: str) -> None:
        self.requests_page = self.requests_page.replace(
            "Pending", "Awarded", 1
        ) if pairing else self.requests_page

    def reject_request(self) -> None:
        self.requests_page = self.requests_page.replace("Pending", "Unable", 1)

    # -- lifecycle -------------------------------------------------------

    def start(self) -> str:
        handler = partial(_Handler, portal=self)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}/"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


class _Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, portal: FakePortal, **kwargs) -> None:
        self._portal = portal
        super().__init__(*args, **kwargs)

    def log_message(self, *args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        status, body = self._portal.body_for(self.path.split("?", 1)[0] or "/")
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:  # noqa: N802
        self._portal.state = "ok"
        self.send_response(302)
        self.send_header("Location", "/")
        self.end_headers()


@pytest.fixture
def portal():
    server = FakePortal()
    url = server.start()
    try:
        yield server, url
    finally:
        server.stop()


@pytest.fixture
def browser_profile(tmp_path):
    """A persistent Chromium profile directory, as the real adapter uses."""
    profile = tmp_path / "browser"
    profile.mkdir(parents=True, exist_ok=True)
    yield profile
    shutil.rmtree(profile, ignore_errors=True)
