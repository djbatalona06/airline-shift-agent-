"""The zero-argument (double-click) dispatch that used to be a bare argparse
crash - see setup/ and docs/INSTALL.md's Troubleshooting section for why this
exists. Every test here monkeypatches the two side-effecting seams
(`main._open_window` and `shift_agent.setup.open_setup_window`) so nothing
touches real pywebview or a browser.
"""

from __future__ import annotations

import pytest

from shift_agent import main as m
from shift_agent import paths
from shift_agent.setup.api import SetupAPI


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFT_AGENT_HOME", str(tmp_path / "home"))


@pytest.fixture
def no_real_windows(monkeypatch):
    """Records what would have opened, instead of opening it."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(m, "_open_window", lambda index: calls.append(("dashboard", str(index))))
    monkeypatch.setattr(
        "shift_agent.setup.open_setup_window", lambda: calls.append(("setup", "")) and None
    )
    return calls


def _save(name: str, **over) -> str:
    api = SetupAPI()
    result = api.save_profile({"name": name, "adapter": "mock", "timezone": "UTC", **over})
    assert result["ok"], result
    return result["profile"]


def test_zero_profiles_opens_setup(no_real_windows):
    assert m.main([]) == 0
    assert no_real_windows == [("setup", "")]


def test_one_profile_opens_its_dashboard_directly(no_real_windows):
    _save("Solo")

    assert m.main([]) == 0
    assert len(no_real_windows) == 1
    kind, index = no_real_windows[0]
    assert kind == "dashboard"
    assert str(paths.dashboard_dir("solo")) in index


def test_one_profile_uses_its_own_state_db_not_the_legacy_default(no_real_windows):
    """Regression pin: a synthetic dashboard-args Namespace that leaves
    `state` unset would silently reuse the shared ~/.shift-agent/state.db
    instead of this profile's own poll history."""
    _save("Solo")

    m.main([])

    assert paths.state_db("solo").is_file()
    assert not m.DEFAULT_STATE.exists()


def test_more_than_one_profile_opens_setup_not_a_dashboard(no_real_windows):
    _save("Alice")
    _save("Bob")

    assert m.main([]) == 0
    assert no_real_windows == [("setup", "")]


def test_a_profile_directory_with_no_config_is_not_counted(no_real_windows):
    """paths.list_profiles() lists directories, not validated profiles."""
    paths.profile_dir("stray")  # creates the directory, no config.yaml inside
    _save("Solo")

    m.main([])

    assert no_real_windows == [("dashboard", str(paths.dashboard_dir("solo") / "index.html"))]


def test_setup_subcommand_reaches_the_setup_window(no_real_windows):
    assert m.main(["setup"]) == 0
    assert no_real_windows == [("setup", "")]


def test_setup_window_choosing_a_profile_opens_its_dashboard(monkeypatch):
    """_setup(): once the window resolves to a profile, its dashboard opens -
    not just the fact that *a* window opened."""
    _save("Solo")
    calls = []
    monkeypatch.setattr(m, "_open_window", lambda index: calls.append(str(index)))
    monkeypatch.setattr("shift_agent.setup.open_setup_window", lambda: "solo")

    assert m.main(["setup"]) == 0
    assert calls == [str(paths.dashboard_dir("solo") / "index.html")]


def test_setup_window_closed_without_choosing_does_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_open_window", lambda index: calls.append(str(index)))
    monkeypatch.setattr("shift_agent.setup.open_setup_window", lambda: None)

    assert m.main(["setup"]) == 0
    assert calls == []
