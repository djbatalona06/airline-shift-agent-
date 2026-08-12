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

import logging
import secrets
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".ics": "text/calendar; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "shift-agent"

    def __init__(self, *args, directory: Path, token: str, **kwargs) -> None:
        self._directory = directory
        self._token = token
        super().__init__(*args, **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        log.debug("dashboard %s", fmt % args)

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

    def _deny(self) -> None:
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"not found")


class DashboardServer:
    def __init__(self, directory: str | Path, port: int = 0) -> None:
        self.directory = Path(directory)
        self.token = secrets.token_urlsafe(16)
        handler = partial(_Handler, directory=self.directory, token=self.token)
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
