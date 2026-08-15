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

The chat assistant added a write path, which needs three more checks that a
read-only static server never did — see `_authorised`. When no chat backend is
attached the POST routes do not exist at all, which is the case for the demo
build and for anyone who has not set up a model.

Standard library only: fewer PyInstaller hidden imports and a smaller executable
than a web framework, for three static routes.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".ics": "text/calendar; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}

# A question plus a short transcript. Large enough for a real conversation,
# small enough that a runaway client cannot make us buffer megabytes.
MAX_BODY_BYTES = 64 * 1024

# Browsers will not send this cross-origin without a successful preflight, and
# we answer no preflight. It is what stops a page in another tab from posting
# here even if it somehow learned the token.
GUARD_HEADER = "x-shift-agent"


class ChatBackend(Protocol):
    """What the server needs from the chat layer.

    A protocol rather than an import so this module keeps knowing nothing about
    providers, models or keys.
    """

    async def handle_chat(self, question: str, history: list[dict[str, Any]]) -> dict[str, Any]: ...

    async def handle_apply(self, change_id: str) -> dict[str, Any]: ...


class _Handler(BaseHTTPRequestHandler):
    server_version = "shift-agent"

    def __init__(self, *args, directory: Path, token: str, chat=None, **kwargs) -> None:
        self._directory = directory
        self._token = token
        self._chat = chat
        super().__init__(*args, **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        log.debug("dashboard %s", fmt % args)

    # -- reads -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0].strip("/")
        parts = path.split("/", 1)

        if not parts or not secrets.compare_digest(parts[0], self._token):
            # Same response for a wrong token and a missing file, so the token
            # cannot be probed by watching which URLs 404 differently.
            self._deny()
            return

        name = parts[1] if len(parts) > 1 and parts[1] else "index.html"
        target = (self._directory / name).resolve()
        try:
            target.relative_to(self._directory.resolve())
        except ValueError:
            # Path traversal attempt (../../etc). Refuse rather than serve.
            self._deny()
            return

        if not target.is_file():
            self._deny()
            return

        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- writes ------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        """Chat and config-apply. Everything else, and every failure, is a 404.

        Deliberately indistinguishable from the read path's refusal: the token
        is only secret while wrong guesses all look the same, and a chattier
        error here would undo that for the whole server.
        """
        if self._chat is None:
            self._deny()
            return

        path = self.path.split("?", 1)[0].strip("/")
        parts = path.split("/", 1)
        if len(parts) != 2 or not secrets.compare_digest(parts[0], self._token):
            self._deny()
            return
        if not self._authorised():
            self._deny()
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._deny()
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._deny()
            return

        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "malformed request"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "malformed request"})
            return

        route = parts[1]
        if route == "api/chat":
            self._run(
                self._chat.handle_chat(
                    str(payload.get("question") or ""),
                    payload.get("history") if isinstance(payload.get("history"), list) else [],
                )
            )
        elif route == "api/config/apply":
            self._run(self._chat.handle_apply(str(payload.get("change_id") or "")))
        else:
            self._deny()

    def _run(self, coro) -> None:
        """Drive one coroutine to completion on this request's thread.

        ThreadingHTTPServer hands each request its own thread, and the chat
        backend is async because the providers are. A fresh loop per request is
        wasteful in principle and irrelevant in practice: this server handles a
        handful of requests from one person.
        """
        import asyncio

        try:
            result = asyncio.run(coro)
        except Exception as exc:  # pragma: no cover - defensive
            from ..logging_safe import scrub

            log.warning("chat request failed: %s", scrub(exc))
            self._json(500, {"error": "something went wrong handling that"})
            return
        self._json(200, result)

    def _authorised(self) -> bool:
        """Host, Origin and a custom header — the three loopback-specific risks.

        The token alone is not enough for a write path. A page on another origin
        can be pointed at 127.0.0.1, and DNS rebinding lets an attacker's domain
        resolve here while keeping their origin, so both ends need pinning.
        """
        expected_host = f"127.0.0.1:{self.server.server_address[1]}"
        if (self.headers.get("Host") or "") != expected_host:
            return False

        origin = self.headers.get("Origin")
        if origin and origin != f"http://{expected_host}":
            return False

        return bool(self.headers.get(GUARD_HEADER))

    # -- responses ---------------------------------------------------------

    def _json(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", CONTENT_TYPES[".json"])
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _deny(self) -> None:
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"not found")


class DashboardServer:
    def __init__(self, directory: str | Path, port: int = 0, chat: ChatBackend | None = None) -> None:
        self.directory = Path(directory)
        self.token = secrets.token_urlsafe(16)
        self.chat = chat
        handler = partial(_Handler, directory=self.directory, token=self.token, chat=chat)
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
