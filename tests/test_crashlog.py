"""Last-resort crash reporting for the frozen desktop build.

See packaging/entry.py: this is what stands between an unhandled startup
exception and the exact "flash and vanish" symptom the setup window (see
setup/) exists to fix for the far more common no-config case.
"""

from __future__ import annotations

import pytest

from shift_agent import crashlog, paths


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFT_AGENT_HOME", str(tmp_path / "home"))


def _boom() -> Exception:
    try:
        raise ValueError("could not read the browsers folder")
    except ValueError as exc:
        return exc


def test_write_crash_log_records_type_message_and_traceback():
    path = crashlog.write_crash_log(_boom())

    assert path is not None
    assert path == paths.app_root() / "logs" / "crash.log"
    text = path.read_text(encoding="utf-8")
    assert "ValueError" in text
    assert "could not read the browsers folder" in text
    assert "Traceback" in text


def test_write_crash_log_appends_rather_than_overwrites():
    crashlog.write_crash_log(_boom())
    crashlog.write_crash_log(_boom())

    path = paths.app_root() / "logs" / "crash.log"
    assert path.read_text(encoding="utf-8").count("Traceback (most recent call last)") == 2


def test_write_crash_log_never_raises_when_the_directory_is_unusable(tmp_path, monkeypatch):
    # A regular file where a directory needs to go makes mkdir() fail with a
    # real OSError, without reaching for anything more invasive than that.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(paths, "app_root", lambda: blocker / "app_root")

    assert crashlog.write_crash_log(_boom()) is None


def test_format_crash_message_includes_the_log_path_when_available():
    path = paths.app_root() / "logs" / "crash.log"
    message = crashlog.format_crash_message(_boom(), path)

    assert "ValueError" in message
    assert "could not read the browsers folder" in message
    assert str(path) in message
    assert "Traceback" not in message  # summary only; the file has the full trace


def test_format_crash_message_without_a_log_path_still_summarises():
    message = crashlog.format_crash_message(_boom(), None)
    assert "ValueError" in message
