"""The conversational surface: one thread, two front doors.

The dashboard's chat panel and the Telegram bot are the same conversation.
Both call `ChatHub.post`, both read the same `chat_messages` rows, and the hub
mirrors each turn to whichever surface did not originate it. History is
server-side, so reopening the dashboard resumes the thread rather than
restarting it, and Telegram carries it to every device already signed in.

The agent behind it is read-mostly on purpose - see `agent.py`'s docstring for
the claiming boundary and `tests/test_chat_boundary.py` for the guard.
"""

from __future__ import annotations

from .agent import ChatAgent, ChatClient, ChatTurn
from .hub import ChatHub

__all__ = ["ChatAgent", "ChatClient", "ChatHub", "ChatTurn"]
