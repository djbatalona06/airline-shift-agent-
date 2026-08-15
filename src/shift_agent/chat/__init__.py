"""The dashboard's chat assistant.

The dashboard answers "what happened". This answers "why", and it is the only
part of the system a tired crew member can argue with at three in the morning.

Three deliberate limits shape everything here:

* **It reads; it does not act.** There is no claim tool and no portal tool. The
  one write it can reach — editing the config file — is gated behind a diff the
  human presses Apply on, and the id that authorises the write comes from the
  UI, never from the model.
* **Portal text is untrusted.** Shift titles and verdict details are strings
  FLICA produced, which means anyone who can get text onto her open-time page
  can get text into this prompt. It travels inside a delimited block that the
  system prompt names as data, and the narrow tool surface is what keeps a
  successful injection boring.
* **The key never reaches the page.** In proxy mode the loopback server holds it
  and the browser only ever sees rendered text.
"""

from __future__ import annotations

from .agent import ChatAgent, ChatReply
from .backend import ChatService
from .providers import AnthropicProvider, OpenAICompatibleProvider, Provider, ProviderError

__all__ = [
    "AnthropicProvider",
    "ChatAgent",
    "ChatReply",
    "ChatService",
    "OpenAICompatibleProvider",
    "Provider",
    "ProviderError",
    "build_provider",
]


def build_provider(config, api_key: str | None, http=None) -> Provider:
    """Pick a provider from config. Raises ProviderError when unusable.

    Kept out of `providers.py` so that module has no import of `config`, which
    lets the tests construct providers directly without a whole UserConfig.
    """
    from ..config import LlmProvider

    llm = config.llm
    if llm.needs_key and not api_key:
        raise ProviderError(
            "No API key stored. Run 'shift-agent set-llm-key', or point llm.base_url "
            "at a local model that does not need one."
        )

    if llm.provider is LlmProvider.ANTHROPIC:
        return AnthropicProvider(
            api_key=api_key or "",
            model=llm.model,
            max_tokens=llm.max_tokens,
            effort=llm.effort,
        )
    return OpenAICompatibleProvider(
        base_url=llm.base_url or "",
        api_key=api_key,
        model=llm.model,
        max_tokens=llm.max_tokens,
        http=http,
    )
