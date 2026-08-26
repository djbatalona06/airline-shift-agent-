"""The friction toolkit's MVP acceptance benchmark.

Drives `vision_loop.run_vision_loop` against Google's public reCAPTCHA demo
page - a legitimate, intentionally-solvable benchmark target, never a
production login. Launches its own throwaway browser context; never touches
FLICA's persistent profile (`adapters/flica.py`) and never imports anything
from `adapters/`.

Headless by default, the opposite of `adapters/flica.py`'s headed-by-default:
no human ever needs to watch this solve anything, since the vision model
replaces the human "seeing" step. That is what lets it run unattended on a
VPS with no display - see docs/FRICTION_TOOLKIT.md and docs/INSTALL.md's VPS
section.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .config import FrictionConfig, load_friction_secrets
from .vision_client import AnthropicVisionClient, VisionClientError
from .vision_loop import run_vision_loop

DEMO_URL = "https://www.google.com/recaptcha/api2/demo"
DEFAULT_TIMEOUT_S = 300.0  # the 5-minute bar

# An image challenge is a sequence of tile clicks, verifies, and refreshes, so
# the loop's own 20-step default runs out well before the time budget does. The
# 5-minute total is what actually bounds this run.
BENCH_MAX_STEPS = 40

# A vision call carrying a full-page screenshot regularly runs past the loop's
# 15s default, which would end the run as a timeout while the model was still
# answering - the bench failing on its own harness rather than on the challenge.
BENCH_STEP_TIMEOUT_S = 60.0
GOAL_PROMPT = (
    "You are looking at a webpage with a reCAPTCHA widget. Solve the "
    "challenge (click the 'I'm not a robot' checkbox, then complete any "
    "image challenge that appears), then click the page's Submit button. "
    "Reply with type=done once the challenge is cleared and submitted, or "
    "type=fail if you get stuck."
)


@dataclass(frozen=True)
class BenchResult:
    success: bool
    elapsed_s: float
    steps: int
    detail: str


async def run_benchmark(
    user: str, *, headless: bool = True, timeout_s: float = DEFAULT_TIMEOUT_S
) -> BenchResult:
    from playwright.async_api import async_playwright

    creds = load_friction_secrets(user)
    if not creds.vision_api_key:
        return BenchResult(
            False, 0.0, 0,
            f"no vision API key stored for user {user!r}. "
            f"Run: shift-agent friction-set-vision-key --user {user}",
        )

    # Honours friction.yaml's vision_model override; falls back to the default
    # when no config file has been written, same as everywhere else here.
    config = FrictionConfig.load_default(user)
    client = AnthropicVisionClient(creds.vision_api_key, model=config.vision_model)
    start = time.monotonic()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        try:
            page = await browser.new_page()
            await page.goto(DEMO_URL)
            try:
                result = await run_vision_loop(
                    page,
                    client=client,
                    goal_prompt=GOAL_PROMPT,
                    max_steps=BENCH_MAX_STEPS,
                    step_timeout_s=BENCH_STEP_TIMEOUT_S,
                    total_timeout_s=timeout_s,
                )
            except VisionClientError as exc:
                return BenchResult(False, time.monotonic() - start, 0, str(exc))
        finally:
            await browser.close()

    return BenchResult(result.success, result.elapsed_s, result.steps, result.detail)
