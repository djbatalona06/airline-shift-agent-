"""Dashboard data, ICS output, rendering, and profile isolation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shift_agent import paths
from shift_agent.config import UserConfig
from shift_agent.dashboard import PLACEHOLDER, build_dashboard, render, try_build_dashboard
from shift_agent.dashboard.data import build_payload
from shift_agent.dashboard.ical import (
    build_calendar,
    build_markdown,
    escape_text,
    fold,
    make_uid,
)
from shift_agent.models import ClaimOutcome, ClaimResult, MatchResult, MatchVerdict, Shift
from shift_agent.store import Store

ALL_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def make_config(name="tester", **over) -> UserConfig:
    base = {
        "name": name,
        "portal": {"adapter": "mock"},
        "availability": {
            "timezone": "America/New_York",
            "slots": [{"day": d, "start": "00:00", "end": "00:00"} for d in ALL_DAYS],
        },
        "rules": {"min_rest_hours": 0},
        "grades": {"pursue": ["E", "D", "B"], "notify_only": ["A", "C"]},
        "dry_run": False,
    }
    base.update(over)
    return UserConfig.model_validate(base)


def seeded_store(tmp_path, user="tester") -> Store:
    store = Store(tmp_path / "state.db")
    start = datetime.now(UTC) + timedelta(days=1)

    def shift(sid, title, hours=6):
        return Shift(id=sid, start=start, end=start + timedelta(hours=hours), title=title)

    store.record_seen(user, MatchResult(shift("S1", "ORD-DFW"), MatchVerdict.MATCH))
    store.record_seen(
        user,
        MatchResult(shift("S2", "DFW-LAX"), MatchVerdict.GRADE_NOTIFY_ONLY, "grade A - alerting you"),
    )
    store.record_seen(
        user, MatchResult(shift("S3", "LAX-SEA"), MatchVerdict.OUTSIDE_AVAILABILITY, "outside window")
    )
    store.record_claim(user, "S1", ClaimResult(ClaimOutcome.CLAIMED, "ok"), dry_run=False)
    return store


# --- payload -----------------------------------------------------------------

def test_payload_has_expected_shape(tmp_path):
    payload = build_payload(seeded_store(tmp_path), make_config())
    assert payload["metrics"]["seen"] == 3
    assert payload["metrics"]["claimed"] == 1
    assert payload["metrics"]["alerts"] == 1
    assert len(payload["shifts"]) == 3


def test_payload_contains_no_secrets(tmp_path):
    """The natural next step after building a page is emailing it."""
    store = seeded_store(tmp_path)
    store.set("telegram_linked_chat_id", 4242)
    blob = str(build_payload(store, make_config())).lower()
    for forbidden in ("password", "token", "cookie", "secret", "credential"):
        assert forbidden not in blob


def test_dry_run_claims_are_labelled_distinctly(tmp_path):
    store = seeded_store(tmp_path)
    store.record_claim("tester", "S2", ClaimResult(ClaimOutcome.CLAIMED, "sim"), dry_run=True)
    payload = build_payload(store, make_config())

    real = next(s for s in payload["shifts"] if s["id"] == "S1")
    dry = next(s for s in payload["shifts"] if s["id"] == "S2")
    assert real["claimed"] and not real["dry_run"]
    assert dry["claimed"] and dry["dry_run"]
    assert payload["metrics"]["claimed"] == 1        # dry runs excluded
    assert payload["metrics"]["dry_run_claims"] == 1


def test_grade_is_recovered_for_display(tmp_path):
    payload = build_payload(seeded_store(tmp_path), make_config())
    alert = next(s for s in payload["shifts"] if s["id"] == "S2")
    assert alert["grade"] == "A"


def test_every_verdict_has_a_label():
    """A verdict added to the enum without a label must not break the page."""
    from shift_agent.dashboard.data import _label

    for verdict in MatchVerdict:
        assert _label(verdict.value)


def test_unknown_verdict_degrades_instead_of_raising():
    from shift_agent.dashboard.data import _label

    assert _label("something_invented_later") == "Something invented later"


def test_verdict_breakdown_counts_every_shift(tmp_path):
    payload = build_payload(seeded_store(tmp_path), make_config())
    assert sum(v["count"] for v in payload["verdicts"]) == 3


def test_payload_isolated_per_user(tmp_path):
    store = seeded_store(tmp_path, user="aunt")
    store.record_seen(
        "uncle",
        MatchResult(
            Shift(id="U1", start=datetime.now(UTC) + timedelta(days=1),
                  end=datetime.now(UTC) + timedelta(days=1, hours=4), title="Uncle trip"),
            MatchVerdict.MATCH,
        ),
    )
    aunt = build_payload(store, make_config("aunt"))
    uncle = build_payload(store, make_config("uncle"))

    assert [s["id"] for s in aunt["shifts"]] == ["S1", "S2", "S3"]
    assert [s["id"] for s in uncle["shifts"]] == ["U1"]


# --- ICS ---------------------------------------------------------------------

def test_escape_handles_separators_and_newlines():
    assert escape_text("a,b") == "a\\,b"
    assert escape_text("a;b") == "a\\;b"
    assert escape_text("a\nb") == "a\\nb"
    assert escape_text("a\\b") == "a\\\\b"


def test_backslash_escaped_first_not_double_escaped():
    assert escape_text("a\\,b") == "a\\\\\\,b"


def test_fold_keeps_lines_within_75_octets():
    folded = fold("SUMMARY:" + "x" * 300)
    for line in folded.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75


def test_fold_does_not_split_multibyte_characters():
    folded = fold("SUMMARY:" + "é" * 120)
    assert "é" in folded
    for line in folded.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75


def test_uid_is_stable_across_rebuilds():
    assert make_uid("ABC-123") == make_uid("ABC-123")


def test_uid_is_sanitised():
    assert " " not in make_uid("A B/C")


def test_calendar_round_trips_times_and_escaping(tmp_path):
    payload = build_payload(seeded_store(tmp_path), make_config())
    shift = dict(payload["shifts"][0])
    shift["title"] = "Trip, with comma\nand newline"

    ics = build_calendar([shift])
    assert ics.startswith("BEGIN:VCALENDAR\r\n")
    assert ics.endswith("END:VCALENDAR\r\n")
    assert "\r\n" in ics

    body = ics.replace("\r\n ", "")     # unfold before searching
    assert "\\," in body and "\\n" in body
    assert "\nSUMMARY:Trip, with comma" not in body        # raw comma must not survive

    stamps = [ln for ln in body.split("\r\n") if ln.startswith("DTSTART:")]
    assert stamps and stamps[0].endswith("Z")


def test_calendar_uid_unchanged_between_two_builds(tmp_path):
    payload = build_payload(seeded_store(tmp_path), make_config())
    first = build_calendar(payload["shifts"])
    second = build_calendar(payload["shifts"])
    uids = lambda t: [l for l in t.split("\r\n") if l.startswith("UID:")]
    assert uids(first) == uids(second)
    assert uids(first), "expected at least one event"


def test_calendar_skips_shifts_without_times():
    ics = build_calendar([{"id": "X", "title": "no times"}])
    assert "BEGIN:VEVENT" not in ics


def test_dry_run_marked_in_calendar_summary(tmp_path):
    payload = build_payload(seeded_store(tmp_path), make_config())
    shift = dict(payload["shifts"][0])
    shift["dry_run"] = True
    assert "DRY RUN" in build_calendar([shift]).replace("\r\n ", "")


def test_markdown_escapes_pipes(tmp_path):
    payload = build_payload(seeded_store(tmp_path), make_config())
    shift = dict(payload["shifts"][0])
    shift["title"] = "A|B"
    assert "A\\|B" in build_markdown([shift])


# --- rendering ---------------------------------------------------------------

def test_render_replaces_placeholder():
    html = render({"hello": "world"}, template=f"<script>{PLACEHOLDER}</script>")
    assert PLACEHOLDER not in html
    assert '"hello"' in html


def test_render_escapes_closing_script_tag():
    """A shift title containing </script> would otherwise break the page."""
    html = render({"t": "</script><img onerror=x>"}, template=f"<script>{PLACEHOLDER}</script>")
    assert "</script><img" not in html
    assert "<\\/script>" in html


def test_render_rejects_template_without_placeholder():
    with pytest.raises(ValueError, match="placeholder"):
        render({}, template="<html>no slot</html>")


def test_build_writes_all_three_files(tmp_path):
    out = tmp_path / "out"
    index = build_dashboard(seeded_store(tmp_path), make_config(), out)

    assert index.is_file()
    assert (out / "shifts.ics").is_file()
    assert (out / "shifts.md").is_file()

    html = index.read_text(encoding="utf-8")
    assert PLACEHOLDER not in html          # injection actually happened
    assert "ORD-DFW" in html


def test_build_leaves_no_temp_files(tmp_path):
    out = tmp_path / "out"
    build_dashboard(seeded_store(tmp_path), make_config(), out)
    assert not [p for p in out.iterdir() if p.name.startswith(".")]


def test_try_build_swallows_failure_so_polling_continues(tmp_path, monkeypatch):
    import shift_agent.dashboard as dash

    monkeypatch.setattr(dash, "build_payload", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert try_build_dashboard(seeded_store(tmp_path), make_config(), tmp_path / "out") is None


# --- profile isolation -------------------------------------------------------

def test_profiles_get_separate_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFT_AGENT_HOME", str(tmp_path))
    assert paths.state_db("aunt") != paths.state_db("uncle")
    assert paths.state_db("aunt").parent.name == "aunt"


def test_profile_names_are_slugified(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFT_AGENT_HOME", str(tmp_path))
    assert paths.profile_dir("Aunt Jenny!").name == "aunt-jenny"


def test_distinct_names_do_not_collide(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFT_AGENT_HOME", str(tmp_path))
    assert paths.profile_dir("aunt").name != paths.profile_dir("uncle").name


def test_unusable_profile_name_is_rejected_not_shared():
    with pytest.raises(ValueError, match="no usable directory"):
        paths.slugify("!!!")


def test_profiles_are_listed(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFT_AGENT_HOME", str(tmp_path))
    paths.profile_dir("aunt")
    paths.profile_dir("uncle")
    assert paths.list_profiles() == ["aunt", "uncle"]
