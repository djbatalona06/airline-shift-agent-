"""The first-run setup/picker: profile validation, YAML round-trip, and the
loopback routes that expose it to setup/index.html.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
import yaml

from shift_agent import paths
from shift_agent.config import UserConfig
from shift_agent.dashboard.server import DashboardServer
from shift_agent.setup.api import SetupAPI


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFT_AGENT_HOME", str(tmp_path / "home"))


# --- SetupAPI, no server involved -------------------------------------------


def test_valid_profile_round_trips_through_the_written_file():
    api = SetupAPI()
    result = api.save_profile(
        {
            "name": "Jane Doe",
            "adapter": "mock",
            "timezone": "America/New_York",
            "home_base": "mco",
            "grades_pursue": "e, d, b",
            "grades_notify_only": "a,c",
            "min_rest_hours": 8,
            "claim_mode": "confirm",
            "dry_run": True,
        }
    )
    assert result == {"ok": True, "profile": "jane-doe"}

    config = UserConfig.load(paths.config_path("jane-doe", create=False))
    assert config.name == "Jane Doe"
    assert config.portal.adapter == "mock"
    assert config.availability.timezone == "America/New_York"
    assert config.home_base.code == "MCO"
    assert config.grades.pursue == ("E", "D", "B")
    assert config.grades.notify_only == ("A", "C")
    assert config.rules.min_rest_hours == 8
    assert config.dry_run is True
    assert len(config.availability.slots) == 7


def test_saved_time_fields_round_trip_as_strings_not_sexagesimal_ints():
    """PyYAML's default resolver treats an unquoted HH:MM:SS-shaped string as
    sexagesimal - this pins that the written file reads back as plain
    strings, not silently-wrong integers."""
    api = SetupAPI()
    api.save_profile({"name": "Jane", "timezone": "UTC"})

    raw = paths.config_path("jane", create=False).read_text(encoding="utf-8")
    first_slot = yaml.safe_load(raw)["availability"]["slots"][0]
    assert first_slot["start"] == "06:00:00"
    assert isinstance(first_slot["start"], str)
    assert first_slot["end"] == "22:00:00"
    assert isinstance(first_slot["end"], str)


def test_default_availability_covers_every_day():
    api = SetupAPI()
    api.save_profile({"name": "Jane", "timezone": "UTC"})
    config = UserConfig.load(paths.config_path("jane", create=False))
    assert sorted(slot.day for slot in config.availability.slots) == list(range(7))


def test_bad_timezone_returns_a_field_error_and_writes_nothing():
    api = SetupAPI()
    result = api.save_profile({"name": "Bad TZ", "timezone": "Not/AZone"})

    assert result["ok"] is False
    assert any("timezone" in e["field"] for e in result["errors"])
    assert not paths.config_path("bad-tz", create=False).is_file()
    json.dumps(result)  # must be JSON-safe - pydantic's raw errors() are not


def test_empty_name_reports_an_error_rather_than_raising():
    api = SetupAPI()
    result = api.save_profile({"name": "", "timezone": "UTC"})

    assert result["ok"] is False
    assert result["errors"]
    json.dumps(result)


def test_list_profiles_reflects_saved_profiles():
    api = SetupAPI()
    api.save_profile({"name": "Alice", "adapter": "mock", "timezone": "UTC"})
    api.save_profile({"name": "Bob", "adapter": "flica", "timezone": "America/Chicago"})

    profiles = {p["id"]: p for p in api.list_profiles()}
    assert profiles.keys() == {"alice", "bob"}
    assert profiles["alice"]["adapter"] == "mock"
    assert profiles["bob"]["timezone"] == "America/Chicago"


def test_list_profiles_surfaces_a_broken_profile_instead_of_dropping_it():
    path = paths.config_path("broken")
    path.write_text("not: [valid, config", encoding="utf-8")

    profiles = SetupAPI().list_profiles()
    assert len(profiles) == 1
    assert profiles[0]["id"] == "broken"
    assert "error" in profiles[0]


def test_choose_rejects_an_unknown_profile():
    api = SetupAPI()
    result = api.choose("does-not-exist")
    assert result == {"ok": False, "error": "unknown profile"}
    assert api.chosen is None
    assert not api.done_event.is_set()


def test_choose_accepts_a_known_profile_and_signals_done():
    api = SetupAPI()
    api.save_profile({"name": "Jane", "timezone": "UTC"})
    api.done_event.clear()  # save_profile already sets it; isolate this assertion

    result = api.choose("jane")
    assert result == {"ok": True, "profile": "jane"}
    assert api.chosen == "jane"
    assert api.done_event.is_set()


# --- the loopback routes, same pattern as test_chat_api.py ------------------


@pytest.fixture
def served(tmp_path):
    (tmp_path / "index.html").write_text("<h1>setup</h1>", encoding="utf-8")
    server = DashboardServer(tmp_path, setup_api=SetupAPI())
    server.start()
    yield server
    server.stop()


def post(url: str, payload: dict, headers: dict | None = None):
    """Sends what setup/index.html sends. A None value removes a header.

    Mirrors test_chat_api.py's helper of the same name - the setup routes
    are gated by the same _origin_ok() check the chat routes are.
    """
    sent = {"Content-Type": "application/json", "X-Shift-Agent": "1"}
    sent.update(headers or {})
    sent = {k: v for k, v in sent.items() if v is not None}
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=sent)
    return urllib.request.urlopen(request, timeout=10)


def get_json(url: str):
    return json.loads(urllib.request.urlopen(url, timeout=10).read().decode())


def test_profiles_route_starts_empty(served):
    assert get_json(f"{served.url}api/setup/profiles") == {"profiles": []}


def test_save_route_then_profiles_route_reflects_it(served):
    result = json.loads(
        post(f"{served.url}api/setup/save", {"name": "Jane", "timezone": "UTC"}).read()
    )
    assert result == {"ok": True, "profile": "jane"}

    profiles = get_json(f"{served.url}api/setup/profiles")["profiles"]
    assert profiles[0]["id"] == "jane"


def test_save_route_reports_validation_errors_as_200_with_ok_false(served):
    result = json.loads(
        post(f"{served.url}api/setup/save", {"name": "Bad", "timezone": "nope"}).read()
    )
    assert result["ok"] is False


def test_choose_route(served):
    post(f"{served.url}api/setup/save", {"name": "Jane", "timezone": "UTC"})
    result = json.loads(
        post(f"{served.url}api/setup/choose", {"profile_id": "jane"}).read()
    )
    assert result == {"ok": True, "profile": "jane"}


def test_setup_routes_are_absent_without_the_token(served):
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"http://127.0.0.1:{served.port}/api/setup/profiles", timeout=5)
    assert exc.value.code == 404


def test_save_route_without_the_custom_header_is_refused(served):
    """Regression pin: do_POST dispatches across chat/setup routes, and it is
    easy for a restructure to drop the shared _origin_ok() gate ahead of that
    dispatch without any single route's own code changing at all."""
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(f"{served.url}api/setup/save", {"name": "Jane", "timezone": "UTC"},
             headers={"X-Shift-Agent": None})
    assert exc.value.code == 404
    assert not paths.config_path("jane", create=False).is_file()


def test_choose_route_without_the_custom_header_is_refused(served):
    post(f"{served.url}api/setup/save", {"name": "Jane", "timezone": "UTC"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(f"{served.url}api/setup/choose", {"profile_id": "jane"},
             headers={"X-Shift-Agent": None})
    assert exc.value.code == 404


def test_setup_routes_are_absent_when_no_setup_api_is_wired(tmp_path):
    (tmp_path / "index.html").write_text("<h1>dashboard</h1>", encoding="utf-8")
    server = DashboardServer(tmp_path)
    server.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{server.url}api/setup/profiles", timeout=5)
        assert exc.value.code == 404
    finally:
        server.stop()
