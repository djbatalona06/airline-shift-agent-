"""Secret storage — OS keychain, never a file.

On Windows this resolves to Credential Manager, so the aunt's portal password
and live session cookies never touch disk in a form the agent itself can read
back without the logged-in user's context. That matters more here than usual:
these are employer credentials on a personal machine, and the whole free tier
depends on them staying on her box and out of any file DJ might ever copy.

Session cookies are treated as secrets too, not operational state — a live
cookie is a bearer token for her crew account.
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
    return {
        "password": get(user, "portal_password"),
        "cookies": get_json(user, "portal_cookies", default=[]),
        "telegram_token": get(user, "telegram_token"),
    }
