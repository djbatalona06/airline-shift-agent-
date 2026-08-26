"""PyInstaller entry point.

A module cannot be bundled with `-m`, so the frozen build needs a real script.
Importing `main` here also gives PyInstaller a static reference to the adapter
registrations in `shift_agent.main`, which the decorator-based registry would
otherwise hide from its analysis.
"""

from __future__ import annotations

import multiprocessing
import sys

from shift_agent.main import main

if __name__ == "__main__":
    # Required in frozen builds: without it, any child process re-executes the
    # bundle from the top and forks endlessly.
    multiprocessing.freeze_support()
    try:
        sys.exit(main())
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:
        # Belt-and-suspenders beneath the setup window: any *other* unhandled
        # startup failure must still be readable, not just flash and vanish
        # in a console that Windows closes the instant this process exits -
        # and under the release build's --windowed mode there is no console
        # at all to print to. See shift_agent/crashlog.py.
        from shift_agent.crashlog import format_crash_message, write_crash_log

        message = format_crash_message(exc, write_crash_log(exc))
        if sys.platform == "win32" and getattr(sys, "frozen", False):
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "Shift agent — error", 0x10)
        else:
            print(message, file=sys.stderr)
        sys.exit(1)
