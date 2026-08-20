"""Config and secrets access for the friction toolkit.

Mirrors the split the rest of the app already uses: non-secret settings in a
per-profile YAML file (`config.py`'s `UserConfig` pattern, `paths.py`'s
`profile_dir`), the vision API key and IMAP app password in the OS keychain
(`secrets.py`). Neither of those modules is modified - this file only calls
into them with new names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from .. import paths, secrets
from .imap_otp import DEFAULT_CODE_PATTERN, ImapConfig, OtpSearch
from .vision_client import DEFAULT_VISION_MODEL


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FrictionImapSettings(_Base):
    """Serialised form of `imap_otp`'s `ImapConfig`/`OtpSearch`.

    Two types for one concept, deliberately: `imap_otp` stays a dependency-free
    stdlib module with plain dataclasses, and pydantic validation lives here
    with the rest of the config loading. `to_imap_config`/`to_search` are the
    bridge - without them the YAML is decorative.
    """

    host: str
    username: str
    port: int = 993
    mailbox: str = "INBOX"
    sender_contains: str | None = None
    subject_contains: str | None = None
    since_minutes: int = 10
    code_pattern: str = DEFAULT_CODE_PATTERN

    def to_imap_config(self) -> ImapConfig:
        return ImapConfig(
            host=self.host, username=self.username, port=self.port, mailbox=self.mailbox
        )

    def to_search(self) -> OtpSearch:
        return OtpSearch(
            sender_contains=self.sender_contains,
            subject_contains=self.subject_contains,
            since_minutes=self.since_minutes,
            code_pattern=self.code_pattern,
        )


class FrictionConfig(_Base):
    vision_model: str = DEFAULT_VISION_MODEL
    imap: FrictionImapSettings | None = None

    @classmethod
    def load(cls, path: str | Path) -> FrictionConfig:
        raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)

    @classmethod
    def load_default(cls, user: str) -> FrictionConfig:
        """Falls back to defaults when no friction.yaml has been written yet -
        the toolkit works with just a vision key and no config file at all."""
        path = friction_config_path(user)
        if not path.is_file():
            return cls()
        return cls.load(path)


class FrictionSecrets(BaseModel):
    vision_api_key: str | None
    imap_app_password: str | None


def friction_config_path(user: str) -> Path:
    return paths.profile_dir(user) / "friction.yaml"


def load_friction_secrets(user: str) -> FrictionSecrets:
    return FrictionSecrets(
        vision_api_key=secrets.get(user, "friction_vision_api_key"),
        imap_app_password=secrets.get(user, "friction_imap_app_password"),
    )
