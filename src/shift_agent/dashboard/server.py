"""Loopback HTTP server for the dashboard.

Exists because `file://` pages cannot use the clipboard API — the
copy-as-markdown button silently fails — and because Outlook and Apple Calendar
can only subscribe to an `http://` feed.

Two security properties, both deliberate:

* **Binds 127.0.0.1 only, never 0.0.0.0.** Binding all interfaces would publish
  her roster to every device on whatever wifi she is on.
* **A random token in the path.** Loopback still allows any local process or
  other signed-in user to reach the port; the token means guessing the port is
  not enough.

Standard library only: fewer PyInstaller hidden imports and a smaller executable
than a web framework, for three static routes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".ics": "text/calendar; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}

CHAT_ROUTE = "api/chat"
SETUP_PROFILES_ROUTE = "api/setup/profiles"
SETUP_SAVE_ROUTE = "api/setup/save"
SETUP_CHOOSE_ROUTE = "api/setup/choose"
MAX_BODY_BYTES = 16 * 1024

# The page is a single self-contained file with inline CSS and JS and no remote
# assets, so everything can be locked to 'self' plus inline. Worth having now
# that the chat panel renders text written by a language model and relayed from
# Telegram: the template escapes it, and this is the second layer.
CSP = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)


class _Handler(BaseHTTPRequestHandler):
    server_version = "shift-agent"

    def __init__(
        self, *args, directory: Path, token: str, hub=None, loop=None, setup_api=None, **kwargs
    ) -> None:
        self._directory = directory
        self._token = token
        # Both None unless the agent is running with the chat surface enabled;
        # the API routes then 404 exactly like any other unknown path.
        self._hub = hub
        self._loop = loop
        # None unless this server is showing the first-run setup/picker window;
        # the setup routes then 404 exactly like any other unknown path.
        self._setup_api = setup_api
        super().__init__(*args, **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        log.debug("dashboard %s", fmt % args)

    def _route(self) -> str | None:
        """The path below the token, or None when the token does not match."""
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/", 1)
        if not parts or not secrets.compare_digest(parts[0], self._token):
            return None
        return parts[1] if len(parts) > 1 else ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        name = self._route()
        if name is None:
            # Same response for a wrong token and a missing file, so the token
            # cannot be probed by watching which URLs 404 differently.
            self._deny()
            return

        if name == CHAT_ROUTE:
            self._chat_history()
            return

        if name == SETUP_PROFILES_ROUTE and self._setup_api is not None:
            self._setup_profiles()
            return

        target = (self._directory / (name or "index.html")).resolve()
        try:
            target.relative_to(self._directory.resolve())
        except ValueError:
            # Path traversal attempt (../../etc). Refuse rather than serve.
            self._deny()
            return

        if not target.is_file():
            self._deny()
            return

        self._respond(
            target.read_bytes(),
            CONTENT_TYPES.get(target.suffix, "application/octet-stream"),
        )

    def _origin_ok(self) -> bool:
        """Guards a *write* route, where loopback plus a token is not enough.

        The token stops a local process that cannot see the URL. It does not
        stop a page in another tab that can: DNS rebinding makes an attacker's
        hostname resolve to 127.0.0.1, at which point their JavaScript is
        same-origin with this server and any URL the browser has ever held is
        reachable from it.

        Two cheap checks close that. Pinning `Host` to a literal loopback
        address means a rebound hostname never matches, since the browser sends
        the name it dialled. Requiring `X-Shift-Agent` forces a CORS preflight
        for anything cross-origin, and the preflight is not answered - a form
        POST or a no-cors fetch cannot set a custom header at all. `Origin` is
        checked when present rather than required, because a same-origin
        navigation legitimately omits it.
        """
        host = (self.headers.get("Host") or "").strip()
        if host not in (f"127.0.0.1:{self.server.server_address[1]}", "127.0.0.1"):
            return False
        if self.headers.get("X-Shift-Agent") != "1":
            return False
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{host}":
            return False
        return True

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        route = self._route()
        # Same 404 as a wrong token or an unknown route, so a rejected write
        # tells an attacker nothing about why. Checked once, ahead of the
        # per-route dispatch below, since every write route - chat and the
        # setup/save and setup/choose routes alike - needs it equally.
        if not self._origin_ok():
            self._deny()
            return
        if route == CHAT_ROUTE and self._hub is not None:
            self._chat_post()
            return
        if route == SETUP_SAVE_ROUTE and self._setup_api is not None:
            self._setup_save()
            return
        if route == SETUP_CHOOSE_ROUTE and self._setup_api is not None:
            self._setup_choose()
            return
        self._deny()

    def _read_json_body(self) -> dict | None:
        """Parse a size-capped JSON object body, or None if it's not usable."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _chat_post(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            self._deny()
            return
        try:
            text = str(payload["text"])
        except (KeyError, TypeError):
            self._deny()
            return
        reply = self._call_hub(self._hub.post(text, source="dashboard"))
        self._json({"reply": reply})

    def _setup_profiles(self) -> None:
        self._json({"profiles": self._setup_api.list_profiles()})

    def _setup_save(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            self._deny()
            return
        self._json(self._setup_api.save_profile(payload))

    def _setup_choose(self) -> None:
        payload = self._read_json_body() or {}
        profile_id = str(payload.get("profile_id", ""))
        self._json(self._setup_api.choose(profile_id))

    def _chat_history(self) -> None:
        if self._hub is None:
            self._deny()
            return
        raw = parse_qs(urlparse(self.path).query).get("after", ["0"])[0]
        try:
            after = int(raw)
        except ValueError:
            after = 0
        messages = self._call_hub(self._hub.history(after))
        self._json({"messages": messages if isinstance(messages, list) else []})

    def _call_hub(self, coro):
        """Run a hub coroutine on the agent's event loop from this worker thread.

        The server runs its handlers on its own threads, but the hub, the store
        and the Telegram client all belong to the poll loop's thread. Hopping
        back rather than starting a second loop is what keeps a chat message and
        a poll cycle from touching SQLite concurrently.
        """
        if self._loop is None:
            coro.close()
            return ""
        try:
            return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=120)
        except Exception as exc:
            log.warning("chat request failed: %s", exc)
            return "Something went wrong handling that. Try again."

    def _respond(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict) -> None:
        self._respond(json.dumps(payload).encode("utf-8"), CONTENT_TYPES[".json"])

    def _deny(self) -> None:
        self._respond(b"not found", "text/plain; charset=utf-8", status=404)


class DashboardServer:
    def __init__(
        self, directory: str | Path, port: int = 0, hub=None, loop=None, setup_api=None
    ) -> None:
        self.directory = Path(directory)
        self.token = secrets.token_urlsafe(16)
        self.hub = hub
        handler = partial(
            _Handler,
            directory=self.directory,
            token=self.token,
            hub=hub,
            loop=loop,
            setup_api=setup_api,
        )
        # 127.0.0.1, never 0.0.0.0 - see module docstring.
        self._server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}/"

    def ics_url(self) -> str:
        return f"{self.url}shifts.ics"

    def chat_url(self) -> str:
        return f"{self.url}{CHAT_ROUTE}"

    def start(self) -> str:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.info("dashboard served on 127.0.0.1:%s", self.port)
        return self.url

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> DashboardServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
