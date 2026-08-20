"""screenshot -> vision model -> execute -> repeat.

The core loop of the friction-handling pattern this toolkit implements: show
the model what the page looks like, get back one structured action, perform
it, and repeat until the model says it's done, gives up, or a timeout/step
cap is hit. `page` and `client` are both injected so this is testable against
a fake Playwright page and a fake `VisionClient` - no real browser or network
required in CI.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .actions import Action, ActionParseError, ActionType
from .vision_client import VisionClient

# Browser calls get their own ceiling. `total_timeout_s` is only checked between
# steps, so without this a hung screenshot or a click that never settles would
# block past the deadline indefinitely - and a stated time bound is the whole
# claim this loop makes.
BROWSER_STEP_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class LoopResult:
    success: bool
    steps: int
    elapsed_s: float
    last_action: Action | None
    detail: str = ""


async def run_vision_loop(
    page,
    *,
    client: VisionClient,
    goal_prompt: str,
    clip: dict[str, int] | None = None,
    max_steps: int = 20,
    step_timeout_s: float = 15.0,
    total_timeout_s: float = 300.0,
    browser_timeout_s: float = BROWSER_STEP_TIMEOUT_S,
) -> LoopResult:
    start = time.monotonic()
    history: list[Action] = []

    def _stop(success: bool, steps: int, detail: str, action: Action | None = None) -> LoopResult:
        return LoopResult(
            success, steps, time.monotonic() - start,
            action if action is not None else (history[-1] if history else None),
            detail=detail,
        )

    for step in range(1, max_steps + 1):
        elapsed = time.monotonic() - start
        if elapsed >= total_timeout_s:
            return _stop(False, step - 1, f"exceeded total_timeout_s={total_timeout_s}")

        try:
            screenshot = await asyncio.wait_for(
                page.screenshot(clip=clip) if clip else page.screenshot(),
                timeout=browser_timeout_s,
            )
        except TimeoutError:
            return _stop(False, step - 1, f"step {step} screenshot exceeded {browser_timeout_s}s")

        try:
            action = await asyncio.wait_for(
                client.next_action(screenshot_png=screenshot, goal_prompt=goal_prompt, history=history),
                timeout=step_timeout_s,
            )
        except TimeoutError:
            return _stop(False, step - 1, f"step {step} exceeded step_timeout_s={step_timeout_s}")
        except ActionParseError as exc:
            # An unusable reply is routine, not exceptional: models wrap JSON in
            # prose, truncate at max_tokens, or answer in words. Ending the run
            # with a reportable result beats a traceback out of the CLI.
            return _stop(False, step - 1, f"step {step} returned an unusable action: {exc}")

        history.append(action)

        if action.type is ActionType.DONE:
            return _stop(True, step, action.reason, action)
        if action.type is ActionType.FAIL:
            return _stop(False, step, action.reason, action)

        try:
            await asyncio.wait_for(_execute(page, action), timeout=browser_timeout_s)
        except TimeoutError:
            return _stop(False, step, f"step {step} action {action.type.value} exceeded {browser_timeout_s}s")

    return _stop(False, max_steps, f"exceeded max_steps={max_steps}")


async def _execute(page, action: Action) -> None:
    if action.type is ActionType.CLICK:
        await page.mouse.click(action.x, action.y)
    elif action.type is ActionType.TYPE:
        await page.keyboard.type(action.text)
    elif action.type is ActionType.KEY:
        await page.keyboard.press(action.key)
    elif action.type is ActionType.WAIT:
        await asyncio.sleep(1.0)
