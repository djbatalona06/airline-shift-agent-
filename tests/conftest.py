"""Shared fixtures.

Only the chat surface needs a store-plus-config pair today; the older test
modules build their own with local helpers and are left alone.
"""

from __future__ import annotations

import pytest

from shift_agent.config import UserConfig
from shift_agent.store import Store

ALL_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def make_chat_config(**over) -> UserConfig:
    base = {
        "name": "tester",
        "portal": {"adapter": "mock"},
        "availability": {
            "timezone": "America/New_York",
            "slots": [{"day": d, "start": "06:00", "end": "22:00"} for d in ALL_DAYS],
        },
    }
    base.update(over)
    return UserConfig.model_validate(base)


@pytest.fixture
def chat_store_and_config(tmp_path):
    store = Store(tmp_path / "state.db")
    try:
        yield store, make_chat_config()
    finally:
        store.close()
