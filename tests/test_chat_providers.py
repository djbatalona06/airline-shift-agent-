"""Providers, and the containment rules around the API key.

The OpenAI-compatible path is exercised through `httpx.MockTransport`, the same
fake-transport idiom `test_telegram.py` uses for the Bot API. The Anthropic path
is driven through an injected fake client, because the SDK's response objects
are what the code actually branches on.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from shift_agent.chat import build_provider
from shift_agent.chat.providers import (
    AnthropicProvider,
    OpenAICompatibleProvider,
    ProviderError,
)
from shift_agent.config import UserConfig
from shift_agent.logging_safe import SecretScrubbingFilter, scrub

# Fabricated, but shaped like the real thing so the scrubbing tests mean something.
FAKE_KEY = "sk-ant-api03-NOTAREALKEY000000000000000000000"


def make_config(**llm) -> UserConfig:
    return UserConfig.model_validate(
        {
            "name": "tester",
            "portal": {"adapter": "mock"},
            "availability": {"timezone": "UTC"},
            "llm": llm,
        }
    )


class FakeOpenAI:
    """A minimal /chat/completions endpoint."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.status = 200
        self.body: dict = {
            "choices": [{"message": {"content": "Nothing is paused."}}],
            "usage": {"total_tokens": 12},
        }

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(
            {
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": json.loads(request.content or b"{}"),
            }
        )
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"})
        return httpx.Response(200, json=self.body)


def openai_provider(api: FakeOpenAI, **over) -> OpenAICompatibleProvider:
    kwargs = {
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "llama3.1",
        "http": httpx.AsyncClient(transport=api.transport()),
    }
    kwargs.update(over)
    return OpenAICompatibleProvider(**kwargs)


# --------------------------------------------------------------------------
# OpenAI-compatible
# --------------------------------------------------------------------------


async def test_plain_completion(tmp_path):
    api = FakeOpenAI()
    reply = await openai_provider(api).complete(system="sys", messages=[], tools=[])
    assert reply.text == "Nothing is paused."
    assert not reply.wants_tools


async def test_system_prompt_is_sent_first():
    api = FakeOpenAI()
    await openai_provider(api).complete(
        system="sys", messages=[{"role": "user", "content": "hi"}], tools=[]
    )
    sent = api.calls[0]["body"]["messages"]
    assert sent[0] == {"role": "system", "content": "sys"}
    assert sent[1]["content"] == "hi"


async def test_tool_calls_are_parsed():
    api = FakeOpenAI()
    api.body = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "get_status", "arguments": '{"x": 1}'},
                        }
                    ],
                }
            }
        ]
    }
    reply = await openai_provider(api).complete(system="s", messages=[], tools=[])
    assert reply.wants_tools
    assert reply.tool_calls[0].name == "get_status"
    assert reply.tool_calls[0].arguments == {"x": 1}


async def test_unparseable_tool_arguments_are_dropped_not_raised():
    """A small local model that emits broken JSON should read as a skipped
    tool, not as a failed request."""
    api = FakeOpenAI()
    api.body = {
        "choices": [
            {
                "message": {
                    "content": "here",
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "get_status", "arguments": "{not json"}}
                    ],
                }
            }
        ]
    }
    reply = await openai_provider(api).complete(system="s", messages=[], tools=[])
    assert reply.tool_calls == ()
    assert reply.text == "here"


async def test_no_authorization_header_when_no_key():
    api = FakeOpenAI()
    await openai_provider(api).complete(system="s", messages=[], tools=[])
    assert "authorization" not in {k.lower() for k in api.calls[0]["headers"]}


async def test_tool_results_are_flattened_for_the_openai_shape():
    api = FakeOpenAI()
    await openai_provider(api).complete(
        system="s",
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "q"}]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "RESULT"}],
            },
        ],
        tools=[],
    )
    flattened = " ".join(m["content"] for m in api.calls[0]["body"]["messages"])
    assert "RESULT" in flattened


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "key was rejected"), (429, "Rate limited"), (503, "provider returned an error")],
)
async def test_http_errors_become_readable_messages(status, expected):
    api = FakeOpenAI()
    api.status = status
    with pytest.raises(ProviderError, match=expected):
        await openai_provider(api).complete(system="s", messages=[], tools=[])


async def test_unreachable_endpoint_says_so():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:9/v1",
        model="m",
        http=httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
    )
    with pytest.raises(ProviderError, match="Could not reach the model endpoint"):
        await provider.complete(system="s", messages=[], tools=[])


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------


class FakeAnthropic:
    def __init__(self, response) -> None:
        self._response = response
        self.kwargs: dict = {}
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def anthropic_response(*, content, stop_reason="end_turn"):
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


async def test_anthropic_text_reply():
    client = FakeAnthropic(
        anthropic_response(content=[SimpleNamespace(type="text", text="All quiet.")])
    )
    provider = AnthropicProvider(api_key=FAKE_KEY, client=client)
    reply = await provider.complete(system="s", messages=[], tools=[])
    assert reply.text == "All quiet."
    assert reply.usage["input_tokens"] == 10


async def test_anthropic_request_uses_adaptive_thinking_and_effort():
    client = FakeAnthropic(anthropic_response(content=[]))
    provider = AnthropicProvider(api_key=FAKE_KEY, client=client, effort="high")
    await provider.complete(
        system="s",
        messages=[],
        tools=[{"name": "t", "description": "d", "parameters": {"type": "object"}}],
    )
    assert client.kwargs["thinking"] == {"type": "adaptive"}
    assert client.kwargs["output_config"] == {"effort": "high"}
    # Schemas are translated to the SDK's field name, not passed through.
    assert client.kwargs["tools"][0]["input_schema"] == {"type": "object"}


async def test_refusal_is_handled_before_content_is_read():
    """A declined request arrives as a perfectly good 200 with nothing in it.
    Indexing content[0] here would surface as a crash, not as a refusal."""
    client = FakeAnthropic(anthropic_response(content=[], stop_reason="refusal"))
    provider = AnthropicProvider(api_key=FAKE_KEY, client=client)
    reply = await provider.complete(system="s", messages=[], tools=[])
    assert reply.refused is True
    assert reply.text


async def test_anthropic_tool_use_is_parsed():
    client = FakeAnthropic(
        anthropic_response(
            content=[
                SimpleNamespace(type="text", text="looking"),
                SimpleNamespace(type="tool_use", id="c1", name="get_status", input={"a": 1}),
            ]
        )
    )
    provider = AnthropicProvider(api_key=FAKE_KEY, client=client)
    reply = await provider.complete(system="s", messages=[], tools=[])
    assert reply.tool_calls[0].name == "get_status"
    assert reply.tool_calls[0].arguments == {"a": 1}


# --------------------------------------------------------------------------
# selection and key containment
# --------------------------------------------------------------------------


def test_anthropic_without_a_key_is_refused_with_advice():
    with pytest.raises(ProviderError, match="set-llm-key"):
        build_provider(make_config(), api_key=None)


def test_local_endpoint_needs_no_key():
    config = make_config(provider="openai_compatible", base_url="http://127.0.0.1:11434/v1")
    assert build_provider(config, api_key=None).name == "openai_compatible"


def test_remote_openai_compatible_endpoint_still_needs_a_key():
    config = make_config(provider="openai_compatible", base_url="https://api.example.invalid/v1")
    with pytest.raises(ProviderError):
        build_provider(config, api_key=None)


def test_scrubber_redacts_an_unlabelled_api_key():
    assert FAKE_KEY not in scrub(f"upstream said {FAKE_KEY} is invalid")
    assert "[API-KEY]" in scrub(f"upstream said {FAKE_KEY} is invalid")


def test_scrubbing_filter_catches_a_key_in_log_arguments():
    record = logging.LogRecord(
        "x", logging.WARNING, __file__, 1, "auth failed: %s", (FAKE_KEY,), None
    )
    assert SecretScrubbingFilter().filter(record) is True
    assert FAKE_KEY not in record.getMessage()


async def test_provider_errors_never_quote_the_key_back():
    api = FakeOpenAI()
    api.status = 401
    provider = openai_provider(api, api_key=FAKE_KEY)
    with pytest.raises(ProviderError) as excinfo:
        await provider.complete(system="s", messages=[], tools=[])
    assert FAKE_KEY not in str(excinfo.value)


def test_dashboard_payload_never_carries_the_key(tmp_path):
    from shift_agent.dashboard.data import build_payload
    from shift_agent.store import Store

    store = Store(tmp_path / "state.db")
    store.set("onboarding_done", True)
    dumped = json.dumps(build_payload(store, make_config(), chat_backed=True)).lower()
    for forbidden in ("sk-ant", "api_key", "apikey", "password", "secret"):
        assert forbidden not in dumped


def test_key_never_reaches_the_state_database(tmp_path):
    """The DB is unencrypted by design. Secrets live in the keychain, and this
    is the assertion that keeps that split honest for the new key."""
    from shift_agent.store import Store

    store = Store(tmp_path / "state.db")
    store.set("onboarding_done", True)
    store.set("last_cycle", {"at": "now", "evaluated": 1})
    dumped = "".join(
        str(row)
        for row in store.db.execute("SELECT * FROM kv UNION ALL SELECT key, value, updated_at FROM kv")
    )
    assert FAKE_KEY not in dumped
    assert "sk-ant" not in dumped
