"""`_build_chat_hub` must never be able to stop the agent from polling.

Found by driving the real CLI rather than by a test: on a box where the
`anthropic` package could not be imported, `run --dashboard` died with a
traceback before the first poll cycle. The chat panel is a convenience and shift
monitoring is the product — docs/SECURITY.md's failure-mode table says so — so
every way this function can fail has to end in `None` and a printed reason.
"""

from __future__ import annotations

import pytest

from shift_agent import main as cli
from shift_agent import secrets


@pytest.fixture
def config_and_store(chat_store_and_config):
    store, config = chat_store_and_config
    return config, store


def test_no_stored_key_turns_chat_off_quietly(monkeypatch, capsys, config_and_store):
    config, store = config_and_store
    monkeypatch.setattr(secrets, "get", lambda user, name: None)

    assert cli._build_chat_hub(config, store, notifier=None) is None
    assert "friction-set-vision-key" in capsys.readouterr().out


def test_an_unreadable_secret_store_turns_chat_off_rather_than_raising(
    monkeypatch, capsys, config_and_store
):
    config, store = config_and_store

    def boom(user, name):
        raise secrets.SecretsUnavailable("no keychain on this box")

    monkeypatch.setattr(secrets, "get", boom)

    assert cli._build_chat_hub(config, store, notifier=None) is None
    assert "Chat is off" in capsys.readouterr().out


def test_a_client_that_cannot_be_constructed_turns_chat_off_rather_than_raising(
    monkeypatch, capsys, config_and_store
):
    """The real regression: a missing or incompatible `anthropic` raises inside
    `AnthropicChatClient.__init__`, which used to propagate all the way out of
    `_run` and take shift monitoring with it."""
    config, store = config_and_store
    monkeypatch.setattr(secrets, "get", lambda user, name: "sk-ant-whatever")

    import shift_agent.chat.agent as agent_module

    def boom(_key):
        raise ModuleNotFoundError("No module named 'anthropic'")

    monkeypatch.setattr(agent_module, "AnthropicChatClient", boom)

    assert cli._build_chat_hub(config, store, notifier=None) is None
    assert "Chat is off" in capsys.readouterr().out


def test_the_reason_is_scrubbed_before_it_is_printed(monkeypatch, capsys, config_and_store):
    """The message reaches a terminal she may screenshot into a support thread,
    so it goes through the same scrubber as everything else that leaves."""
    config, store = config_and_store
    monkeypatch.setattr(secrets, "get", lambda user, name: "sk-ant-whatever")

    import shift_agent.chat.agent as agent_module

    def boom(_key):
        raise RuntimeError("bad key sk-ant-api03-0123456789abcdefghijklmnop")

    monkeypatch.setattr(agent_module, "AnthropicChatClient", boom)

    assert cli._build_chat_hub(config, store, notifier=None) is None
    out = capsys.readouterr().out
    assert "sk-ant-api03-0123456789abcdefghijklmnop" not in out
    assert "[API-KEY]" in out


def test_a_working_client_still_produces_a_hub(monkeypatch, config_and_store):
    """The guard must not swallow the success path."""
    config, store = config_and_store
    monkeypatch.setattr(secrets, "get", lambda user, name: "sk-ant-whatever")

    import shift_agent.chat.agent as agent_module

    class FakeClient:
        def __init__(self, key):
            self.key = key

    monkeypatch.setattr(agent_module, "AnthropicChatClient", FakeClient)

    hub = cli._build_chat_hub(config, store, notifier=None)
    assert hub is not None
