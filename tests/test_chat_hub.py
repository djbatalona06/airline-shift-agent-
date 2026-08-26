"""ChatHub: one conversation, two front doors.

Against a fake ChatClient - no network, no key, no `anthropic` import, matching
ci.yml's rule for the whole suite.
"""

from __future__ import annotations

import asyncio

import pytest

from shift_agent.chat.agent import ChatAgent
from shift_agent.chat.hub import ChatHub
from shift_agent.store import PAUSE_REASON_KEY, PAUSED_KEY


class FakeChatClient:
    """Returns a canned reply; records the history it was handed."""

    def __init__(self, text="sure thing", tool_calls=()):
        self.text = text
        self.tool_calls = list(tool_calls)
        self.seen_history = None
        self.seen_tools = None

    async def reply(self, *, history, tools, run_tool):
        self.seen_history = list(history)
        self.seen_tools = list(tools)
        for name, arguments in self.tool_calls:
            run_tool(name, arguments)
        return self.text


class ExplodingChatClient:
    async def reply(self, *, history, tools, run_tool):
        raise RuntimeError("api key sk-not-a-real-key rejected")


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    async def info(self, text):
        self.sent.append(text)


def build(store, config, client=None, notifier=None):
    agent = ChatAgent(config, store, client or FakeChatClient())
    return ChatHub(config, store, agent, notifier=notifier)


async def test_records_both_sides_of_the_turn(chat_store_and_config):
    store, config = chat_store_and_config
    hub = build(store, config)

    reply = await hub.post("what have you seen?", source="dashboard")

    assert reply == "sure thing"
    rows = store.messages_after("tester")
    assert [(r["role"], r["text"]) for r in rows] == [
        ("user", "what have you seen?"),
        ("agent", "sure thing"),
    ]


async def test_dashboard_messages_are_mirrored_to_telegram(chat_store_and_config):
    """The whole point of the hub: both surfaces show one conversation."""
    store, config = chat_store_and_config
    notifier = RecordingNotifier()
    hub = build(store, config, notifier=notifier)

    await hub.post("hello", source="dashboard")

    assert any("hello" in text for text in notifier.sent)
    assert any("sure thing" in text for text in notifier.sent)


async def test_telegram_messages_are_not_echoed_back_to_telegram(chat_store_and_config):
    """Telegram already shows what she typed and what the bot replied; mirroring
    would duplicate every line in her chat."""
    store, config = chat_store_and_config
    notifier = RecordingNotifier()
    hub = build(store, config, notifier=notifier)

    await hub.post("hello", source="telegram")

    assert notifier.sent == []
    assert len(store.messages_after("tester")) == 2


async def test_a_model_failure_becomes_a_visible_turn_not_an_exception(chat_store_and_config):
    """A poll loop or an HTTP handler must not die because the model was down."""
    store, config = chat_store_and_config
    hub = build(store, config, client=ExplodingChatClient())

    reply = await hub.post("hi", source="dashboard")

    assert "couldn't reach" in reply
    rows = store.messages_after("tester")
    assert rows[-1]["role"] == "agent"
    assert rows[-1]["source"] == "system"


async def test_failure_text_never_leaks_the_key(chat_store_and_config):
    """The raw exception carries an api key; the stored turn must not."""
    store, config = chat_store_and_config
    hub = build(store, config, client=ExplodingChatClient())

    reply = await hub.post("hi", source="dashboard")

    assert "sk-not-a-real-key" not in reply
    assert all("sk-not-a-real-key" not in r["text"] for r in store.messages_after("tester"))


async def test_empty_messages_are_ignored(chat_store_and_config):
    store, config = chat_store_and_config
    hub = build(store, config)

    assert await hub.post("   ", source="dashboard") == ""
    assert store.messages_after("tester") == []


async def test_long_messages_are_truncated(chat_store_and_config):
    store, config = chat_store_and_config
    hub = build(store, config)

    await hub.post("x" * 50_000, source="dashboard")

    assert len(store.messages_after("tester")[0]["text"]) == 4000


async def test_the_model_sees_prior_turns(chat_store_and_config):
    store, config = chat_store_and_config
    client = FakeChatClient()
    hub = build(store, config, client=client)

    await hub.post("first", source="dashboard")
    await hub.post("second", source="dashboard")

    assert [t.text for t in client.seen_history] == ["first", "sure thing", "second"]


async def test_concurrent_posts_do_not_interleave(chat_store_and_config):
    """Two surfaces posting at once must not produce a reply to a conversation
    neither person had."""
    store, config = chat_store_and_config
    hub = build(store, config)

    await asyncio.gather(
        hub.post("a", source="dashboard"),
        hub.post("b", source="telegram"),
    )

    roles = [r["role"] for r in store.messages_after("tester")]
    assert roles == ["user", "agent", "user", "agent"]


async def test_pause_tool_actually_pauses(chat_store_and_config):
    store, config = chat_store_and_config
    client = FakeChatClient(text="paused", tool_calls=[("pause", {"reason": "on holiday"})])
    hub = build(store, config, client=client)

    await hub.post("stop for now", source="telegram")

    assert store.get(PAUSED_KEY) is True
    assert store.get(PAUSE_REASON_KEY) == "on holiday"


async def test_resume_tool_clears_the_pause(chat_store_and_config):
    store, config = chat_store_and_config
    store.set(PAUSED_KEY, True)
    client = FakeChatClient(text="watching", tool_calls=[("resume", {})])
    hub = build(store, config, client=client)

    await hub.post("go again", source="telegram")

    assert store.get(PAUSED_KEY) is False


async def test_read_tools_return_data_without_secrets(chat_store_and_config):
    store, config = chat_store_and_config
    agent = ChatAgent(config, store, FakeChatClient())

    status = agent.run_tool("get_status", {})
    settings = agent.run_tool("get_settings", {})

    assert status["paused"] is False
    assert settings["profile"] == "tester"
    blob = f"{status}{settings}"
    assert "password" not in blob.lower() and "token" not in blob.lower()


async def test_note_records_an_unprompted_line(chat_store_and_config):
    store, config = chat_store_and_config
    hub = build(store, config)

    await hub.note("Claimed trip ABC12.")

    row = store.messages_after("tester")[0]
    assert row["role"] == "agent" and row["source"] == "system"


async def test_history_serialises_for_the_api(chat_store_and_config):
    store, config = chat_store_and_config
    hub = build(store, config)
    await hub.post("hi", source="dashboard")

    history = await hub.history()

    assert set(history[0]) == {"id", "role", "source", "text", "created_at"}


@pytest.mark.parametrize("source", ["dashboard", "telegram"])
async def test_either_front_door_reaches_the_same_thread(chat_store_and_config, source):
    store, config = chat_store_and_config
    hub = build(store, config)

    await hub.post("hello", source=source)

    assert len(await hub.history()) == 2
