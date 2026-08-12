"""The two things that broke the VPS path, pinned so they cannot come back.

Both failed only on a headless Linux box, which is precisely where nobody was
running the tests — so they are exercised here by simulating that platform
rather than by needing one.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from shift_agent import secrets
from shift_agent.adapters.flica import NoDisplayError, _require_display


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point the whole module at a scratch directory and reset the cache."""
    monkeypatch.setenv("SHIFT_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(secrets, "_backend", None)
    yield
    monkeypatch.setattr(secrets, "_backend", None)


# --- display preflight ------------------------------------------------------


def test_linux_without_display_is_refused_in_english(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)

    with pytest.raises(NoDisplayError) as caught:
        _require_display()

    message = str(caught.value)
    # The point of the check is the guidance, not the exception type: a user
    # who cannot act on the message is no better off than with a traceback.
    assert "shift-agent-xvfb" in message
    assert "docs/VPS.md" in message
    assert "Traceback" not in message


def test_linux_with_display_is_allowed(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":99")
    _require_display()


def test_windows_is_never_blocked(monkeypatch):
    """Windows has no DISPLAY variable and does not need one."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("DISPLAY", raising=False)
    _require_display()


# --- secret store fallback --------------------------------------------------


def test_headless_linux_falls_back_to_a_file(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("SHIFT_AGENT_SECRETS", raising=False)
    monkeypatch.setattr(secrets.keyring, "set_password",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no D-Bus")))

    assert secrets.backend() == "file"


def test_windows_never_falls_back(monkeypatch):
    """A regression here would silently downgrade her laptop to a plain file."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("SHIFT_AGENT_SECRETS", raising=False)
    assert secrets.backend() == "os"


def test_file_backend_round_trips(monkeypatch):
    monkeypatch.setenv("SHIFT_AGENT_SECRETS", "file")

    secrets.put("aunt", "telegram_token", "12345:abcdef")
    assert secrets.get("aunt", "telegram_token") == "12345:abcdef"

    secrets.delete("aunt", "telegram_token")
    assert secrets.get("aunt", "telegram_token") is None


def test_file_backend_keeps_users_apart(monkeypatch):
    monkeypatch.setenv("SHIFT_AGENT_SECRETS", "file")
    secrets.put("aunt", "telegram_token", "hers")
    secrets.put("girlfriend", "telegram_token", "theirs")
    assert secrets.get("aunt", "telegram_token") == "hers"
    assert secrets.get("girlfriend", "telegram_token") == "theirs"


def test_missing_secret_is_none_not_an_error(monkeypatch):
    monkeypatch.setenv("SHIFT_AGENT_SECRETS", "file")
    assert secrets.get("nobody", "telegram_token") is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_secrets_file_is_not_world_readable(monkeypatch):
    """The file is the only thing protecting the token on a shared box."""
    monkeypatch.setenv("SHIFT_AGENT_SECRETS", "file")
    secrets.put("aunt", "telegram_token", "12345:abcdef")

    path = secrets._secrets_file()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"secrets file is {oct(mode)}, expected 0o600"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_corrupt_secrets_file_reports_rather_than_crashes(monkeypatch):
    monkeypatch.setenv("SHIFT_AGENT_SECRETS", "file")
    secrets.put("aunt", "telegram_token", "x")
    secrets._secrets_file().write_text("{not json", encoding="utf-8")

    with pytest.raises(secrets.SecretsUnavailable):
        secrets.get("aunt", "telegram_token")


def test_stored_file_contains_only_what_was_put(monkeypatch):
    monkeypatch.setenv("SHIFT_AGENT_SECRETS", "file")
    secrets.put("aunt", "telegram_token", "12345:abcdef")
    data = json.loads(secrets._secrets_file().read_text(encoding="utf-8"))
    assert data == {"aunt:telegram_token": "12345:abcdef"}
    assert os.environ.get("SHIFT_AGENT_SECRETS") == "file"
