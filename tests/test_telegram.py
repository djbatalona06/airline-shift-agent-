"""Telegram notifier tests against a mocked Bot API. No network, no token.

These cannot catch a wrong Bot API field name or live-only behaviour — that
needs a real BotFather token and is explicitly deferred. What they do cover is
every branch where being wrong is a security or consent bug.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from shift_agent.models import ClaimOutcome, ClaimResult, Shift
from shift_agent.notify.telegram import TelegramNotifier
from shift_agent.store import (
    PAUSED_KEY,
    TELEGRAM_CHAT_KEY,
    TELEGRAM_OFFSET_KEY,
    Store,
)

CHAT = 4242
STRANGER = 9999


class FakeBotAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.pending_updates: list[dict] = []
        self.fail: set[str] = set()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))

        if method in self.fail:
            return httpx.Response(500, json={"ok": False, "description": "simulated failure"})
        if method == "getUpdates":
            out, self.pending_updates = self.pending_updates, []
            return httpx.Response(200, json={"ok": True, "result": out})
        if method == "sendMessage":
            return httpx.Response(200, json={"ok": True, "result": {
                "message_id": len(self.calls),
                "chat": {"id": body.get("chat_id")},
                "text": body.get("text", ""),
            }})
        return httpx.Response(200, json={"ok": True, "result": True})

    def sent(self) -> list[dict]:
        return [b for m, b in self.calls if m == "sendMessage"]

    def last_text(self) -> str:
        return self.sent()[-1].get("text", "")


def build(tmp_path, *, link_code=None, chat_id=CHAT):
    api = FakeBotAPI()
    http = httpx.AsyncClient(transport=api.transport())
    store = Store(tmp_path / "state.db")
    notifier = TelegramNotifier(
        "token123", store, http=http, link_code=link_code, chat_id=chat_id
    )
    return notifier, api, store


def future_shift(sid: str = "S1") -> Shift:
    start = datetime.now(UTC) + timedelta(days=2)
    return Shift(id=sid, start=start, end=start + timedelta(hours=6), title="Trip")


def nonce_from(api: FakeBotAPI) -> str:
    markup = api.sent()[-1]["reply_markup"]
    return markup["inline_keyboard"][0][0]["callback_data"].split(":", 1)[1]


def callback(nonce: str, kind: str = "c", chat: int = CHAT, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cbq1",
            "data": f"{kind}:{nonce}",
            "message": {"message_id": 1, "chat": {"id": chat}, "text": "Shift available"},
        },
    }


def message(text: str, chat: int = CHAT, update_id: int = 1) -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": chat}, "text": text}}


async def resolve_offer(notifier, api, kind: str, timeout_minutes=5, chat=CHAT):
    """Run offer() and deliver a callback answer to it."""
    task = asyncio.create_task(notifier.offer(future_shift(), timeout_minutes))
    for _ in range(200):                    # wait for sendMessage to land
        await asyncio.sleep(0)
        if any(m == "sendMessage" for m, _ in api.calls):
            break
    await notifier.consume(callback(nonce_from(api), kind, chat=chat))
    return await task


# --- consent -----------------------------------------------------------------

async def test_confirm_returns_true(tmp_path):
    notifier, api, _ = build(tmp_path)
    assert await resolve_offer(notifier, api, "c") is True


async def test_skip_returns_false(tmp_path):
    notifier, api, _ = build(tmp_path)
    assert await resolve_offer(notifier, api, "s") is False


async def test_timeout_returns_false(tmp_path):
    """The path that runs while she is asleep. Silence is never consent."""
    notifier, api, _ = build(tmp_path)
    assert await notifier.offer(future_shift(), timeout_minutes=0.002) is False
    assert "No answer" in api.last_text()


async def test_offer_fails_closed_when_message_cannot_be_sent(tmp_path):
    notifier, api, _ = build(tmp_path)
    api.fail.add("sendMessage")
    assert await notifier.offer(future_shift(), timeout_minutes=5) is False


async def test_offer_fails_closed_when_no_chat_linked(tmp_path):
    notifier, api, store = build(tmp_path, chat_id=None)
    assert store.get(TELEGRAM_CHAT_KEY) is None
    assert await notifier.offer(future_shift(), timeout_minutes=5) is False


async def test_stranger_cannot_confirm_an_offer(tmp_path):
    notifier, api, _ = build(tmp_path)
    task = asyncio.create_task(notifier.offer(future_shift(), 0.004))
    for _ in range(200):
        await asyncio.sleep(0)
        if any(m == "sendMessage" for m, _ in api.calls):
            break
    await notifier.consume(callback(nonce_from(api), "c", chat=STRANGER))
    assert await task is False              # falls through to timeout, not confirmed


# --- replay / staleness ------------------------------------------------------

async def test_unknown_nonce_is_reported_expired(tmp_path):
    notifier, api, _ = build(tmp_path)
    await notifier.consume(callback("no-such-nonce", "c"))
    answers = [b for m, b in api.calls if m == "answerCallbackQuery"]
    assert answers and "expired" in answers[-1]["text"].lower()


async def test_callback_replayed_after_restart_does_nothing(tmp_path):
    """Pending offers are in-memory by design, so a restart cannot honour a
    Confirm the user tapped before the process died."""
    notifier, api, store = build(tmp_path)
    task = asyncio.create_task(notifier.offer(future_shift(), 0.004))
    for _ in range(200):
        await asyncio.sleep(0)
        if any(m == "sendMessage" for m, _ in api.calls):
            break
    nonce = nonce_from(api)
    await task  # times out

    restarted = TelegramNotifier("token123", store, http=httpx.AsyncClient(transport=api.transport()))
    await restarted.consume(callback(nonce, "c"))
    answers = [b for m, b in api.calls if m == "answerCallbackQuery"]
    assert "expired" in answers[-1]["text"].lower()


async def test_offset_advances_before_handling(tmp_path):
    notifier, _, store = build(tmp_path)
    await notifier.consume(message("/status", update_id=77))
    assert store.get(TELEGRAM_OFFSET_KEY) == 78


async def test_offset_advances_even_if_handler_raises(tmp_path):
    notifier, api, store = build(tmp_path)
    api.fail.add("sendMessage")
    await notifier.consume(message("/status", update_id=100))
    assert store.get(TELEGRAM_OFFSET_KEY) == 101


# --- linking and authorization ----------------------------------------------

async def test_link_code_binds_chat(tmp_path):
    notifier, api, store = build(tmp_path, link_code="secret42", chat_id=None)
    await notifier.consume(message("/start secret42", chat=CHAT))
    assert store.get(TELEGRAM_CHAT_KEY) == CHAT
    assert "Linked" in api.last_text()


async def test_wrong_link_code_rejected(tmp_path):
    notifier, api, store = build(tmp_path, link_code="secret42", chat_id=None)
    await notifier.consume(message("/start wrong", chat=STRANGER))
    assert store.get(TELEGRAM_CHAT_KEY) is None
    assert "Invalid" in api.last_text()


async def test_link_code_is_single_use(tmp_path):
    notifier, api, store = build(tmp_path, link_code="secret42", chat_id=None)
    await notifier.consume(message("/start secret42", chat=CHAT))
    await notifier.consume(message("/start secret42", chat=STRANGER, update_id=2))
    assert store.get(TELEGRAM_CHAT_KEY) == CHAT     # still the original chat


async def test_command_from_unlinked_chat_is_ignored(tmp_path):
    notifier, api, store = build(tmp_path)
    before = len(api.sent())
    await notifier.consume(message("/pause", chat=STRANGER))
    assert len(api.sent()) == before                 # silent, no reply at all
    assert not store.get(PAUSED_KEY, False)


# --- commands ----------------------------------------------------------------

async def test_pause_and_resume_update_store(tmp_path):
    notifier, _, store = build(tmp_path)
    await notifier.consume(message("/pause"))
    assert store.get(PAUSED_KEY) is True
    await notifier.consume(message("/resume", update_id=2))
    assert store.get(PAUSED_KEY) is False


async def test_status_reports_paused_state(tmp_path):
    notifier, api, store = build(tmp_path)
    store.set(PAUSED_KEY, True)
    store.set("pause_reason", "captcha")
    await notifier.consume(message("/status"))
    text = api.last_text()
    assert "Paused" in text and "captcha" in text


async def test_unknown_command_shows_help(tmp_path):
    notifier, api, _ = build(tmp_path)
    await notifier.consume(message("/wat"))
    assert "/status" in api.last_text()


# --- resilience --------------------------------------------------------------

async def test_send_failure_does_not_raise(tmp_path):
    notifier, api, _ = build(tmp_path)
    api.fail.add("sendMessage")
    await notifier.info("hello")            # must not raise into the poll loop
    await notifier.alert("uh oh")


async def test_needs_human_includes_challenge_url(tmp_path):
    notifier, api, _ = build(tmp_path)
    await notifier.needs_human("Captcha shown", "https://portal.example/challenge")
    text = api.last_text()
    assert "https://portal.example/challenge" in text
    assert "/resume" in text


async def test_claim_outcome_marks_dry_run(tmp_path):
    notifier, api, _ = build(tmp_path)
    await notifier.claim_outcome(
        future_shift(), ClaimResult(ClaimOutcome.CLAIMED, "ok"), dry_run=True
    )
    assert "DRY RUN" in api.last_text()


async def test_claim_outcome_reports_lost_race(tmp_path):
    notifier, api, _ = build(tmp_path)
    await notifier.claim_outcome(
        future_shift(), ClaimResult(ClaimOutcome.LOST_RACE, "someone else got it"), dry_run=False
    )
    text = api.last_text()
    assert "Lost Race" in text and "DRY RUN" not in text


# --- link-code robustness ----------------------------------------------------

async def test_non_ascii_link_attempt_gets_a_reply(tmp_path):
    """A smart quote or emoji must produce "Invalid link code", not silence.

    compare_digest's str form raises TypeError on non-ASCII, which `consume`
    would swallow - leaving someone whose keyboard autocorrected the code with
    no feedback at all and no way to tell why linking failed.
    """
    notifier, api, store = build(tmp_path, link_code="secret42", chat_id=None)

    await notifier.consume(message("/start secret\u201942", chat=STRANGER))

    assert "Invalid link code" in api.last_text()
    assert store.get(TELEGRAM_CHAT_KEY) is None


async def test_used_link_code_is_expired_from_the_keychain(tmp_path, monkeypatch):
    """Clearing it in memory alone is not single-use: main._build_notifier reads
    the code back from the keychain on every start."""
    deleted: list[tuple[str, str]] = []
    import shift_agent.secrets as keychain

    monkeypatch.setattr(keychain, "delete", lambda user, name: deleted.append((user, name)))

    api = FakeBotAPI()
    http = httpx.AsyncClient(transport=api.transport())
    store = Store(tmp_path / "state.db")
    notifier = TelegramNotifier(
        "token123", store, http=http, link_code="secret42", chat_id=None, user="tester"
    )

    await notifier.consume(message("/start secret42", chat=CHAT))

    assert store.get(TELEGRAM_CHAT_KEY) == CHAT
    assert deleted == [("tester", "telegram_link_code")]


async def test_a_failed_link_does_not_expire_the_code(tmp_path, monkeypatch):
    deleted: list[tuple[str, str]] = []
    import shift_agent.secrets as keychain

    monkeypatch.setattr(keychain, "delete", lambda user, name: deleted.append((user, name)))

    api = FakeBotAPI()
    http = httpx.AsyncClient(transport=api.transport())
    store = Store(tmp_path / "state.db")
    notifier = TelegramNotifier(
        "token123", store, http=http, link_code="secret42", chat_id=None, user="tester"
    )

    await notifier.consume(message("/start wrong", chat=CHAT))

    assert deleted == []


# --- chat routing ------------------------------------------------------------

class RecordingHub:
    def __init__(self, reply="here you go"):
        self.reply = reply
        self.posted: list[tuple[str, str]] = []

    async def post(self, text, *, source):
        self.posted.append((text, source))
        return self.reply


async def test_plain_text_reaches_the_chat_hub(tmp_path):
    notifier, api, _ = build(tmp_path)
    notifier.hub = RecordingHub()

    await notifier.consume(message("what did you see today?"))

    assert notifier.hub.posted == [("what did you see today?", "telegram")]
    assert api.last_text() == "here you go"


async def test_slash_commands_never_reach_the_hub(tmp_path):
    """/pause has to stay instant; putting a model in that path would make the
    one command that stops claiming the slowest one."""
    notifier, api, store = build(tmp_path)
    notifier.hub = RecordingHub()

    await notifier.consume(message("/pause"))

    assert notifier.hub.posted == []
    assert store.get(PAUSED_KEY) is True


async def test_plain_text_without_a_hub_explains_itself(tmp_path):
    notifier, api, _ = build(tmp_path)

    await notifier.consume(message("hello?"))

    assert "can't chat yet" in api.last_text()


async def test_chat_from_a_stranger_is_ignored(tmp_path):
    """The authorization gate has to sit in front of the hub too, or an
    unlinked chat could drive the agent by conversation."""
    notifier, api, _ = build(tmp_path)
    notifier.hub = RecordingHub()

    await notifier.consume(message("pause everything", chat=STRANGER))

    assert notifier.hub.posted == []
