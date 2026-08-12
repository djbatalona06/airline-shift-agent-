"""Per-profile filesystem layout.

The same executable is shipped to more than one person — an aunt at one airline,
potentially an uncle-in-law at another. Their data must never mix.

Isolation has three layers, and only the third is ours:

1. **Windows account.** `%LOCALAPPDATA%` resolves per signed-in user and is
   ACL-protected from other accounts on the machine. Two people on separate
   machines, or separate Windows logins, are already fully separated.
2. **Credential Manager.** Secrets are encrypted against the Windows login that
   wrote them. Another account cannot read them even with disk access.
3. **Profile namespace.** Within one Windows account, each configured profile
   gets its own directory and its own SQLite file — so one person running two
   airlines' agents, or testing, never cross-contaminates.

Every per-user artefact resolves through this module. Nothing writes to the
install directory: a shipped exe may live in Program Files, which is read-only
for a standard user.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

APP_NAME = "shift-agent"


def slugify(name: str) -> str:
    """Filesystem-safe profile id.

    Rejects empty results rather than silently falling back to a shared
    directory — two users colliding on one folder is the exact failure this
    module exists to prevent.
    """
    normalised = unicodedata.normalize("NFKD", name)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", ascii_only).strip("-.").lower()
    if not slug:
        raise ValueError(f"profile name {name!r} produces no usable directory name")
    return slug[:64]


def app_root() -> Path:
    """Base data directory for the current Windows user."""
    override = os.environ.get("SHIFT_AGENT_HOME")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / f".{APP_NAME}"


def profile_dir(user: str, create: bool = True) -> Path:
    path = app_root() / "profiles" / slugify(user)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def state_db(user: str, create: bool = True) -> Path:
    return profile_dir(user, create) / "state.db"


def dashboard_dir(user: str, create: bool = True) -> Path:
    path = profile_dir(user, create) / "dashboard"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def config_path(user: str, create: bool = True) -> Path:
    return profile_dir(user, create) / "config.yaml"


def list_profiles() -> list[str]:
    root = app_root() / "profiles"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())
