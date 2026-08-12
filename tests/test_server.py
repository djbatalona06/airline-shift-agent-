"""Loopback dashboard server.

The binding and token tests are the point of this file: the page carries her
roster, so reachability is a security property, not a convenience.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request

import pytest

from shift_agent.dashboard.server import DashboardServer


@pytest.fixture
def served(tmp_path):
    (tmp_path / "index.html").write_text("<h1>dashboard</h1>", encoding="utf-8")
    (tmp_path / "shifts.ics").write_text("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", encoding="utf-8")
    (tmp_path / "shifts.md").write_text("| a |\n", encoding="utf-8")
    server = DashboardServer(tmp_path)
    server.start()
    yield server
    server.stop()


def get(url: str):
    return urllib.request.urlopen(url, timeout=5)


def test_serves_index_at_the_token_url(served):
    body = get(served.url).read().decode()
    assert "dashboard" in body


def test_serves_calendar_with_the_right_content_type(served):
    response = get(served.ics_url())
    assert response.headers["Content-Type"].startswith("text/calendar")
    assert b"VCALENDAR" in response.read()


def test_serves_markdown(served):
    assert get(f"{served.url}shifts.md").status == 200


# --- security ----------------------------------------------------------------

def test_wrong_token_is_refused(served):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(f"http://127.0.0.1:{served.port}/not-the-token/")
    assert exc.value.code == 404


def test_no_token_is_refused(served):
    with pytest.raises(urllib.error.HTTPError):
        get(f"http://127.0.0.1:{served.port}/")


def test_path_traversal_is_refused(served, tmp_path):
    (tmp_path.parent / "secret.txt").write_text("password", encoding="utf-8")
    with pytest.raises(urllib.error.HTTPError):
        get(f"{served.url}../secret.txt")


def test_binds_loopback_only(served):
    """Binding 0.0.0.0 would publish her roster to the whole local network."""
    host = served._server.server_address[0]
    assert host == "127.0.0.1"

    lan_ip = socket.gethostbyname(socket.gethostname())
    if lan_ip.startswith("127."):
        pytest.skip("no non-loopback address available on this host")

    with socket.socket() as probe:
        probe.settimeout(1.5)
        assert probe.connect_ex((lan_ip, served.port)) != 0


def test_each_server_gets_a_distinct_token(tmp_path):
    (tmp_path / "index.html").write_text("x", encoding="utf-8")
    a, b = DashboardServer(tmp_path), DashboardServer(tmp_path)
    try:
        assert a.token != b.token
    finally:
        a._server.server_close()
        b._server.server_close()
