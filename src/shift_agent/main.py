"""CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from . import logging_safe, paths, secrets
from .logging_safe import scrub
# These imports look unused and are not. The adapter registry is populated by
# decorators, so PyInstaller's static analysis sees no reference to these
# modules; without them the packaged exe builds cleanly and then fails at
# runtime with "unknown adapter". See packaging/README.md.
from .adapters import flica as _flica  # noqa: F401  -- registers the "flica" adapter
from .adapters import mock as _mock  # noqa: F401  -- registers the "mock" adapter
from .adapters.base import available_adapters, get_adapter
from .config import UserConfig
from .models import Shift
from .notify.console import ConsoleNotifier
from .notify.telegram import TelegramNotifier
from .poller import Poller
from .store import Store

DEFAULT_STATE = Path.home() / ".shift-agent" / "state.db"


def _configure_browsers() -> None:
    """Point Playwright at the Chromium shipped beside the executable.

    Chromium is deliberately not inside the exe: it is ~150 MB and --onefile
    unpacks its whole payload to a temp folder on every launch. It travels in a
    `browsers/` folder next to the exe instead.

    Without this, a frozen build looks inside its own temp extraction, finds
    nothing, and prints Playwright's "run playwright install" banner — advice
    that makes no sense to someone who was handed a zip file.
    """
    if not getattr(sys, "frozen", False):
        return
    beside = Path(sys.executable).parent / "browsers"
    if beside.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(beside))


def browsers_available() -> bool:
    path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if path:
        return any(Path(path).glob("chromium*"))
    return True   # source installs resolve through Playwright's own default


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Backstop against portal error text carrying tokens into the log file.
    logging_safe.install()


def _schedule_summary(config: UserConfig) -> str:
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    lines = [f"Availability ({config.availability.timezone}):"]
    if not config.availability.slots:
        lines.append("  none configured")
    for slot in config.availability.slots:
        crosses = " (overnight)" if slot.crosses_midnight else ""
        lines.append(f"  {days[slot.day]} {slot.start:%H:%M}-{slot.end:%H:%M}{crosses}")
    if config.availability.excluded_dates:
        lines.append("Excluded: " + ", ".join(str(d) for d in config.availability.excluded_dates))
    return "\n".join(lines)


def _build_notifier(config: UserConfig, store: Store, http: httpx.AsyncClient):
    """Telegram when a bot token is present, console otherwise.

    Falling back to console rather than failing keeps the agent monitoring even
    before the chat transport is set up.
    """
    try:
        token = secrets.get(config.name, "telegram_token")
    except secrets.SecretsUnavailable as exc:
        # Same principle as a missing token: keep monitoring on the console
        # rather than taking the agent down over its notification transport.
        print(f"Could not read the stored Telegram token ({scrub(exc)}) - using console output.")
        return ConsoleNotifier(tz=config.availability.tz)

    if not token:
        print("No Telegram token stored - using console output. "
              "Run 'shift-agent set-token' then 'shift-agent link' to enable Telegram.")
        return ConsoleNotifier(tz=config.availability.tz)

    notifier = TelegramNotifier(
        token,
        store,
        http=http,
        link_code=secrets.get(config.name, "telegram_link_code"),
        chat_id=config.notify.telegram_chat_id,
        tz=config.availability.tz,
        user=config.name,
    )
    if notifier.linked_chat_id is None:
        print("Telegram token found but no chat linked yet. Run 'shift-agent link'.")
    return notifier


def _build_chat_hub(config: UserConfig, store: Store, notifier):
    """Wire the chat surface, or explain why it stayed off.

    Needs the same vision/chat API key the friction toolkit uses - one key for
    everything that talks to a model, rather than a second thing to set up.
    Returns None when it is not configured, and the dashboard tab then renders
    an explanation instead of a dead input box.
    """
    from .chat.agent import AnthropicChatClient, ChatAgent
    from .chat.hub import ChatHub

    try:
        key = secrets.get(config.name, "friction_vision_api_key")
    except secrets.SecretsUnavailable as exc:
        print(f"Chat is off: could not read the secret store ({scrub(exc)}).")
        return None

    if not key:
        print(
            "No API key stored, so chat is off. Enable it with:\n"
            f"  shift-agent friction-set-vision-key --user {config.name}"
        )
        return None

    try:
        client = AnthropicChatClient(key)
    except Exception as exc:
        # `AnthropicChatClient.__init__` imports `anthropic` and constructs a
        # real client, so it can fail on a broken install or an SDK version
        # bump. Non-fatal by design: monitoring is the product and chat is the
        # convenience, which is the promise docs/SECURITY.md's failure-mode
        # table makes. Without this the agent stops polling over a chat panel.
        print(f"Chat is off: could not start the model client ({scrub(exc)}).")
        return None

    agent = ChatAgent(config, store, client)
    hub = ChatHub(config, store, agent, notifier=notifier)
    if isinstance(notifier, TelegramNotifier):
        notifier.hub = hub
    return hub


async def _run(args: argparse.Namespace) -> int:
    config = UserConfig.load(args.config)
    if args.dry_run:
        config = config.model_copy(update={"dry_run": True})

    if config.portal.adapter not in available_adapters():
        print(
            f"Unknown adapter {config.portal.adapter!r}. "
            f"Available: {', '.join(available_adapters())}",
            file=sys.stderr,
        )
        return 2

    store = Store(args.state)
    store.set("schedule_summary", _schedule_summary(config))
    try:
        creds = secrets.load_portal_secrets(config.name)
    except secrets.SecretsUnavailable as exc:
        # Reached before the scrubbed handler below, so it needs its own. A
        # headless server with an unwritable data directory lands here.
        store.close()
        print(f"\nStopped: could not open the secret store ({scrub(exc)}).", file=sys.stderr)
        print("Check that you own ~/.shift-agent and can write to it. See docs/VPS.md.",
              file=sys.stderr)
        return 1

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        notifier = _build_notifier(config, store, http)
        if isinstance(notifier, TelegramNotifier):
            notifier.start()

        dashboard_dir = paths.dashboard_dir(config.name)
        server = None
        chat_url = None
        if args.dashboard:
            from .dashboard.server import DashboardServer

            hub = _build_chat_hub(config, store, notifier)
            # The server's handler threads hop coroutines back onto this loop -
            # store and Telegram client both belong to it.
            server = DashboardServer(
                dashboard_dir, hub=hub, loop=asyncio.get_running_loop()
            )
            server.start()
            chat_url = server.chat_url() if hub else None
            # Built before the first poll cycle finishes. The URL is printed
            # right here and she will open it immediately; without this she gets
            # whatever the last run left behind, or a chat panel claiming the
            # agent is not running while it is.
            from .dashboard import try_build_dashboard

            try_build_dashboard(store, config, dashboard_dir, chat_url)
            print(f"Dashboard: {server.url}")

        adapter = get_adapter(config.portal.adapter)(config, http, creds)
        poller = Poller(
            config, adapter, notifier, store,
            dashboard_dir=dashboard_dir,
            chat_url=chat_url,
        )
        mode = "DRY RUN" if config.dry_run else "LIVE"
        print(f"shift-agent starting for {config.name} [{mode}, claim_mode={config.claim_mode.value}]")
        try:
            await adapter.start()
            if args.once:
                print(await poller.run_once())
            else:
                await poller.run_forever()
        except KeyboardInterrupt:
            print("\nstopped")
        except Exception as exc:
            # A shipped app must never show a non-technical user a traceback.
            # Scrubbed because portal errors routinely carry session tokens.
            print(f"\nStopped: {scrub(exc)}", file=sys.stderr)
            print("Run with -v for the full technical detail.", file=sys.stderr)
            logging.getLogger(__name__).debug("fatal error", exc_info=True)
            return 1
        finally:
            if server is not None:
                server.stop()
            await notifier.close()
            await adapter.close()
            store.close()
    return 0


async def _dashboard(args: argparse.Namespace) -> int:
    from .dashboard import build_dashboard

    config = UserConfig.load(args.config)
    outdir = args.out or paths.dashboard_dir(config.name)
    store = Store(args.state or paths.state_db(config.name))
    try:
        index = build_dashboard(store, config, outdir)
    finally:
        store.close()

    print(f"Dashboard written to: {index}")
    if args.open:
        _open_window(index)
    return 0


def _open_window(index: Path) -> None:
    """Serve the dashboard on loopback and show it as an app window.

    Served over HTTP rather than opened as a file: browsers block the clipboard
    API on file:// origins, so copy-as-markdown would silently do nothing, and
    calendar apps cannot subscribe to a file path.
    """
    from .dashboard.server import DashboardServer

    server = DashboardServer(index.parent)
    url = server.start()
    print(f"Serving at {url}")
    try:
        _show(url)
    finally:
        server.stop()


def _show(url: str) -> None:
    """pywebview first; Edge app mode as the fallback.

    The fallback exists because the packaging spike showed pywebview bundles are
    the most fragile part of the build, and a dashboard that cannot open at all
    is worse than one that opens in a slightly different frame.
    """
    try:
        import webview

        webview.create_window("Shift agent", url, width=1180, height=820)
        webview.start()
        return
    except Exception as exc:
        log = logging.getLogger(__name__)
        log.warning("pywebview unavailable (%s); falling back to Edge app mode", exc)

    import shutil
    import subprocess

    for candidate in ("msedge", "chrome"):
        exe = shutil.which(candidate)
        if exe:
            subprocess.Popen([exe, f"--app={url}"])
            return

    import webbrowser

    webbrowser.open(url)


async def _recon(args: argparse.Namespace) -> int:
    from .recon import run_capture

    await run_capture(
        args.url,
        args.out,
        headless=args.headless,
        auto_seconds=args.auto_seconds,
    )
    print(f"\nCapture written to: {args.out.resolve()}")
    return 0


async def _set_token(args: argparse.Namespace) -> int:
    """Store a Telegram bot token in the OS keychain.

    Read via getpass so the token never appears in shell history or in the
    process list, and never passes through a command-line argument.
    """
    import getpass

    token = getpass.getpass("Telegram bot token (from @BotFather, input hidden): ").strip()
    if not token:
        print("No token entered.", file=sys.stderr)
        return 1
    secrets.put(args.user, "telegram_token", token)
    print(f"Stored for user {args.user!r}. Next: shift-agent link --user {args.user}")
    return 0


async def _link(args: argparse.Namespace) -> int:
    import secrets as _stdlib_secrets

    if not secrets.get(args.user, "telegram_token"):
        print("No bot token stored yet. Run 'shift-agent set-token' first.", file=sys.stderr)
        return 1

    code = _stdlib_secrets.token_urlsafe(6)
    secrets.put(args.user, "telegram_link_code", code)
    print(
        "\nOpen Telegram, find your bot, and send exactly:\n\n"
        f"    /start {code}\n\n"
        "The code works once. Until it is used, the bot will accept no commands\n"
        "from anyone - this is what stops a stranger who finds the bot from\n"
        "pausing the agent or confirming claims.\n"
    )
    return 0


async def _friction_bench(args: argparse.Namespace) -> int:
    """Benchmark the vision-model action loop against a public reCAPTCHA demo.

    Never touches a real portal adapter or FLICA's login - see
    docs/FRICTION_TOOLKIT.md for the boundary this respects.
    """
    from .friction.bench_recaptcha import run_benchmark

    try:
        result = await run_benchmark(args.user, headless=not args.headed, timeout_s=args.timeout_s)
    except Exception as exc:
        # Same contract as `_run`: a shipped app never shows a traceback, and
        # the text is scrubbed because a failing browser or API call carries
        # URLs and keys in its message.
        print(f"\nBenchmark stopped: {scrub(exc)}", file=sys.stderr)
        print("Run with -v for the full technical detail.", file=sys.stderr)
        logging.getLogger(__name__).debug("friction-bench failed", exc_info=True)
        return 1

    status = "PASS" if result.success else "FAIL"
    print(f"{status} in {result.elapsed_s:.1f}s ({result.steps} steps){': ' + result.detail if result.detail else ''}")
    return 0 if result.success else 1


async def _friction_otp(args: argparse.Namespace) -> int:
    from .friction.cli import fetch_otp

    try:
        return await fetch_otp(args.user, timeout_s=args.timeout_s)
    except Exception as exc:
        print(f"\nCould not read the mailbox: {scrub(exc)}", file=sys.stderr)
        print("Run with -v for the full technical detail.", file=sys.stderr)
        logging.getLogger(__name__).debug("friction-otp failed", exc_info=True)
        return 1


async def _friction_set_vision_key(args: argparse.Namespace) -> int:
    from .friction.cli import set_vision_key

    return await set_vision_key(args.user)


async def _friction_set_imap_password(args: argparse.Namespace) -> int:
    from .friction.cli import set_imap_password

    return await set_imap_password(args.user)


async def _demo(args: argparse.Namespace) -> int:
    """Run the full pipeline against fabricated data.

    Shifts are generated relative to now so the demo never goes stale, and it
    needs no config file, no credentials, and no network.
    """
    now = datetime.now(UTC)
    config = UserConfig.model_validate(
        {
            "name": "demo",
            "portal": {"adapter": "mock"},
            "availability": {
                "timezone": "America/New_York",
                "slots": [
                    {"day": d, "start": "06:00", "end": "22:00"}
                    for d in ("Monday", "Tuesday", "Wednesday", "Thursday",
                              "Friday", "Saturday", "Sunday")
                ],
            },
            "rules": {"min_rest_hours": 8},
            "claim_mode": "confirm",
            "dry_run": True,
        }
    )

    def at(days: int, hour: int, length: int, sid: str, title: str) -> Shift:
        local = (now + timedelta(days=days)).astimezone(config.availability.tz)
        start = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        return Shift(id=sid, start=start, end=start + timedelta(hours=length), title=title)

    offered = [
        at(2, 9, 6, "OK-1", "Day trip - matches availability"),
        at(3, 23, 5, "LATE", "Overnight - outside 06:00-22:00 window"),
        at(4, 8, 4, "CLASH", "Conflicts with an assigned shift"),
    ]
    assigned = [at(4, 10, 6, "ASSIGNED", "Already rostered")]

    adapter = _mock.MockAdapter(config, open_shifts=offered, assigned=assigned)
    notifier = ConsoleNotifier(tz=config.availability.tz, auto_confirm=True)
    store = Store(args.state)
    poller = Poller(config, adapter, notifier, store)

    print(f"\nEvaluating {len(offered)} offered shifts against a 06:00-22:00 window...\n")
    report = await poller.run_once()
    print(f"\n  evaluated : {report.evaluated}")
    print(f"  matched   : {report.matched}")
    print(f"  offered   : {report.offered}")
    print(f"  claimed   : {report.claimed} (dry run - nothing sent to any portal)")
    print(f"  verdicts  : {report.verdicts}")
    print(f"  portal claim calls: {adapter.claim_calls or 'none'}\n")
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shift-agent")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="SQLite state file")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="poll a configured portal")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--once", action="store_true", help="single cycle, then exit")
    run.add_argument("--dry-run", action="store_true", help="never send a claim request")
    run.add_argument(
        "--dashboard", action="store_true",
        help="serve the live dashboard on loopback, with the chat panel enabled",
    )
    run.set_defaults(func=_run)

    demo = sub.add_parser("demo", help="run the pipeline on fabricated data")
    demo.set_defaults(func=_demo)

    dash = sub.add_parser("dashboard", help="build the dashboard for a profile")
    dash.add_argument("--config", type=Path, required=True)
    dash.add_argument("--out", type=Path, default=None, help="defaults to the profile's folder")
    dash.add_argument("--open", action="store_true", help="open it as an app window")
    dash.set_defaults(func=_dashboard)

    recon = sub.add_parser("recon", help="one-shot portal capture (run with the account holder)")
    recon.add_argument("--url", required=True, help="portal login URL")
    recon.add_argument("--out", type=Path, required=True, help="output folder (keep OUTSIDE the repo)")
    recon.add_argument("--headless", action="store_true", help="rehearsal only")
    recon.add_argument("--auto-seconds", type=float, default=None,
                       help="rehearsal: capture unattended for N seconds instead of waiting for ENTER")
    recon.set_defaults(func=_recon)

    token = sub.add_parser("set-token", help="store a Telegram bot token in the OS keychain")
    token.add_argument("--user", default="default")
    token.set_defaults(func=_set_token)

    link = sub.add_parser("link", help="generate a one-time code to link a Telegram chat")
    link.add_argument("--user", default="default")
    link.set_defaults(func=_link)

    fbench = sub.add_parser(
        "friction-bench",
        help="benchmark the vision-model action loop against a public reCAPTCHA demo page",
    )
    fbench.add_argument("--user", default="default")
    fbench.add_argument(
        "--headed", action="store_true",
        help="show the browser window (debugging only - no human input is required)",
    )
    fbench.add_argument("--timeout-s", type=float, default=300.0)
    fbench.set_defaults(func=_friction_bench)

    fvkey = sub.add_parser(
        "friction-set-vision-key", help="store the vision-model API key in the OS keychain"
    )
    fvkey.add_argument("--user", default="default")
    fvkey.set_defaults(func=_friction_set_vision_key)

    fimap = sub.add_parser(
        "friction-set-imap-password", help="store an IMAP app password in the OS keychain"
    )
    fimap.add_argument("--user", default="default")
    fimap.set_defaults(func=_friction_set_imap_password)

    fotp = sub.add_parser(
        "friction-otp", help="wait for a one-time code to arrive by email and print it"
    )
    fotp.add_argument("--user", default="default")
    fotp.add_argument("--timeout-s", type=float, default=120.0)
    fotp.set_defaults(func=_friction_otp)

    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    _configure_browsers()

    if getattr(args, "command", None) == "run" and not browsers_available():
        print(
            "The browser files are missing.\n\n"
            "If you unzipped the download, make sure the 'browsers' folder is\n"
            "still next to ShiftAgent.exe - moving the .exe on its own breaks it.\n"
            "Re-extract the whole zip and try again.",
            file=sys.stderr,
        )
        return 1

    return asyncio.run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
