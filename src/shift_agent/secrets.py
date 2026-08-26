"""Secret storage — OS keychain where there is one, a locked-down file where
there is not.

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
import os
import sys
from pathlib import Path
from typing import Any

import keyring
from keyring.errors import KeyringError

from . import paths

SERVICE = "shift-agent"
_PROBE = "__backend_probe__"

# Resolved once per process: "os" or "file". Cached because the probe writes to
# the real keychain, and doing that on every get() would be both slow and rude.
_backend: str | None = None


class SecretsUnavailable(RuntimeError):
    pass


def _key(user: str, name: str) -> str:
    return f"{user}:{name}"


def _secrets_file() -> Path:
    return paths.app_root() / "secrets.json"


def backend() -> str:
    """Which store this process will use: ``"os"`` or ``"file"``.

    Windows and macOS always have a working keychain, so they are assumed rather
    than probed. Elsewhere the only trustworthy test is a real round trip — a
    backend can import cleanly and still fail on first use when there is no
    D-Bus session, which is exactly the VPS case.
    """
    global _backend
    if _backend is not None:
        return _backend

    forced = os.environ.get("SHIFT_AGENT_SECRETS")
    if forced in ("os", "file"):
        _backend = forced
        return _backend

    if sys.platform == "win32" or sys.platform == "darwin":
        _backend = "os"
        return _backend

    try:
        keyring.set_password(SERVICE, _PROBE, "1")
        ok = keyring.get_password(SERVICE, _PROBE) == "1"
        try:
            keyring.delete_password(SERVICE, _PROBE)
        except Exception:
            pass
        _backend = "os" if ok else "file"
    except Exception:
        _backend = "file"
    return _backend


def _read_file() -> dict[str, str]:
    path = _secrets_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SecretsUnavailable(f"could not read {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _write_file(data: dict[str, str]) -> None:
    path = _secrets_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        # Create with 0600 from the outset. Writing then chmod-ing leaves a
        # window where another account on the box can open the file.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
    except OSError as exc:
        raise SecretsUnavailable(f"could not write {path}: {exc}") from exc


def put(user: str, name: str, value: str) -> None:
    if backend() == "file":
        data = _read_file()
        data[_key(user, name)] = value
        _write_file(data)
        return
    try:
        keyring.set_password(SERVICE, _key(user, name), value)
    except KeyringError as exc:
        raise SecretsUnavailable(f"could not write secret {name!r}: {exc}") from exc


def get(user: str, name: str) -> str | None:
    if backend() == "file":
        return _read_file().get(_key(user, name))
    try:
        return keyring.get_password(SERVICE, _key(user, name))
    except KeyringError as exc:
        raise SecretsUnavailable(f"could not read secret {name!r}: {exc}") from exc


def delete(user: str, name: str) -> None:
    if backend() == "file":
        data = _read_file()
        if data.pop(_key(user, name), None) is not None:
            _write_file(data)
        return
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


LLM_KEY = "llm_api_key"


def load_llm_secrets(user: str) -> dict[str, Any]:
    """Deliberately not folded into `load_portal_secrets`.

    That dict is handed straight to the portal adapter, which drives a browser
    against an employer's site. An LLM key has no business travelling into it.
    """
    return {"api_key": get(user, LLM_KEY)}
