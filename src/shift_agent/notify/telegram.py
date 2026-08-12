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
from datetime import tzinfo
from typing import Any

import httpx

from ..models import ClaimResult, Shift
from ..store import (
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


class TelegramError(RuntimeError):
    pass


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
    ) -> None:
        self.token = token
        self.store = store
        self.http = http
        self.link_code = link_code
        self.tz = tz
        self.poll_timeout = poll_timeout
        self._pending: dict[str, tuple[asyncio.Future[bool], str]] = {}
        self._listener: asyncio.Task[None] | None = None

        if chat_id is not None and self.linked_chat_id is None:
            self.store.set(TELEGRAM_CHAT_KEY, chat_id)

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

    async def _send(self, text: str, **extra: Any) -> dict[str, Any] | None:
        """Best-effort send. Never raises into the poll loop.

        A notification transport failing must not take down shift monitoring;
        the agent should keep working even if Telegram is unreachable.
        """
        chat = self.linked_chat_id
        if chat is None:
            log.warning("no linked chat; dropping message: %s", text[:80])
            return None
        try:
            return await self._call("sendMessage", chat_id=chat, text=text, **extra)
        except (httpx.HTTPError, TelegramError) as exc:
            log.warning("telegram send failed: %s", exc)
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

        await self._run_command(command, rest)

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
        if _secrets.compare_digest(supplied, self.link_code):
            self.store.set(TELEGRAM_CHAT_KEY, chat_id)
            self.link_code = None  # single use
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
            await self._send("Paused. Nothing will be claimed until you /resume.")
        elif command == "/resume":
            self.store.set(PAUSED_KEY, False)
            self.store.set(PAUSE_REASON_KEY, "")
            await self._send("Resumed. Watching for shifts again.")
        elif command == "/schedule":
            await self._send(self.store.get("schedule_summary") or "No schedule summary available.")
        else:
            await self._send(
                "Commands:\n"
                "/status - is the agent running, what did it see\n"
                "/pause - stop claiming\n"
                "/resume - start again\n"
                "/schedule - show your availability"
            )

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
