"""Glue between the loopback server and the agent.

Kept separate from `agent.py` so the agent stays a plain object a test can drive
directly, and separate from `server.py` so that module never imports a provider.

This is also where the two halves of a config change meet: `handle_chat` can
reach `propose_config_change`, and only `handle_apply` — which is called when
someone presses a button — can reach the write.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..config import UserConfig
from ..logging_safe import scrub
from ..store import Store
from .agent import ChatAgent
from .providers import Provider, ProviderError
from .tools import ToolBox, ToolError

log = logging.getLogger(__name__)


class ChatService:
    """Implements the `ChatBackend` protocol the dashboard server expects."""

    def __init__(
        self,
        store: Store,
        config: UserConfig,
        provider: Provider,
        config_path: Path | None = None,
    ) -> None:
        self.tools = ToolBox(store, config, config_path=config_path)
        self.agent = ChatAgent(provider, self.tools)
        self.store = store

    async def handle_chat(self, question: str, history: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            reply = await self.agent.ask(question, history=history)
        except ProviderError as exc:
            # Already phrased for a human by `providers._friendly`.
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            log.warning("chat failed: %s", scrub(exc))
            return {"ok": False, "error": "Something went wrong answering that."}

        return {
            "ok": True,
            "text": reply.text,
            "proposal": reply.proposal,
            "tools_used": reply.tools_used,
        }

    async def handle_apply(self, change_id: str) -> dict[str, Any]:
        try:
            proposal = self.tools.apply(change_id)
        except ToolError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            log.warning("apply failed: %s", scrub(exc))
            return {"ok": False, "error": "Could not write the config file."}

        return {
            "ok": True,
            "summary": proposal.summary,
            "restart_required": True,
            "note": "Saved. The change takes effect the next time the agent starts.",
        }
