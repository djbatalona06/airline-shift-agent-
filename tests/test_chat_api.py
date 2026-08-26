"""The dashboard server's chat routes.

The token gate matters more here than on the static files: these routes write
to the conversation and can pause monitoring, so an unauthenticated caller
reaching them would be worse than one reading the roster.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request

import pytest

from shift_agent.chat.agent import ChatAgent
from shift_agent.chat.hub import ChatHub
from shift_agent.dashboard.server import DashboardServer
from shift_agent.store import Store
from conftest import make_chat_config


class FakeChatClient:
    async def reply(self, *, history, tools, run_tool):
        return "got it"


@pytest.fixture
def served(tmp_path):
    """A server with a hub, backed by a real event loop on its own thread.

    Mirrors production: the loop belongs to the agent, and the server's handler
    threads hop coroutines back onto it.
    """
    (tmp_path / "index.html").write_text("<h1>dashboard</h1>", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    config = make_chat_config()
    hub = ChatHub(config, store, ChatAgent(config, store, FakeChatClient()))

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    server = DashboardServer(tmp_path, hub=hub, loop=loop)
    server.start()
    try:
        yield server, store
    finally:
        server.stop()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()
        store.close()


def post(url: str, payload: dict, headers: dict | None = None):
    """Sends what the dashboard page sends. A None value removes a header."""
    sent = {"Content-Type": "application/json", "X-Shift-Agent": "1"}
    sent.update(headers or {})
    sent = {k: v for k, v in sent.items() if v is not None}
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=sent
    )
    return urllib.request.urlopen(request, timeout=10)


def get_json(url: str):
    return json.loads(urllib.request.urlopen(url, timeout=10).read().decode())


def test_posting_a_message_gets_a_reply(served):
    server, _ = served
    body = json.loads(post(server.chat_url(), {"text": "hello"}).read().decode())
    assert body["reply"] == "got it"


def test_the_message_lands_in_history(served):
    server, _ = served
    post(server.chat_url(), {"text": "hello"})

    messages = get_json(server.chat_url())["messages"]

    assert [m["text"] for m in messages] == ["hello", "got it"]
    assert messages[0]["source"] == "dashboard"


def test_after_cursor_returns_only_newer_messages(served):
    server, _ = served
    post(server.chat_url(), {"text": "one"})
    first_batch = get_json(server.chat_url())["messages"]
    post(server.chat_url(), {"text": "two"})

    newer = get_json(f"{server.chat_url()}?after={first_batch[-1]['id']}")["messages"]

    assert [m["text"] for m in newer] == ["two", "got it"]


# --- security ----------------------------------------------------------------


def test_chat_post_without_the_token_is_refused(served):
    server, store = served
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(f"http://127.0.0.1:{server.port}/not-the-token/api/chat", {"text": "hi"})
    assert exc.value.code == 404
    assert store.messages_after("tester") == []


def test_chat_history_without_the_token_is_refused(served):
    server, _ = served
    with pytest.raises(urllib.error.HTTPError):
        get_json(f"http://127.0.0.1:{server.port}/not-the-token/api/chat")


def test_an_oversized_body_is_refused(served):
    server, store = served
    with pytest.raises(urllib.error.HTTPError):
        post(server.chat_url(), {"text": "x" * 100_000})
    assert store.messages_after("tester") == []


def test_a_malformed_body_is_refused(served):
    server, _ = served
    request = urllib.request.Request(
        server.chat_url(),
        data=b"not json",
        headers={"Content-Type": "application/json", "X-Shift-Agent": "1"},
    )
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(request, timeout=10)


# --- cross-origin writes -----------------------------------------------------
#
# The token alone protects a route from a local process that cannot see the
# URL. It does not protect a *write* route from a browser that can, which is
# what these three cover.


def test_a_write_without_the_custom_header_is_refused(served):
    """The header is what forces a CORS preflight. A cross-origin form POST or
    a no-cors fetch cannot set it, so requiring it is what makes the preflight
    unavoidable - and the preflight is never answered."""
    server, store = served
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(server.chat_url(), {"text": "hi"}, headers={"X-Shift-Agent": None})
    assert exc.value.code == 404
    assert store.messages_after("tester") == []


def test_a_write_with_a_rebound_host_is_refused(served):
    """DNS rebinding: the attacker's hostname resolves to 127.0.0.1, so the
    connection is genuinely to us, but the browser still sends the name it
    dialled. Pinning Host to the literal address is what catches it."""
    server, store = served
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(server.chat_url(), {"text": "hi"}, headers={"Host": "attacker.example"})
    assert exc.value.code == 404
    assert store.messages_after("tester") == []


def test_a_write_from_another_origin_is_refused(served):
    server, store = served
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(server.chat_url(), {"text": "hi"}, headers={"Origin": "https://evil.example"})
    assert exc.value.code == 404
    assert store.messages_after("tester") == []


def test_a_write_from_our_own_origin_is_allowed(served):
    """The Origin check must not break the page that legitimately sends one."""
    server, store = served
    origin = f"http://127.0.0.1:{server.port}"
    response = post(server.chat_url(), {"text": "hi"}, headers={"Origin": origin})
    assert response.status == 200
    assert [m["text"] for m in store.messages_after("tester")][:1] == ["hi"]


def test_security_headers_are_sent(served):
    server, _ = served
    response = urllib.request.urlopen(server.url, timeout=10)
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


# --- no hub ------------------------------------------------------------------


def test_routes_are_absent_when_no_hub_is_wired(tmp_path):
    """A statically built dashboard has no agent behind it; the endpoints must
    not exist rather than fail oddly."""
    (tmp_path / "index.html").write_text("x", encoding="utf-8")
    server = DashboardServer(tmp_path)
    server.start()
    try:
        with pytest.raises(urllib.error.HTTPError):
            get_json(f"{server.url}api/chat")
        with pytest.raises(urllib.error.HTTPError):
            post(f"{server.url}api/chat", {"text": "hi"})
    finally:
        server.stop()
