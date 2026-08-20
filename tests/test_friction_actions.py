"""Vision-model action schema. No I/O - pure parsing/validation tests."""

from __future__ import annotations

import pytest

from shift_agent.friction.actions import Action, ActionParseError, ActionType, parse_action


def test_parses_a_plain_json_click():
    action = parse_action('{"type": "click", "x": 10, "y": 20, "reason": "checkbox"}')
    assert action == Action(type=ActionType.CLICK, x=10, y=20, reason="checkbox")


def test_tolerates_prose_and_markdown_fence():
    reply = 'Here is my plan:\n```json\n{"type": "done", "reason": "solved"}\n```\nDone.'
    action = parse_action(reply)
    assert action.type is ActionType.DONE


def test_rejects_reply_with_no_json():
    with pytest.raises(ActionParseError):
        parse_action("I clicked the checkbox.")


def test_rejects_invalid_json():
    with pytest.raises(ActionParseError):
        parse_action("{not json}")


def test_click_requires_coordinates():
    with pytest.raises(Exception):
        Action(type=ActionType.CLICK)


def test_type_requires_text():
    with pytest.raises(Exception):
        Action(type=ActionType.TYPE)


def test_key_requires_key():
    with pytest.raises(Exception):
        Action(type=ActionType.KEY)


def test_wait_done_and_fail_need_nothing_extra():
    Action(type=ActionType.WAIT)
    Action(type=ActionType.DONE)
    Action(type=ActionType.FAIL)


def test_extra_fields_are_rejected():
    with pytest.raises(Exception):
        Action.model_validate({"type": "done", "unexpected": True})


# --- key allow-list ----------------------------------------------------------
# `key` reaches page.keyboard.press(), which interprets its argument as a chord.
# An unconstrained string there is a route to browser-level shortcuts rather
# than to the page the loop is looking at.

def test_allows_the_keys_a_challenge_page_needs():
    for key in ("Enter", "Tab", "Escape", "ArrowDown", "Space", "F5", "a", "7"):
        Action(type=ActionType.KEY, key=key)


def test_allows_shift_chords():
    Action(type=ActionType.KEY, key="Shift+Tab")


@pytest.mark.parametrize("key", ["Control+A", "Meta+W", "Alt+F4", "ControlOrMeta+R"])
def test_rejects_chords_that_reach_the_browser(key):
    with pytest.raises(Exception):
        Action(type=ActionType.KEY, key=key)


def test_rejects_an_unknown_key_name():
    with pytest.raises(Exception):
        Action(type=ActionType.KEY, key="LaunchCalculator")


def test_rejects_a_negative_coordinate():
    with pytest.raises(Exception):
        Action(type=ActionType.CLICK, x=-5, y=10)


def test_rejects_an_absurd_coordinate():
    with pytest.raises(Exception):
        Action(type=ActionType.CLICK, x=10, y=9_999_999)


def test_a_rejected_key_surfaces_as_a_parse_error():
    """The loop catches ActionParseError; a bare ValidationError would escape."""
    with pytest.raises(ActionParseError):
        parse_action('{"type": "key", "key": "Control+A", "reason": "select all"}')
