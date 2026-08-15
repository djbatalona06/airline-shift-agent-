"""Talking to a model, over two shapes.

Anthropic is the default and the one the project supports properly. The
OpenAI-compatible path exists for a local model — Ollama, llama.cpp, LM Studio —
because that is the only configuration where using this feature does not send a
crew member's roster to anybody's server.

Both are reduced to one small `Reply`, so `agent.py` never learns which is in
use and a third could be added without touching the tool loop.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ..logging_safe import scrub

log = logging.getLogger(__name__)

# Long enough for a slow local model on a laptop, short enough that a wedged
# endpoint does not hold a dashboard request open forever.
TIMEOUT_SECONDS = 120.0


class ProviderError(RuntimeError):
    """Anything that stops us getting an answer, already safe to show a user."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Reply:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    # Set when the model declined rather than answered. Callers must check this
    # before treating empty text as a bug.
    refused: bool = False
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class Provider(Protocol):
    name: str

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Reply: ...

    async def close(self) -> None: ...


class AnthropicProvider:
    """Claude, via the official SDK.

    Adaptive thinking is on and `effort` carries the cost dial, because a fixed
    token budget is the wrong knob for a chat box where one question is "what
    time is M4A76" and the next is "re-plan my whole week".
    """

    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "claude-opus-5",
        max_tokens: int = 4096,
        effort: str = "medium",
        client: Any = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self._client = client
        self._api_key = api_key

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ProviderError(
                "The 'anthropic' package is not installed. Reinstall the app, or "
                "switch llm.provider to 'openai_compatible' for a local model."
            ) from exc
        self._client = anthropic.AsyncAnthropic(api_key=self._api_key, timeout=TIMEOUT_SECONDS)
        return self._client

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Reply:
        client = self._ensure_client()
        try:
            response = await client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=messages,
                tools=[
                    {
                        "name": t["name"],
                        "description": t["description"],
                        "input_schema": t["parameters"],
                    }
                    for t in tools
                ],
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
            )
        except Exception as exc:
            raise ProviderError(_friendly(exc)) from exc

        # Check this before reading content: a declined request comes back as a
        # perfectly good 200 with nothing in it, and indexing content[0] here
        # would surface as a crash rather than as the refusal it is.
        if getattr(response, "stop_reason", None) == "refusal":
            return Reply(
                text="I can't answer that one. Try asking about your shifts or your rules.",
                refused=True,
            )

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content or []:
            kind = getattr(block, "type", None)
            if kind == "text":
                text_parts.append(block.text)
            elif kind == "tool_use":
                calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {}))
                )

        usage = getattr(response, "usage", None)
        return Reply(
            text="".join(text_parts).strip(),
            tool_calls=tuple(calls),
            usage={
                "input_tokens": getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0),
            },
        )

    async def close(self) -> None:
        client = self._client
        if client is not None and hasattr(client, "close"):
            try:
                await client.close()
            except Exception:  # pragma: no cover - shutdown is best effort
                pass


class OpenAICompatibleProvider:
    """Anything speaking /chat/completions — chiefly a local model.

    Raw httpx rather than the openai package, for the same reason the Telegram
    notifier avoids python-telegram-bot: this is one endpoint with one response
    shape, and the dependency would be the largest in the project for no gain.
    """

    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        max_tokens: int = 4096,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self._api_key = api_key
        self._http = http
        self._owns_http = http is None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        return self._http

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Reply:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "system", "content": system}, *_to_openai(messages)],
        }
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]

        try:
            response = await self._client().post(
                f"{self.base_url}/chat/completions", json=body, headers=headers
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise ProviderError(_friendly(exc)) from exc

        choices = payload.get("choices") or []
        if not choices:
            raise ProviderError("The model returned no answer.")
        message = choices[0].get("message") or {}

        calls: list[ToolCall] = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                # A local model that emits malformed arguments should read as a
                # skipped tool, not as a 500 on the dashboard.
                log.warning("discarding tool call with unparseable arguments: %s", fn.get("name"))
                continue
            calls.append(
                ToolCall(id=call.get("id") or fn.get("name", ""), name=fn.get("name", ""),
                         arguments=arguments if isinstance(arguments, dict) else {})
            )

        return Reply(
            text=(message.get("content") or "").strip(),
            tool_calls=tuple(calls),
            usage=payload.get("usage") or {},
        )

    async def close(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None


def _to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten Anthropic-shaped history into the OpenAI shape.

    The agent keeps one canonical history in Anthropic's format because that is
    the provider it is built around; this is the lossy edge for the other one.
    Tool results become plain user text, which every local model understands and
    which costs only the ability to link a result back to its call id.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            out.append({"role": message["role"], "content": content})
            continue

        texts: list[str] = []
        for block in content or []:
            kind = block.get("type")
            if kind == "text":
                texts.append(block.get("text", ""))
            elif kind == "tool_result":
                texts.append(f"Tool result: {block.get('content')}")
            elif kind == "tool_use":
                texts.append(f"(requested {block.get('name')})")
        if texts:
            out.append({"role": message["role"], "content": "\n".join(texts)})
    return out


def _friendly(exc: Exception) -> str:
    """Turn a provider failure into something worth showing in a chat panel.

    Scrubbed unconditionally: an httpx error carries the request URL, and an
    auth failure is exactly the kind of message that quotes the key back.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 401 or status == 403:
        return "The API key was rejected. Re-run 'shift-agent set-llm-key'."
    if status == 429:
        return "Rate limited by the provider. Try again in a moment."
    if status and status >= 500:
        return f"The provider returned an error ({status}). Try again shortly."
    if isinstance(exc, httpx.ConnectError):
        return "Could not reach the model endpoint. Is it running?"
    if isinstance(exc, httpx.TimeoutException):
        return "The model took too long to answer."
    return f"Could not reach the model: {scrub(exc)}"
