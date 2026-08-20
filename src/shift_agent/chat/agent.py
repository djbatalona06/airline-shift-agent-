"""The model turn behind the chat surface.

Mirrors `friction/vision_client.py` exactly: `ChatClient` is a `Protocol`, and
`AnthropicChatClient` imports `anthropic` inside `__init__` rather than at
module top. That keeps `import shift_agent.chat` free of any import-time
network or API-client construction, so the whole suite runs against a fake
client with no key and no network - matching ci.yml's rule for this repo.

## The claiming boundary (non-negotiable)

The tools below read state and toggle pause. **None of them claims a shift**,
and none may be added that does.

Claiming stays behind `Notifier.offer` and its explicit Confirm button. That
interface's contract is that a timeout returns False because "silence is never
consent" - and a model inferring consent from "yeah go for it, if it looks
good" is a weaker signal than the silence that interface already refuses. The
worst outcome available here is being rostered onto a trip she did not agree
to, out of a base she does not live in, and no conversational convenience is
worth putting a language model in that path.

`tests/test_chat_boundary.py` fails the build if `TOOLS` ever grows a claim
verb. Changing that is a new, explicit, written decision - the same rule
`friction/` operates under.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from ..config import UserConfig
from ..dashboard.data import build_settings, build_shifts, build_status
from ..store import PAUSE_REASON_KEY, PAUSED_KEY, Store

DEFAULT_CHAT_MODEL = "claude-sonnet-5"
MAX_HISTORY_TURNS = 40
MAX_TOOL_ROUNDS = 5

# Verbs the chat agent is allowed to perform. Read-mostly by construction; see
# the module docstring for why claiming is absent and must stay absent.
TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "get_status",
        "description": "Whether the agent is running or paused, its claim mode, "
                       "dry-run flag, and what the last poll cycle saw.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_settings",
        "description": "The user's configured availability, timezone, rules, "
                       "grades and poll interval. Contains no secrets.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_shifts",
        "description": "Recently seen shifts with the verdict the agent reached "
                       "for each one, newest first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "How many to return (default 20)."},
                "only_interesting": {
                    "type": "boolean",
                    "description": "Restrict to matched/alerted/gave-up shifts.",
                },
            },
        },
    },
    {
        "name": "pause",
        "description": "Stop claiming shifts until resumed. Use when the user asks to stop.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
        },
    },
    {
        "name": "resume",
        "description": "Start watching for shifts again after a pause.",
        "input_schema": {"type": "object", "properties": {}},
    },
)

SYSTEM_PROMPT = """You are the assistant built into a shift-monitoring agent \
that watches an employer's open-shift portal and claims shifts the user has \
confirmed she wants.

Answer questions about what the agent has seen and done, why a shift was or \
was not pursued, and what the current settings are. Use the tools rather than \
guessing - every answer about state should come from a tool call.

You can pause and resume monitoring when asked. You CANNOT claim a shift, and \
you must not imply you can: claiming happens only through the Confirm button \
on a shift offer, which the user presses herself. If asked to claim something, \
say plainly that she needs to confirm the offer and that you will not do it \
for her.

Be brief. This is read on a phone as often as on a screen. Plain text only - \
no markdown formatting, no tables."""


class ChatError(RuntimeError):
    """Raised when the chat model cannot be reached or replies unusably."""


@dataclass(frozen=True)
class ChatTurn:
    role: str          # "user" | "agent"
    text: str


class ChatClient(Protocol):
    async def reply(self, *, history: list[ChatTurn], tools: list[dict[str, Any]],
                    run_tool) -> str: ...


class ChatAgent:
    """Binds a `ChatClient` to this profile's store and config."""

    def __init__(self, config: UserConfig, store: Store, client: ChatClient) -> None:
        self.config = config
        self.store = store
        self.client = client

    async def respond(self, history: list[ChatTurn]) -> str:
        return await self.client.reply(
            history=history[-MAX_HISTORY_TURNS:], tools=list(TOOLS), run_tool=self.run_tool
        )

    def run_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Dispatch one tool call.

        An unknown name returns an error payload rather than raising: the model
        occasionally invents a tool, and that should be correctable inside the
        conversation instead of ending it.
        """
        if name == "get_status":
            return build_status(self.store, self.config)
        if name == "get_settings":
            return build_settings(self.store, self.config)
        if name == "list_shifts":
            shifts = build_shifts(self.store, self.config)
            if arguments.get("only_interesting"):
                shifts = [s for s in shifts if s["interesting"]]
            limit = arguments.get("limit")
            return shifts[: int(limit) if limit else 20]
        if name == "pause":
            self.store.set(PAUSED_KEY, True)
            self.store.set(PAUSE_REASON_KEY, str(arguments.get("reason") or "paused from chat"))
            return {"paused": True}
        if name == "resume":
            self.store.set(PAUSED_KEY, False)
            self.store.set(PAUSE_REASON_KEY, "")
            return {"paused": False}
        return {"error": f"unknown tool {name!r}"}


class AnthropicChatClient:
    """The one real `ChatClient`. `anthropic` is imported inside `__init__`."""

    def __init__(self, api_key: str, model: str = DEFAULT_CHAT_MODEL) -> None:
        import anthropic

        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model

    async def reply(self, *, history: list[ChatTurn], tools: list[dict[str, Any]],
                    run_tool) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "user" if t.role == "user" else "assistant", "content": t.text}
            for t in history
            if t.text.strip()
        ]
        if not messages:
            return ""

        for _ in range(MAX_TOOL_ROUNDS):
            try:
                response = await self._client.messages.create(
                    model=self._model,
                    max_tokens=1024,
                    system=SYSTEM_PROMPT,
                    tools=tools,
                    messages=messages,
                )
            except Exception as exc:  # anthropic.APIError and friends
                raise ChatError(f"chat model call failed: {exc}") from exc

            if response.stop_reason != "tool_use":
                return "".join(b.text for b in response.content if b.type == "text").strip()

            messages.append({"role": "assistant", "content": response.content})
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(
                                run_tool(block.name, dict(block.input or {})), default=str
                            ),
                        }
                        for block in response.content
                        if block.type == "tool_use"
                    ],
                }
            )

        # Ran the tool budget out. Say so rather than looping forever or
        # returning an empty string that reads as the agent ignoring her.
        return "I got stuck looking that up. Try asking a narrower question."
