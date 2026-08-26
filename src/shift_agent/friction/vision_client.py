"""Vision-model client used by `vision_loop.run_vision_loop`.

`VisionClient` is a `Protocol`, not a concrete import, so the loop can be
tested against a hand-written fake with zero network and zero dependency on
the `anthropic` package itself. `AnthropicVisionClient` is the one real
implementation.
"""

from __future__ import annotations

from typing import Protocol

from .actions import Action, parse_action

DEFAULT_VISION_MODEL = "claude-sonnet-5"


class VisionClientError(RuntimeError):
    """Raised when the vision model cannot be reached or replies unusably."""


class VisionClient(Protocol):
    async def next_action(
        self, *, screenshot_png: bytes, goal_prompt: str, history: list[Action]
    ) -> Action: ...


class AnthropicVisionClient:
    """Talks to the Anthropic Messages API for one action per call.

    `anthropic` is imported here, inside `__init__`, rather than at module
    top. That keeps `import shift_agent.friction.vision_client` free of any
    import-time side effect or cost for code paths - including tests - that
    only need the `VisionClient` Protocol or a fake implementation of it.
    """

    def __init__(self, api_key: str, model: str = DEFAULT_VISION_MODEL) -> None:
        import anthropic

        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model

    async def next_action(
        self, *, screenshot_png: bytes, goal_prompt: str, history: list[Action]
    ) -> Action:
        import base64

        history_text = "\n".join(f"- {a.type.value}: {a.reason}" for a in history) or "(none yet)"
        prompt = (
            f"{goal_prompt}\n\n"
            f"Steps taken so far:\n{history_text}\n\n"
            "Reply with exactly one JSON object describing the next action, "
            'e.g. {"type": "click", "x": 120, "y": 340, "reason": "..."} or '
            '{"type": "done", "reason": "..."}. No other text.'
        )
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=512,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": base64.b64encode(screenshot_png).decode("ascii"),
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )
        except Exception as exc:  # anthropic.APIError and friends
            raise VisionClientError(f"vision model call failed: {exc}") from exc

        text = "".join(block.text for block in response.content if block.type == "text")
        return parse_action(text)
