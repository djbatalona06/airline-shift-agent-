"""The fan-out point that makes two surfaces one conversation.

Every message from either front door goes through `ChatHub.post`. It appends
the user turn, gets a reply, appends that, and mirrors both to the surface that
did not originate them. Nothing else writes `chat_messages`, which is what
keeps the dashboard panel and the Telegram thread from diverging.

Serialised behind a lock: two surfaces posting at once would otherwise
interleave turns in the model's history and produce a reply to a conversation
neither person had.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import UserConfig
from ..logging_safe import scrub
from ..store import Store
from .agent import ChatAgent, ChatTurn

log = logging.getLogger(__name__)

DASHBOARD = "dashboard"
TELEGRAM = "telegram"
SYSTEM = "system"

MAX_MESSAGE_CHARS = 4000


class ChatHub:
    def __init__(
        self,
        config: UserConfig,
        store: Store,
        agent: ChatAgent,
        *,
        notifier=None,
    ) -> None:
        self.config = config
        self.store = store
        self.agent = agent
        # The Telegram notifier, when there is one. Used only to mirror; the
        # hub works with it set to None (dashboard-only, or console mode).
        self.notifier = notifier
        self._lock = asyncio.Lock()

    @property
    def user(self) -> str:
        return self.config.name

    async def history(self, after_id: int = 0) -> list[dict[str, object]]:
        """Async despite doing no awaiting, so that every store access goes
        through the loop that owns the SQLite connection.

        sqlite3 rejects cross-thread use of a connection, and the dashboard
        server answers on its own handler threads - a synchronous reader here
        would work in tests that call it directly and fail over HTTP.
        """
        return [_as_dict(row) for row in self.store.messages_after(self.user, after_id)]

    async def post(self, text: str, *, source: str) -> str:
        """Record one message, answer it, and mirror both. Returns the reply.

        Never raises into a caller: a poll loop or an HTTP handler must not die
        because the model was unreachable. The failure is recorded as a system
        turn so it is visible in the thread rather than lost.
        """
        cleaned = (text or "").strip()[:MAX_MESSAGE_CHARS]
        if not cleaned:
            return ""

        async with self._lock:
            self.store.append_message(self.user, "user", source, cleaned)
            if source != TELEGRAM:
                # Mirror the human's own message so the Telegram thread reads as
                # one conversation rather than half of one.
                await self._to_telegram(cleaned, prefix="you (dashboard): ")

            try:
                turns = [
                    ChatTurn(role=row["role"], text=row["text"])
                    for row in self.store.recent_messages(self.user)
                ]
                reply = (await self.agent.respond(turns)).strip()
            except Exception as exc:
                log.warning("chat turn failed: %s", scrub(exc))
                reply = "I couldn't reach the assistant just then. Try again in a moment."
                self.store.append_message(self.user, "agent", SYSTEM, reply)
                return reply

            if not reply:
                return ""

            self.store.append_message(self.user, "agent", source, reply)
            if source != TELEGRAM:
                await self._to_telegram(reply)
            return reply

    async def note(self, text: str) -> None:
        """Record an agent-authored line that nobody asked for.

        Lets the poll loop drop context into the thread - a claim outcome, a
        captcha hand-off - so the conversation reflects what the agent actually
        did rather than only what it was asked.
        """
        cleaned = (text or "").strip()[:MAX_MESSAGE_CHARS]
        if cleaned:
            self.store.append_message(self.user, "agent", SYSTEM, cleaned)

    async def _to_telegram(self, text: str, prefix: str = "") -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier.info(f"{prefix}{text}")
        except Exception as exc:
            # `info` is already best-effort, but a mirror failing must never
            # lose the reply that was already stored.
            log.debug("chat mirror to telegram failed: %s", scrub(exc))


def _as_dict(row) -> dict[str, object]:
    return {
        "id": row["id"],
        "role": row["role"],
        "source": row["source"],
        "text": row["text"],
        "created_at": row["created_at"],
    }
