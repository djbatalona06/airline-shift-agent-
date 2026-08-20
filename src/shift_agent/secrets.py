"""Secret storage — OS keychain, never a file.

On Windows this resolves to Credential Manager, so anything stored here is
encrypted against the logged-in Windows account and cannot be read back by
another user or by copying the file out. That matters more here than usual:
these are employer credentials on a personal machine, and the whole free tier
depends on them staying on her box and out of any file DJ might ever copy.

**What this module does NOT cover — the portal session.** The FLICA adapter
signs in through a Playwright *persistent* browser profile
(`paths.profile_dir(user)/browser`), so the live session lives on disk as an
ordinary Chromium profile, not in here. Two things protect it, neither of them
this module:

* `%LOCALAPPDATA%` is ACL-protected from other accounts on the machine.
* Chromium encrypts its own cookie store with DPAPI, keyed to the Windows login.

That is meaningfully weaker than the keychain — a running process under her
account can read that profile — and it is the deliberate tradeoff for a session
that survives restarts, so she clears a captcha once rather than every launch.
Anything that copies a profile directory off the machine is copying a live
bearer token for her crew account.
"""

from __future__ import annotations

import json
from typing import Any

import keyring
from keyring.errors import KeyringError

SERVICE = "shift-agent"


class SecretsUnavailable(RuntimeError):
    pass


def _key(user: str, name: str) -> str:
    return f"{user}:{name}"


def put(user: str, name: str, value: str) -> None:
    try:
        keyring.set_password(SERVICE, _key(user, name), value)
    except KeyringError as exc:
        raise SecretsUnavailable(f"could not write secret {name!r}: {exc}") from exc


def get(user: str, name: str) -> str | None:
    try:
        return keyring.get_password(SERVICE, _key(user, name))
    except KeyringError as exc:
        raise SecretsUnavailable(f"could not read secret {name!r}: {exc}") from exc


def delete(user: str, name: str) -> None:
    try:
        keyring.delete_password(SERVICE, _key(user, name))
    except keyring.errors.PasswordDeleteError:
        pass
    except KeyringError as exc:
        raise SecretsUnavailable(f"could not delete secret {name!r}: {exc}") from exc


def put_json(user: str, name: str, value: Any) -> None:
    put(user, name, json.dumps(value))


def get_json(user: str, name: str, default: Any = None) -> Any:
    raw = get(user, name)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def load_portal_secrets(user: str) -> dict[str, Any]:
    """Credentials handed to a `PortalAdapter` at construction.

    Only `telegram_token` is populated today. `portal_password`/`portal_cookies`
    were dropped rather than left as an unwired credential path: nothing wrote
    them, no CLI command set them, and FLICA authenticates through its
    persistent browser profile instead (see the module docstring). An adapter
    that does need a stored password should add its own key here together with
    the command that writes it.
    """
    return {
        "telegram_token": get(user, "telegram_token"),
    }
