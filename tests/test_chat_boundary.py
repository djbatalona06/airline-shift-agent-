"""Enforces that the chat agent can never claim a shift.

Claiming stays behind `Notifier.offer` and its explicit Confirm button, whose
contract is that a timeout returns False because silence is never consent. A
model inferring consent from conversational text is a weaker signal than the
silence that interface already refuses.

This is the same kind of tripwire as `test_friction_boundary.py`: it fails the
moment a claiming verb appears in the tool registry, long before such a tool
could actually be called.
"""

from __future__ import annotations

from pathlib import Path

import shift_agent.chat.agent as chat_agent

# Substrings that would indicate a tool taking a shift rather than reading one.
FORBIDDEN = ("claim", "request_shift", "take_shift", "bid", "pickup", "pick_up", "award")


def test_no_tool_can_claim_a_shift():
    offenders = [
        tool["name"]
        for tool in chat_agent.TOOLS
        if any(word in tool["name"].lower() for word in FORBIDDEN)
    ]
    assert not offenders, (
        f"{offenders} look like claiming tools. The chat agent must never be able to "
        "claim a shift - that requires an explicit Confirm from the user via "
        "Notifier.offer. Adding one is a new, explicit, written decision recorded "
        "in docs/SECURITY.md and chat/agent.py's module docstring."
    )


def test_tool_dispatch_rejects_a_claim_verb(chat_store_and_config):
    """Even if a model invents one, dispatch must not honour it."""
    store, config = chat_store_and_config
    agent = chat_agent.ChatAgent(config, store, client=None)

    result = agent.run_tool("claim_shift", {"shift_id": "S1"})

    assert "error" in result
    assert store.db.execute("SELECT COUNT(*) AS n FROM claims").fetchone()["n"] == 0


def test_chat_package_does_not_touch_the_adapters():
    """The chat surface talks to the store, never to a live portal session.

    Reaching into an adapter would give a model a path to the browser holding
    her authenticated portal session - the same reasoning as the friction
    boundary, for the same reason.
    """
    chat_dir = Path(chat_agent.__file__).parent
    offenders = [
        py.name
        for py in chat_dir.glob("*.py")
        if "adapters" in py.read_text(encoding="utf-8")
    ]
    assert not offenders, f"{offenders} mention 'adapters'; the chat surface must not."
