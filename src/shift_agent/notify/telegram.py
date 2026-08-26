"""Telegram Bot API transport.

Raw httpx rather than python-telegram-bot: the four calls needed (sendMessage,
editMessageText, answerCallbackQuery, getUpdates) are simple, and the library
would be the heaviest dependency in the project for no gain.

The update listener runs as its own task alongside the poll loop so that /pause
and /status stay responsive while an offer is pending.
"""

from __future__ import annotations

import asyncio
import logging
import secrets as _secrets
import time
from datetime import tzinfo
from typing import Any

import httpx

from ..models import ClaimResult, Shift
from ..store import (
    CHALLENGE_PAUSE_KEY,
    PAUSE_REASON_KEY,
    PAUSED_KEY,
    TELEGRAM_CHAT_KEY,
    TELEGRAM_OFFSET_KEY,
    LAST_CYCLE_KEY,
    Store,
)
from .base import Notifier, describe

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
CONFIRM = "c"
SKIP = "s"

# Telegram allows roughly one message per second to a single chat.
MIN_SEND_INTERVAL = 1.0
# Cap on how long we will sit out a 429. Beyond this the message is not worth
# blocking the poll loop for.
MAX_RETRY_AFTER = 30.0


class TelegramError(RuntimeError):
    pass


def _retry_after(response: httpx.Response) -> float:
    """Seconds to wait, from Telegram's own answer where it gives one."""
    try:
        body = response.json()
        value = float((body.get("parameters") or {}).get("retry_after", 0))
    except (ValueError, AttributeError, TypeError):
        value = 0.0
    if value <= 0:
        try:
            value = float(response.headers.get("Retry-After") or 0)
        except ValueError:
            value = 0.0
    return min(max(value, MIN_SEND_INTERVAL), MAX_RETRY_AFTER)


class TelegramNotifier(Notifier):
    def __init__(
        self,
        token: str,
        store: Store,
        *,
        http: httpx.AsyncClient,
        link_code: str | None = None,
        chat_id: int | None = None,
        tz: tzinfo | None = None,
        poll_timeout: int = 25,
        chat_agent: Any = None,
        min_send_interval: float = MIN_SEND_INTERVAL,
        clock: Any = None,
        sleep: Any = None,
        user: str | None = None,
        hub: Any = None,
    ) -> None:
        self.token = token
        self.store = store
        self.http = http
        self.link_code = link_code
        self.tz = tz
        self.poll_timeout = poll_timeout
        # The same assistant the dashboard bubble uses, so /ask and the panel
        # give the same answers. None means /ask politely declines.
        self.chat_agent = chat_agent
        # Profile name, needed only to expire the link code from the keychain
        # once it has been used. Optional so tests can build a notifier without
        # touching the OS keychain at all.
        self.user = user
        # Set by main._run when the chat surface is enabled. None means plain
        # text gets the help blurb, exactly as before.
        self.hub = hub
        self._pending: dict[str, tuple[asyncio.Future[bool], str]] = {}
        self._listener: asyncio.Task[None] | None = None
        self._min_send_interval = min_send_interval
        self._clock = clock or time.monotonic
        # Injectable so tests can assert the pacing without waiting it out, the
        # same trick the poller uses for its backoff.
        self._sleep = sleep or asyncio.sleep
        self._last_send = -1e9
        self._send_lock = asyncio.Lock()

        if chat_id is not None and self.linked_chat_id is None:
            self.store.set(TELEGRAM_CHAT_KEY, chat_id)

    def _forget_link_code(self) -> None:
        """Expire the used link code from the keychain.

        Clearing `self.link_code` alone only makes it single-use *in this
        process*: `main._build_notifier` reads it back from the keychain on
        every start, so without this a restart revives a code that has already
        been spent.
        """
        if not self.user:
            return
        try:
            from .. import secrets as _keychain

            _keychain.delete(self.user, "telegram_link_code")
        except Exception as exc:
            # An unavailable keychain must not undo a successful link.
            log.warning("could not expire the used link code: %s", exc)

    # --- plumbing ------------------------------------------------------------

    @property
    def base(self) -> str:
        return f"{API_ROOT}/bot{self.token}"

    @property
    def linked_chat_id(self) -> int | None:
        value = self.store.get(TELEGRAM_CHAT_KEY)
        return int(value) if value is not None else None

    async def _call(self, method: str, **params: Any) -> Any:
        resp = await self.http.post(f"{self.base}/{method}", json=params)
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("ok"):
            raise TelegramError(f"{method} failed: {payload.get('description')}")
        return payload.get("result")

    async def _throttle(self) -> None:
        """Hold sends to roughly one a second in a single chat.

        Shift alerts are sparse enough that this never mattered. Conversational
        replies are not: `/ask` can produce several messages in a burst, and
        Telegram answers a burst with 429s. Being paced is better than having a
        captcha alert silently dropped because a chat reply used up the budget.
        """
        async with self._send_lock:
            now = self._clock()
            wait = self._min_send_interval - (now - self._last_send)
            if wait > 0:
                await self._sleep(wait)
            self._last_send = self._clock()

    async def _send(self, text: str, **extra: Any) -> dict[str, Any] | None:
        """Best-effort send. Never raises into the poll loop.

        A notification transport failing must not take down shift monitoring;
        the agent should keep working even if Telegram is unreachable.
        """
        chat = self.linked_chat_id
        if chat is None:
            log.warning("no linked chat; dropping message: %s", text[:80])
            return None

        for attempt in range(2):
            await self._throttle()
            try:
                return await self._call("sendMessage", chat_id=chat, text=text, **extra)
            except httpx.HTTPStatusError as exc:
                # Telegram says exactly how long to wait. Honour it once rather
                # than dropping a message that would have gone through.
                if exc.response.status_code == 429 and attempt == 0:
                    delay = _retry_after(exc.response)
                    log.warning("telegram rate limited; waiting %.1fs", delay)
                    await self._sleep(delay)
                    continue
                log.warning("telegram send failed: %s", exc)
                return None
            except (httpx.HTTPError, TelegramError) as exc:
                log.warning("telegram send failed: %s", exc)
                return None
        return None

    # --- listener ------------------------------------------------------------

    def start(self) -> None:
        if self._listener is None or self._listener.done():
            self._listener = asyncio.create_task(self._listen())

    async def close(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            try:
                await self._listener
            except asyncio.CancelledError:
                pass
            self._listener = None

    async def _listen(self) -> None:
        backoff = 1.0
        while True:
            try:
                offset = self.store.get(TELEGRAM_OFFSET_KEY)
                updates = await self._call(
                    "getUpdates",
                    offset=offset,
                    timeout=self.poll_timeout,
                    allowed_updates=["message", "callback_query"],
                )
                backoff = 1.0
                for update in updates or []:
                    await self.consume(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("telegram listener error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def consume(self, update: dict[str, Any]) -> None:
        """Process one update.

        The offset is advanced BEFORE handling, deliberately. If the process
        dies mid-handling the update is lost rather than replayed — and for a
        Confirm callback, losing it is far safer than replaying it, which would
        claim a shift on consent the user gave hours ago.
        """
        update_id = update.get("update_id")
        if update_id is not None:
            self.store.set(TELEGRAM_OFFSET_KEY, update_id + 1)

        try:
            if "callback_query" in update:
                await self._handle_callback(update["callback_query"])
            elif "message" in update:
                await self._handle_message(update["message"])
        except Exception:
            log.exception("failed handling update %s", update_id)

    def _authorized(self, chat_id: int | None) -> bool:
        linked = self.linked_chat_id
        return linked is not None and chat_id == linked

    # --- messages ------------------------------------------------------------

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if not text:
            return
        command, _, rest = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        rest = rest.strip()

        if self.linked_chat_id is None:
            if command == "/start":
                await self._try_link(chat_id, rest)
            return

        if not self._authorized(chat_id):
            # Silently ignore strangers rather than confirming the bot exists.
            log.warning("ignoring command from unlinked chat %s", chat_id)
            return

        # Slash commands keep their direct path with no model in the loop, so
        # /pause stays instant when it matters. Anything else is conversation.
        if not text.startswith("/"):
            await self._handle_chat(text)
            return

        await self._run_command(command, rest)

    async def _handle_chat(self, text: str) -> None:
        if self.hub is None:
            await self._send(
                "I can't chat yet - start the agent with --dashboard to enable it.\n"
                "Commands I do understand: /status /pause /resume /schedule"
            )
            return
        reply = await self.hub.post(text, source="telegram")
        if reply:
            await self._send(reply)

    async def _try_link(self, chat_id: int | None, supplied: str) -> None:
        """Bind the bot to a chat, gated on a one-time code.

        Without the code the first stranger to message the bot would become its
        owner, gaining the ability to pause monitoring or confirm claims.
        """
        if not self.link_code:
            await self._call(
                "sendMessage", chat_id=chat_id,
                text="This agent has no link code configured. Run: shift-agent link",
            )
            return
        # Compared as bytes. The str form of compare_digest is ASCII-only and
        # raises TypeError otherwise, which `consume` would swallow - leaving
        # someone whose keyboard inserted a smart quote with no reply at all
        # rather than "Invalid link code".
        if _secrets.compare_digest(supplied.encode("utf-8"), self.link_code.encode("utf-8")):
            self.store.set(TELEGRAM_CHAT_KEY, chat_id)
            self.link_code = None  # single use, in this process
            self._forget_link_code()
            await self._call(
                "sendMessage", chat_id=chat_id,
                text="Linked. I'll message you when a matching shift opens.\n"
                     "Commands: /status /pause /resume /schedule /help",
            )
        else:
            await self._call("sendMessage", chat_id=chat_id, text="Invalid link code.")

    async def _run_command(self, command: str, rest: str) -> None:
        if command == "/status":
            await self._send(self._status_text())
        elif command == "/pause":
            self.store.set(PAUSED_KEY, True)
            self.store.set(PAUSE_REASON_KEY, rest or "paused from Telegram")
            # Clear the challenge flag, or a stale one left by an earlier
            # captcha would let the agent quietly resume a pause a human asked
            # for. Only the agent's own challenge pause is self-recoverable.
            self.store.set(CHALLENGE_PAUSE_KEY, False)
            await self._send("Paused. Nothing will be claimed until you /resume.")
        elif command == "/resume":
            self.store.set(PAUSED_KEY, False)
            self.store.set(PAUSE_REASON_KEY, "")
            self.store.set(CHALLENGE_PAUSE_KEY, False)
            await self._send("Resumed. Watching for shifts again.")
        elif command == "/schedule":
            await self._send(self.store.get("schedule_summary") or "No schedule summary available.")
        elif command == "/ask":
            await self._ask(rest)
        else:
            await self._send(
                "Commands:\n"
                "/status - is the agent running, what did it see\n"
                "/pause - stop claiming\n"
                "/resume - start again\n"
                "/schedule - show your availability\n"
                "/ask <question> - ask about a shift or a rule"
            )

    async def _ask(self, question: str) -> None:
        """Answer from the same assistant the dashboard bubble uses.

        Config changes are deliberately not offered here. Approving a diff needs
        somewhere to read the diff, and a chat message is not that; the proposal
        would be approved on the strength of a one-line summary.
        """
        if self.chat_agent is None:
            await self._send(
                "No assistant is set up. Run 'shift-agent set-llm-key' on the machine "
                "running the agent."
            )
            return
        question = (question or "").strip()
        if not question:
            await self._send("Ask me something, for example: /ask why did you skip M8W77")
            return

        try:
            reply = await self.chat_agent.ask(question)
        except Exception as exc:
            log.warning("/ask failed: %s", exc)
            await self._send("I could not answer that just now.")
            return

        text = (reply.text or "").strip() or "I do not have an answer for that."
        if reply.proposal:
            text += "\n\nI can suggest a settings change for this - open the dashboard to see the diff and approve it."
        await self._send(text)

    async def system(self, text: str) -> None:
        """A health event rather than a shift event.

        Split from `alert` so the reason is visible at the call site: these are
        the messages that tell someone the agent has stopped being useful, which
        is otherwise only discoverable by noticing silence.
        """
        await self._send(f"🩺 {text}")

    def _status_text(self) -> str:
        paused = bool(self.store.get(PAUSED_KEY, False))
        lines = ["Paused" if paused else "Running"]
        if paused:
            reason = self.store.get(PAUSE_REASON_KEY)
            if reason:
                lines.append(f"Reason: {reason}")
        last = self.store.get(LAST_CYCLE_KEY)
        if last:
            lines.append(
                f"Last check: {last.get('at', '?')}\n"
                f"Seen {last.get('evaluated', 0)}, matched {last.get('matched', 0)}"
            )
        else:
            lines.append("No completed check yet.")
        return "\n".join(lines)

    # --- callbacks -----------------------------------------------------------

    async def _handle_callback(self, query: dict[str, Any]) -> None:
        chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
        query_id = query.get("id")

        if not self._authorized(chat_id):
            await self._answer(query_id, "Not authorized.")
            return

        kind, _, nonce = (query.get("data") or "").partition(":")
        entry = self._pending.get(nonce)
        if entry is None:
            # Unknown nonce: either expired, already answered, or replayed after
            # a restart (the pending map is in-memory by design).
            await self._answer(query_id, "That offer has expired.")
            await self._edit(query, "Expired - no action taken.")
            return

        future, _shift_id = entry
        confirmed = kind == CONFIRM
        if not future.done():
            future.set_result(confirmed)
        await self._answer(query_id, "Claiming..." if confirmed else "Skipped.")
        await self._edit(query, "Confirmed - attempting to claim." if confirmed else "Skipped.")

    async def _answer(self, query_id: str | None, text: str) -> None:
        if not query_id:
            return
        try:
            await self._call("answerCallbackQuery", callback_query_id=query_id, text=text)
        except (httpx.HTTPError, TelegramError) as exc:
            log.debug("answerCallbackQuery failed: %s", exc)

    async def _edit(self, query: dict[str, Any], suffix: str) -> None:
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        original = message.get("text", "")
        if not (chat_id and message_id):
            return
        try:
            await self._call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=f"{original}\n\n{suffix}",
            )
        except (httpx.HTTPError, TelegramError) as exc:
            log.debug("editMessageText failed: %s", exc)

    # --- Notifier interface --------------------------------------------------

    async def info(self, text: str) -> None:
        await self._send(text)

    async def alert(self, text: str) -> None:
        await self._send(f"⚠️ {text}")

    async def needs_human(self, reason: str, url: str | None = None) -> None:
        body = f"🔐 Action needed\n\n{reason}"
        if url:
            body += f"\n\nOpen: {url}"
        body += "\n\nSolve it, then send /resume."
        await self._send(body)

    async def offer(self, shift: Shift, timeout_minutes: int) -> bool:
        nonce = _secrets.token_urlsafe(8)
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[nonce] = (future, shift.id)

        text = (
            f"🗓 Shift available\n\n{describe(shift, self.tz)}\n\n"
            f"Confirm within {timeout_minutes} min or it will be skipped."
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Confirm", "callback_data": f"{CONFIRM}:{nonce}"},
                {"text": "✖️ Skip", "callback_data": f"{SKIP}:{nonce}"},
            ]]
        }

        sent = await self._send(text, reply_markup=keyboard)
        if sent is None:
            # Could not reach her, so we have no consent. Fail closed.
            self._pending.pop(nonce, None)
            return False

        try:
            return await asyncio.wait_for(future, timeout=timeout_minutes * 60)
        except (asyncio.TimeoutError, TimeoutError):
            await self._send(f"⌛ No answer in {timeout_minutes} min - skipped:\n{describe(shift, self.tz)}")
            return False
        finally:
            self._pending.pop(nonce, None)

    async def claim_outcome(self, shift: Shift, result: ClaimResult, dry_run: bool) -> None:
        icons = {"claimed": "✅", "lost_race": "🏃", "rejected": "🚫", "error": "❌"}
        icon = icons.get(result.outcome.value, "•")
        prefix = "[DRY RUN] " if dry_run else ""
        body = f"{icon} {prefix}{result.outcome.value.replace('_', ' ').title()}\n\n{describe(shift, self.tz)}"
        if result.detail:
            body += f"\n\n{result.detail}"
        await self._send(body)
