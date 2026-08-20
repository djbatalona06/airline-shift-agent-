"""Secret-setup handlers for the `shift-agent friction-*` subcommands.

Plain async functions, not their own argparse - the parsing lives in
`main.py` alongside every other subcommand, matching how `_set_token`/`_link`
already work there.
"""

from __future__ import annotations

import sys

from .. import secrets


async def set_vision_key(user: str) -> int:
    import getpass

    key = getpass.getpass("Vision model API key (input hidden): ").strip()
    if not key:
        print("No key entered.", file=sys.stderr)
        return 1
    secrets.put(user, "friction_vision_api_key", key)
    print(f"Stored for user {user!r}. Try: shift-agent friction-bench --user {user}")
    return 0


async def set_imap_password(user: str) -> int:
    import getpass

    password = getpass.getpass("IMAP app password (input hidden): ").strip()
    if not password:
        print("No password entered.", file=sys.stderr)
        return 1
    secrets.put(user, "friction_imap_app_password", password)
    print(f"Stored for user {user!r}. Try: shift-agent friction-otp --user {user}")
    return 0


async def fetch_otp(user: str, *, timeout_s: float) -> int:
    """Wait for a one-time code to arrive and print it.

    The entry point for `imap_otp.fetch_latest_otp`. Without this the IMAP half
    of the toolkit has a stored password and no way to spend it.

    Run in a thread: `fetch_latest_otp` is blocking stdlib `imaplib`, and
    blocking the event loop for up to two minutes would freeze anything else
    sharing it.
    """
    import asyncio

    from .config import FrictionConfig, friction_config_path, load_friction_secrets

    config = FrictionConfig.load_default(user)
    if config.imap is None:
        print(
            "No IMAP settings configured. Add an `imap:` block to\n"
            f"  {friction_config_path(user)}\n"
            "with at least `host` and `username`.",
            file=sys.stderr,
        )
        return 2

    creds = load_friction_secrets(user)
    if not creds.imap_app_password:
        print(
            f"No IMAP password stored for user {user!r}. "
            f"Run: shift-agent friction-set-imap-password --user {user}",
            file=sys.stderr,
        )
        return 2

    from .imap_otp import fetch_latest_otp

    print(f"Watching {config.imap.username} for a code (up to {timeout_s:.0f}s)...")
    code = await asyncio.to_thread(
        fetch_latest_otp,
        config.imap.to_imap_config(),
        creds.imap_app_password,
        config.imap.to_search(),
        timeout_s=timeout_s,
    )
    if code is None:
        print("No matching code arrived in time.", file=sys.stderr)
        return 1
    print(code)
    return 0
