"""The dashboard server's write path.

Most of these are refusal tests. A loopback server with a write route is
reachable by anything running on the machine and by any page the browser has
open, so what it *declines* is the interesting behaviour.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from shift_agent.dashboard.server import GUARD_HEADER, MAX_BODY_BYTES, DashboardServer


class FakeChat:
    def __init__(self) -> None:
        self.questions: list[str] = []
        self.applied: list[str] = []

    async def handle_chat(self, question, history):
        self.questions.append(question)
        return {"ok": True, "text": f"answer to {question}", "history_len": len(history)}

    async def handle_apply(self, change_id):
        self.applied.append(change_id)
        return {"ok": True, "summary": "applied"}


@pytest.fixture
def served(tmp_path):
    (tmp_path / "index.html").write_text("<html>dash</html>", encoding="utf-8")
    chat = FakeChat()
    server = DashboardServer(tmp_path, chat=chat)
    server.start()
    try:
        yield server, chat
    finally:
        server.stop()


def post(server, route, body, *, headers=None, host=None, raw=None):
    url = f"{server.url}{route}"
    data = raw if raw is not None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in (headers if headers is not None else {GUARD_HEADER: "1"}).items():
        request.add_header(key, value)
    if host:
        request.add_header("Host", host)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read())


# -- the happy path --------------------------------------------------------


def test_chat_route_reaches_the_backend(served):
    server, chat = served
    status, body = post(server, "api/chat", {"question": "why skip M8W77?"})
    assert status == 200
    assert body["text"] == "answer to why skip M8W77?"
    assert chat.questions == ["why skip M8W77?"]


def test_history_is_passed_through(served):
    server, _ = served
    _, body = post(
        server,
        "api/chat",
        {"question": "and then?", "history": [{"role": "user", "content": "hi"}]},
    )
    assert body["history_len"] == 1


def test_apply_route_reaches_the_backend(served):
    server, chat = served
    status, body = post(server, "api/config/apply", {"change_id": "abc123"})
    assert status == 200 and body["ok"] is True
    assert chat.applied == ["abc123"]


def test_get_still_serves_the_dashboard(served):
    server, _ = served
    with urllib.request.urlopen(server.url, timeout=5) as response:
        assert b"dash" in response.read()


# -- refusals --------------------------------------------------------------


def test_wrong_token_is_refused(served):
    server, chat = served
    url = f"http://127.0.0.1:{server.port}/wrong-token/api/chat"
    request = urllib.request.Request(url, data=b"{}", method="POST")
    request.add_header(GUARD_HEADER, "1")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=5)
    assert excinfo.value.code == 404
    assert chat.questions == []


def test_missing_guard_header_is_refused(served):
    """Without a custom header a browser can post cross-origin with no
    preflight. Requiring one is what forces the preflight we never answer."""
    server, chat = served
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(server, "api/chat", {"question": "hi"}, headers={})
    assert excinfo.value.code == 404
    assert chat.questions == []


def test_foreign_origin_is_refused(served):
    server, chat = served
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(
            server,
            "api/chat",
            {"question": "hi"},
            headers={GUARD_HEADER: "1", "Origin": "https://evil.example"},
        )
    assert excinfo.value.code == 404
    assert chat.questions == []


def test_rebound_host_header_is_refused(served):
    """DNS rebinding: an attacker's name resolves to 127.0.0.1 while the page
    keeps their origin. Pinning Host is what closes it."""
    server, chat = served
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(server, "api/chat", {"question": "hi"}, host="attacker.example")
    assert excinfo.value.code == 404
    assert chat.questions == []


def test_unknown_route_is_refused(served):
    server, _ = served
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(server, "api/claim", {"shift_id": "M4A76"})
    assert excinfo.value.code == 404


def test_oversized_body_is_refused_without_being_read(served):
    server, chat = served
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(server, "api/chat", None, raw=b"x" * (MAX_BODY_BYTES + 1))
    assert excinfo.value.code == 404
    assert chat.questions == []


def test_empty_body_is_refused(served):
    server, _ = served
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(server, "api/chat", None, raw=b"")
    assert excinfo.value.code == 404


def test_malformed_json_is_a_clean_error(served):
    server, _ = served
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(server, "api/chat", None, raw=b"{not json")
    assert excinfo.value.code == 400


def test_non_object_json_is_refused(served):
    server, _ = served
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(server, "api/chat", None, raw=b'"just a string"')
    assert excinfo.value.code == 400


def test_refusals_are_indistinguishable_from_a_missing_file(served):
    """The token is only secret while every wrong guess looks the same."""
    server, _ = served
    bodies = set()
    for url in (
        f"http://127.0.0.1:{server.port}/wrong-token/api/chat",
        f"{server.url}api/nope",
    ):
        request = urllib.request.Request(url, data=b"{}", method="POST")
        request.add_header(GUARD_HEADER, "1")
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            bodies.add((exc.code, exc.read()))
    assert len(bodies) == 1


# -- no backend attached ---------------------------------------------------


def test_post_does_not_exist_without_a_chat_backend(tmp_path):
    """The demo build and any install without a model get a read-only server,
    with no write path present to attack at all."""
    (tmp_path / "index.html").write_text("<html>x</html>", encoding="utf-8")
    server = DashboardServer(tmp_path)
    server.start()
    try:
        request = urllib.request.Request(f"{server.url}api/chat", data=b"{}", method="POST")
        request.add_header(GUARD_HEADER, "1")
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=5)
        assert excinfo.value.code == 404
    finally:
        server.stop()
