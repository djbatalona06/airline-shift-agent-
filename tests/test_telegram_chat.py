"""Telegram's new surface: /ask, health alerts, and send pacing.

Same `FakeBotAPI` fake-transport idiom as test_telegram.py, extended so a test
can force a 429 and inspect what the notifier does about it.
"""

from __future__ import annotations

import json

import httpx
import pytest

from shift_agent.chat.agent import ChatReply
from shift_agent.notify.telegram import MAX_RETRY_AFTER, TelegramNotifier, _retry_after
from shift_agent.store import Store

CHAT = 4242


class FakeBotAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        # Queue of statuses to return for sendMessage, consumed one per call.
        self.send_statuses: list[int] = []
        self.retry_after: float | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))

        if method == "sendMessage" and self.send_statuses:
            status = self.send_statuses.pop(0)
            if status != 200:
                payload: dict = {"ok": False, "description": "Too Many Requests"}
                if self.retry_after is not None:
                    payload["parameters"] = {"retry_after": self.retry_after}
                return httpx.Response(status, json=payload)
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.calls)}})

    def sent(self) -> list[dict]:
        return [b for m, b in self.calls if m == "sendMessage"]

    def last_text(self) -> str:
        return self.sent()[-1].get("text", "")


class FakeClock:
    """Monotonic time the test drives, so pacing is asserted not waited out."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeAgent:
    def __init__(self, reply=None, error: Exception | None = None) -> None:
        self.reply = reply or ChatReply(text="Because it starts at 23:40.")
        self.error = error
        self.asked: list[str] = []

    async def ask(self, question, history=None):
        self.asked.append(question)
        if self.error:
            raise self.error
        return self.reply


def build(tmp_path, *, chat_agent=None, interval=1.0):
    api = FakeBotAPI()
    clock = FakeClock()
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.now += seconds

    notifier = TelegramNotifier(
        "token123",
        Store(tmp_path / "state.db"),
        http=httpx.AsyncClient(transport=api.transport()),
        chat_id=CHAT,
        chat_agent=chat_agent,
        min_send_interval=interval,
        clock=clock,
        sleep=fake_sleep,
    )
    return notifier, api, slept


def message(text: str, chat: int = CHAT, update_id: int = 1) -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": chat}, "text": text}}


# --- /ask --------------------------------------------------------------------


async def test_ask_routes_to_the_agent(tmp_path):
    agent = FakeAgent()
    notifier, api, _ = build(tmp_path, chat_agent=agent, interval=0)
    await notifier.consume(message("/ask why did you skip M8W77"))
    assert agent.asked == ["why did you skip M8W77"]
    assert "23:40" in api.last_text()


async def test_ask_without_an_agent_explains_how_to_set_one_up(tmp_path):
    notifier, api, _ = build(tmp_path, interval=0)
    await notifier.consume(message("/ask anything"))
    assert "set-llm-key" in api.last_text()


async def test_ask_with_no_question_prompts_for_one(tmp_path):
    agent = FakeAgent()
    notifier, api, _ = build(tmp_path, chat_agent=agent, interval=0)
    await notifier.consume(message("/ask"))
    assert agent.asked == []
    assert "for example" in api.last_text().lower()


async def test_ask_survives_an_agent_failure(tmp_path):
    agent = FakeAgent(error=RuntimeError("model down"))
    notifier, api, _ = build(tmp_path, chat_agent=agent, interval=0)
    await notifier.consume(message("/ask anything"))
    assert "could not answer" in api.last_text().lower()
    assert "model down" not in api.last_text()


async def test_ask_points_config_changes_at_the_dashboard(tmp_path):
    """A diff cannot be reviewed in a chat message, so Telegram never offers
    an approval - it says where the diff can actually be read."""
    agent = FakeAgent(
        ChatReply(text="You could raise rest.", proposal={"change_id": "x", "summary": "rest 12h"})
    )
    notifier, api, _ = build(tmp_path, chat_agent=agent, interval=0)
    await notifier.consume(message("/ask more rest please"))
    assert "dashboard" in api.last_text().lower()


async def test_ask_is_listed_in_help(tmp_path):
    notifier, api, _ = build(tmp_path, interval=0)
    await notifier.consume(message("/wat"))
    assert "/ask" in api.last_text()


async def test_ask_from_an_unlinked_chat_is_ignored(tmp_path):
    agent = FakeAgent()
    notifier, api, _ = build(tmp_path, chat_agent=agent, interval=0)
    notifier.store.set("telegram_linked_chat_id", CHAT)
    await notifier.consume(message("/ask secret question", chat=9999))
    assert agent.asked == []


# --- health alerts -----------------------------------------------------------


async def test_system_alerts_are_marked_differently_from_shift_alerts(tmp_path):
    notifier, api, _ = build(tmp_path, interval=0)
    await notifier.system("Poller has stalled.")
    await notifier.alert("A shift you might want opened.")
    texts = [s["text"] for s in api.sent()]
    assert texts[0].startswith("🩺")
    assert texts[1].startswith("⚠️")


# --- pacing and 429 ----------------------------------------------------------


async def test_sends_are_paced_within_one_chat(tmp_path):
    """Bursty chat replies must not starve a captcha alert of its send budget."""
    notifier, api, slept = build(tmp_path, interval=1.0)
    await notifier.info("first")
    assert slept == []  # nothing to wait for on the first send
    await notifier.info("second")
    assert slept == [1.0]
    assert len(api.sent()) == 2


async def test_pacing_does_not_delay_an_isolated_send(tmp_path):
    notifier, _, slept = build(tmp_path, interval=1.0)
    await notifier.info("first")
    notifier._last_send -= 60  # an hour of quiet, as in normal operation
    await notifier.info("much later")
    assert slept == []


async def test_rate_limited_send_is_retried_once(tmp_path):
    notifier, api, slept = build(tmp_path, interval=0)
    api.send_statuses = [429, 200]
    api.retry_after = 7
    await notifier.info("important")
    assert len(api.sent()) == 2  # the 429, then the successful retry
    assert 7.0 in slept  # waited exactly as long as Telegram asked


async def test_rate_limit_retry_is_not_infinite(tmp_path):
    notifier, api, _ = build(tmp_path, interval=0)
    api.send_statuses = [429, 429]
    api.retry_after = 0
    await notifier.info("important")
    assert len(api.sent()) == 2  # gives up rather than looping


async def test_send_failure_still_never_raises(tmp_path):
    notifier, api, _ = build(tmp_path, interval=0)
    api.send_statuses = [500]
    await notifier.info("whatever")  # must not raise


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"parameters": {"retry_after": 5}}, 5.0),
        ({"parameters": {"retry_after": 9999}}, MAX_RETRY_AFTER),
        ({}, 1.0),
    ],
)
def test_retry_after_is_read_and_bounded(body, expected):
    response = httpx.Response(429, json=body)
    assert _retry_after(response) == expected


def test_retry_after_survives_a_junk_body():
    response = httpx.Response(429, content=b"not json")
    assert _retry_after(response) == 1.0
