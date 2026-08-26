"""Last-resort crash reporting for the frozen desktop build.

The setup window (see `setup/`) fixes the one crash this project shipped with
- a bare double-click hitting a required CLI subcommand - but it can't fix
every future one: a hand-edited YAML file, a missing `browsers/` folder, a
corrupt profile. Without this module, any of those still just print to a
console that Windows closes the instant the process exits, exactly the
original "flash and vanish" symptom. `packaging/entry.py` wraps `main()` with
these two functions so a future unknown failure is always readable somewhere,
even with no console at all under a `--windowed` build.

Kept in `src/` rather than `packaging/`, which is not on `pythonpath` (see
`pyproject.toml`) and so cannot be exercised by the test suite.
"""

from __future__ import annotations

import traceback
from datetime import UTC, datetime
from pathlib import Path

from . import paths


def write_crash_log(exc: BaseException) -> Path | None:
    """Append a timestamped traceback to the crash log. Best-effort: never
    raises, so a failure writing the log can never mask the original crash.
    Returns the log path, or None if it could not be written."""
    try:
        log_dir = paths.app_root() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "crash.log"
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n----- {stamp} -----\n{text}")
        return path
    except Exception:
        return None


def format_crash_message(exc: BaseException, log_path: Path | None) -> str:
    """Short, human-readable summary - what a message box or a printed line
    should say, never the full traceback (that's what the log file is for)."""
    where = f"\n\nDetails were written to:\n{log_path}" if log_path else ""
    return (
        "Shift agent hit a problem and could not continue.\n\n"
        f"{type(exc).__name__}: {exc}"
        f"{where}"
    )
