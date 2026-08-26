"""run_vision_loop() against a fake Playwright page and a fake VisionClient.

No real browser, no network - `page` and `client` are both injected, which is
the whole point of keeping them behind a Protocol/duck-typed interface.
"""

from __future__ import annotations

import asyncio

from shift_agent.friction.actions import Action, ActionType
from shift_agent.friction.vision_loop import run_vision_loop


class FakeMouse:
    def __init__(self):
        self.clicks = []

    async def click(self, x, y):
        self.clicks.append((x, y))


class FakeKeyboard:
    def __init__(self):
        self.typed = []
        self.pressed = []

    async def type(self, text):
        self.typed.append(text)

    async def press(self, key):
        self.pressed.append(key)


class FakePage:
    def __init__(self):
        self.mouse = FakeMouse()
        self.keyboard = FakeKeyboard()
        self.screenshots = 0

    async def screenshot(self, clip=None):
        self.screenshots += 1
        return b"fake-png-bytes"


class ScriptedVisionClient:
    """Replays a fixed sequence of actions, one per call."""

    def __init__(self, actions):
        self._actions = list(actions)
        self.calls = 0

    async def next_action(self, *, screenshot_png, goal_prompt, history):
        self.calls += 1
        return self._actions.pop(0)


class SlowVisionClient:
    async def next_action(self, *, screenshot_png, goal_prompt, history):
        await asyncio.sleep(10)


async def test_clicks_then_reports_done():
    page = FakePage()
    client = ScriptedVisionClient(
        [
            Action(type=ActionType.CLICK, x=5, y=5, reason="checkbox"),
            Action(type=ActionType.DONE, reason="solved"),
        ]
    )
    result = await run_vision_loop(page, client=client, goal_prompt="solve it")
    assert result.success
    assert result.steps == 2
    assert page.mouse.clicks == [(5, 5)]


async def test_fail_action_stops_the_loop():
    page = FakePage()
    client = ScriptedVisionClient([Action(type=ActionType.FAIL, reason="stuck")])
    result = await run_vision_loop(page, client=client, goal_prompt="solve it")
    assert not result.success
    assert result.detail == "stuck"


async def test_stops_at_max_steps():
    page = FakePage()
    client = ScriptedVisionClient([Action(type=ActionType.WAIT) for _ in range(5)])
    result = await run_vision_loop(page, client=client, goal_prompt="solve it", max_steps=3)
    assert not result.success
    assert result.steps == 3
    assert "max_steps" in result.detail


async def test_step_timeout_stops_the_loop():
    page = FakePage()
    result = await run_vision_loop(
        page, client=SlowVisionClient(), goal_prompt="solve it", step_timeout_s=0.05
    )
    assert not result.success
    assert "step_timeout_s" in result.detail


async def test_type_and_key_actions_reach_the_page():
    page = FakePage()
    client = ScriptedVisionClient(
        [
            Action(type=ActionType.TYPE, text="hello", reason="fill field"),
            Action(type=ActionType.KEY, key="Enter", reason="submit"),
            Action(type=ActionType.DONE, reason="done"),
        ]
    )
    result = await run_vision_loop(page, client=client, goal_prompt="solve it")
    assert result.success
    assert page.keyboard.typed == ["hello"]
    assert page.keyboard.pressed == ["Enter"]


class UnparseableVisionClient:
    """What a model does routinely: answer in prose, or get truncated."""

    async def next_action(self, *, screenshot_png, goal_prompt, history):
        from shift_agent.friction.actions import parse_action

        return parse_action("I clicked the checkbox for you.")


class HangingPage(FakePage):
    async def screenshot(self, clip=None):
        await asyncio.sleep(10)


class HangingMouse(FakeMouse):
    async def click(self, x, y):
        await asyncio.sleep(10)


async def test_an_unusable_reply_ends_the_run_instead_of_raising():
    """ActionParseError is a ValueError, so before this it escaped both the loop
    and the benchmark and reached the user as a traceback."""
    page = FakePage()
    result = await run_vision_loop(page, client=UnparseableVisionClient(), goal_prompt="solve it")

    assert not result.success
    assert "unusable action" in result.detail


async def test_a_hung_screenshot_does_not_block_forever():
    """total_timeout_s is only checked between steps; without a browser-call
    ceiling the stated time bound is not a bound at all."""
    result = await run_vision_loop(
        HangingPage(), client=ScriptedVisionClient([]), goal_prompt="solve it",
        browser_timeout_s=0.05,
    )

    assert not result.success
    assert "screenshot exceeded" in result.detail


async def test_a_hung_action_does_not_block_forever():
    page = FakePage()
    page.mouse = HangingMouse()
    client = ScriptedVisionClient([Action(type=ActionType.CLICK, x=1, y=1, reason="checkbox")])

    result = await run_vision_loop(
        page, client=client, goal_prompt="solve it", browser_timeout_s=0.05
    )

    assert not result.success
    assert "exceeded" in result.detail


async def test_elapsed_time_is_reported_on_every_exit_path():
    page = FakePage()
    client = ScriptedVisionClient([Action(type=ActionType.DONE, reason="solved")])
    result = await run_vision_loop(page, client=client, goal_prompt="solve it")
    assert result.elapsed_s >= 0
