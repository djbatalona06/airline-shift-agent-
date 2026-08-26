"""FrictionConfig / FrictionImapSettings validation. No network."""

from __future__ import annotations

import pytest
import yaml

from shift_agent.friction.config import FrictionConfig, FrictionImapSettings


def test_defaults_with_no_imap():
    config = FrictionConfig()
    assert config.imap is None
    assert config.vision_model


def test_loads_from_yaml(tmp_path):
    path = tmp_path / "friction.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "vision_model": "claude-sonnet-5",
                "imap": {"host": "imap.example.com", "username": "me@example.com"},
            }
        )
    )
    config = FrictionConfig.load(path)
    assert config.imap.host == "imap.example.com"
    assert config.imap.port == 993


def test_rejects_unknown_fields():
    with pytest.raises(Exception):
        FrictionConfig.model_validate({"unexpected": True})


def test_imap_settings_require_host_and_username():
    with pytest.raises(Exception):
        FrictionImapSettings.model_validate({})


def test_load_default_falls_back_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFT_AGENT_HOME", str(tmp_path))
    result = FrictionConfig.load_default("someone-new")
    assert result == FrictionConfig()
