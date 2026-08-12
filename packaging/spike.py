"""Phase 0 packaging spike — throwaway, not shipped.

Answers one question: can shift-agent be a single portable .exe with a desktop
window? The risky combination is PyInstaller --onefile + pywebview + pythonnet,
and the failure that matters most is `keyring` working in the SOURCE build but
silently failing in the PACKAGED one from missing hidden imports. That failure
would surface as "the agent forgot my password" on her machine, weeks later.

Run `--checks` to verify imports and keyring without opening anything.
Run `--window` to prove WebView2 actually renders.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time

CHECK_KEY = "packaging-spike"
CHECK_VALUE = "round-trip-ok"

PAGE = """<!doctype html><html><body style="font-family:system-ui;padding:2rem">
<h2>WebView2 is rendering</h2>
<p>If you can read this, the bundled desktop window works.</p>
<p id="t"></p><script>document.getElementById('t').textContent =
'JS executed at ' + new Date().toISOString();</script>
</body></html>"""


def run_checks() -> dict:
    out: dict = {
        "frozen": bool(getattr(sys, "frozen", False)),
        "meipass": getattr(sys, "_MEIPASS", None),
        "python": sys.version.split()[0],
        "imports": {},
        "keyring": {},
    }

    for name in ("webview", "keyring", "httpx", "pydantic", "clr_loader"):
        try:
            mod = __import__(name)
            out["imports"][name] = getattr(mod, "__version__", "ok")
        except Exception as exc:
            out["imports"][name] = f"FAILED: {type(exc).__name__}: {exc}"

    # The check that actually matters in a frozen build.
    try:
        import keyring

        backend = type(keyring.get_keyring()).__name__
        out["keyring"]["backend"] = backend
        keyring.set_password("shift-agent-spike", CHECK_KEY, CHECK_VALUE)
        got = keyring.get_password("shift-agent-spike", CHECK_KEY)
        out["keyring"]["round_trip"] = got == CHECK_VALUE
        out["keyring"]["read_back"] = got
        try:
            keyring.delete_password("shift-agent-spike", CHECK_KEY)
            out["keyring"]["cleaned_up"] = True
        except Exception as exc:
            out["keyring"]["cleaned_up"] = f"FAILED: {exc}"
    except Exception as exc:
        out["keyring"]["error"] = f"{type(exc).__name__}: {exc}"

    return out


def open_window(seconds: float) -> None:
    import webview

    window = webview.create_window("shift agent — spike", html=PAGE, width=640, height=420)

    def close_later() -> None:
        time.sleep(seconds)
        try:
            window.destroy()
        except Exception:
            pass

    threading.Thread(target=close_later, daemon=True).start()
    webview.start()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checks", action="store_true")
    parser.add_argument("--window", action="store_true")
    parser.add_argument("--seconds", type=float, default=4.0)
    args = parser.parse_args()

    if args.checks or not args.window:
        print(json.dumps(run_checks(), indent=2))

    if args.window:
        try:
            open_window(args.seconds)
            print(json.dumps({"window": "opened and closed cleanly"}))
        except Exception as exc:
            print(json.dumps({"window": f"FAILED: {type(exc).__name__}: {exc}"}))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
