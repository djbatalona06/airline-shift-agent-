"""The chat bubble, in a real browser, against the real loopback server.

A stub stands in for the model so the assertions are about the page and the
server rather than about what a model happened to say. The interesting claims
here are the containment ones: the key must never reach the browser, and nothing
may leave loopback.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from shift_agent.chat.backend import ChatService
from shift_agent.chat.providers import Reply, ToolCall
from shift_agent.config import UserConfig
from shift_agent.dashboard import build_dashboard
from shift_agent.dashboard.server import DashboardServer
from shift_agent.models import ClaimOutcome, ClaimResult, MatchResult, MatchVerdict, Shift
from shift_agent.store import Store

from .conftest import launch_kwargs

pytestmark = pytest.mark.e2e

FAKE_KEY = "sk-ant-api03-NOTAREALKEY00000000000000000"
EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "config" / "users" / "example.yaml"


class ScriptedProvider:
    """Returns queued replies; records the key it was built with, so a test can
    look for that exact string anywhere in the browser."""

    name = "scripted"

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.api_key = FAKE_KEY

    async def complete(self, *, system, messages, tools):
        return self.replies.pop(0) if self.replies else Reply(text="Nothing else to add.")

    async def close(self) -> None:
        return None


def make_config(**over) -> UserConfig:
    raw = {
        "name": "e2e",
        "portal": {"adapter": "mock"},
        "availability": {
            "timezone": "America/New_York",
            "slots": [{"day": "Monday", "start": "08:00", "end": "17:00"}],
        },
    }
    raw.update(over)
    return UserConfig.model_validate(raw)


def seeded_store(tmp_path) -> Store:
    store = Store(tmp_path / "state.db")
    start = datetime.now(UTC) + timedelta(days=2)
    kept = Shift(id="M4A76", start=start, end=start + timedelta(hours=6), title="MCO-ATL-MCO")
    skipped = Shift(
        id="M8W77",
        start=start + timedelta(days=1),
        end=start + timedelta(days=1, hours=5),
        title="MCO-MIA late",
    )
    store.record_seen("e2e", MatchResult(kept, MatchVerdict.MATCH, "grade E"))
    store.record_seen(
        "e2e", MatchResult(skipped, MatchVerdict.OUTSIDE_AVAILABILITY, "starts 23:40")
    )
    store.record_claim("e2e", "M4A76", ClaimResult(ClaimOutcome.CLAIMED, ""), dry_run=False)
    return store


async def serve(tmp_path, *replies, configured=True, with_config_file=False, chat=True):
    """Build a real dashboard behind a real server, and open a real browser."""
    config = make_config()
    store = seeded_store(tmp_path)
    if configured:
        store.set("onboarding_done", True)

    path = None
    if with_config_file:
        path = tmp_path / "user.yaml"
        path.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")

    service = (
        ChatService(store, config, ScriptedProvider(*replies), config_path=path) if chat else None
    )
    outdir = tmp_path / "dash"
    build_dashboard(store, config, outdir, chat_backed=chat)

    server = DashboardServer(outdir, chat=service)
    url = server.start()
    return server, url, store, service, path


async def browser_page(playwright):
    browser = await playwright.chromium.launch(**launch_kwargs(headless=True))
    page = await browser.new_page(viewport={"width": 1180, "height": 860})
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    return browser, page, errors


# --------------------------------------------------------------------------


async def test_answering_from_stored_history_works_through_the_browser(tmp_path):
    """A tool that reads the database, driven from the page. This is the shape
    of nearly every real question, and it exercises the store from the server's
    request thread rather than the one that opened it."""
    server, url, store, _, _ = await serve(
        tmp_path,
        Reply(tool_calls=(ToolCall("t1", "explain_shift", {"shift_id": "M8W77"}),)),
        Reply(text="It starts at 23:40, outside your Monday window."),
    )
    async with async_playwright() as pw:
        browser, page, errors = await browser_page(pw)
        try:
            await page.goto(url)
            await page.click("#chat-fab")
            await page.fill("#chat-input", "why did you skip M8W77?")
            await page.click("#chat-send")
            await page.wait_for_selector("text=outside your Monday window", timeout=10_000)
            assert errors == []
        finally:
            await browser.close()
            server.stop()
            store.close()


async def test_chat_round_trip(tmp_path):
    server, url, store, _, _ = await serve(
        tmp_path, Reply(text="It starts at 23:40, outside your Monday window.")
    )
    async with async_playwright() as pw:
        browser, page, errors = await browser_page(pw)
        try:
            await page.goto(url)
            await page.click("#chat-fab")
            await page.fill("#chat-input", "why did you skip M8W77?")
            await page.click("#chat-send")
            await page.wait_for_selector("text=outside your Monday window", timeout=10_000)

            assert errors == []
            log = await page.inner_text("#chat-log")
            assert "why did you skip M8W77?" in log
        finally:
            await browser.close()
            server.stop()
            store.close()


async def test_api_key_never_reaches_the_browser(tmp_path):
    """In proxy mode the server holds the key. If it ever appears in the DOM,
    in localStorage or in the payload, that guarantee is gone."""
    server, url, store, _, _ = await serve(tmp_path, Reply(text="All quiet."))
    async with async_playwright() as pw:
        browser, page, _ = await browser_page(pw)
        try:
            await page.goto(url)
            await page.click("#chat-fab")
            await page.fill("#chat-input", "status?")
            await page.click("#chat-send")
            await page.wait_for_selector("text=All quiet.", timeout=10_000)

            html = await page.content()
            storage = await page.evaluate("() => JSON.stringify(localStorage)")
            payload = await page.inner_text("#payload")

            for haystack in (html, storage, payload):
                assert FAKE_KEY not in haystack
                assert "sk-ant" not in haystack
        finally:
            await browser.close()
            server.stop()
            store.close()


async def test_nothing_leaves_loopback(tmp_path):
    server, url, store, _, _ = await serve(tmp_path, Reply(text="Fine."))
    async with async_playwright() as pw:
        browser, page, _ = await browser_page(pw)
        external: list[str] = []
        page.on(
            "request",
            lambda r: (
                external.append(r.url)
                if not r.url.startswith(("http://127.0.0.1", "about:", "data:", "chrome"))
                else None
            ),
        )
        try:
            await page.goto(url)
            await page.click("#chat-fab")
            await page.fill("#chat-input", "hello")
            await page.click("#chat-send")
            await page.wait_for_selector("text=Fine.", timeout=10_000)
            assert external == []
        finally:
            await browser.close()
            server.stop()
            store.close()


async def test_onboarding_shows_when_no_key_is_stored(tmp_path):
    server, url, store, _, _ = await serve(tmp_path, configured=False)
    async with async_playwright() as pw:
        browser, page, errors = await browser_page(pw)
        try:
            await page.goto(url)
            # Opens itself: the setup card is the point of a first run.
            await page.wait_for_selector("#chat-setup", state="visible", timeout=5_000)
            assert "set-llm-key" in await page.inner_text("#chat-setup")
            assert await page.locator("#chat-form").is_hidden()
            assert errors == []
        finally:
            await browser.close()
            server.stop()
            store.close()


async def test_no_setup_card_once_configured(tmp_path):
    server, url, store, _, _ = await serve(tmp_path, Reply(text="hi"))
    async with async_playwright() as pw:
        browser, page, _ = await browser_page(pw)
        try:
            await page.goto(url)
            await page.click("#chat-fab")
            assert await page.locator("#chat-setup").is_hidden()
            assert await page.locator("#chat-form").is_visible()
        finally:
            await browser.close()
            server.stop()
            store.close()


async def test_config_change_needs_apply_before_anything_is_written(tmp_path):
    """The whole safety story for the one write the assistant can reach."""
    server, url, store, service, config_path = await serve(
        tmp_path,
        Reply(
            tool_calls=(
                ToolCall(
                    "t1",
                    "propose_config_change",
                    {"patch": {"rules": {"min_rest_hours": 12}}, "summary": "raise rest to 12h"},
                ),
            )
        ),
        Reply(text="Proposed - press Apply to confirm."),
        with_config_file=True,
    )
    original = config_path.read_text(encoding="utf-8")
    async with async_playwright() as pw:
        browser, page, errors = await browser_page(pw)
        try:
            await page.goto(url)
            await page.click("#chat-fab")
            await page.fill("#chat-input", "give me more rest")
            await page.click("#chat-send")
            await page.wait_for_selector(".chat-diff", timeout=10_000)

            # The diff is on screen and the file is untouched.
            diff = await page.inner_text(".chat-diff")
            assert "min_rest_hours" in diff
            assert config_path.read_text(encoding="utf-8") == original

            await page.click("button[data-apply]")
            await page.wait_for_selector("text=Saved", timeout=10_000)

            written = config_path.read_text(encoding="utf-8")
            assert written != original
            assert UserConfig.load(config_path).rules.min_rest_hours == 12
            # The comments that document the file survived the edit.
            assert "Domicile lock" in written
            assert errors == []
        finally:
            await browser.close()
            server.stop()
            store.close()


async def test_discarding_a_proposal_writes_nothing(tmp_path):
    server, url, store, _, config_path = await serve(
        tmp_path,
        Reply(
            tool_calls=(
                ToolCall(
                    "t1",
                    "propose_config_change",
                    {"patch": {"rules": {"min_rest_hours": 12}}, "summary": "raise rest"},
                ),
            )
        ),
        Reply(text="Proposed."),
        with_config_file=True,
    )
    original = config_path.read_text(encoding="utf-8")
    async with async_playwright() as pw:
        browser, page, _ = await browser_page(pw)
        try:
            await page.goto(url)
            await page.click("#chat-fab")
            await page.fill("#chat-input", "more rest")
            await page.click("#chat-send")
            await page.wait_for_selector(".chat-diff", timeout=10_000)
            await page.click("button[data-discard]")
            await page.wait_for_selector(".chat-diff", state="detached", timeout=5_000)
            assert config_path.read_text(encoding="utf-8") == original
        finally:
            await browser.close()
            server.stop()
            store.close()


async def test_bubble_is_legible_in_every_theme(tmp_path):
    """The bubble uses only existing design tokens, so this is really a check
    that no hard-coded colour crept in."""
    server, url, store, _, _ = await serve(tmp_path, Reply(text="hi"))
    async with async_playwright() as pw:
        browser, page, errors = await browser_page(pw)
        try:
            await page.goto(url)
            await page.click("#chat-fab")
            for theme in ("contrail", "departure", "jetway", "night"):
                await page.select_option("#theme", theme)
                panel = page.locator("#chat-panel")
                assert await panel.is_visible()
                background = await panel.evaluate(
                    "el => getComputedStyle(el).backgroundColor"
                )
                colour = await panel.evaluate("el => getComputedStyle(el).color")
                assert background != colour  # would be invisible text
                assert "rgba(0, 0, 0, 0)" not in background  # must not be transparent
            assert errors == []
        finally:
            await browser.close()
            server.stop()
            store.close()


async def test_transcript_survives_a_reload(tmp_path):
    """The poller rewrites index.html every cycle, so anything the page needs
    to remember has to live in localStorage."""
    server, url, store, _, _ = await serve(tmp_path, Reply(text="Remembered answer."))
    async with async_playwright() as pw:
        browser, page, _ = await browser_page(pw)
        try:
            await page.goto(url)
            await page.click("#chat-fab")
            await page.fill("#chat-input", "a question")
            await page.click("#chat-send")
            await page.wait_for_selector("text=Remembered answer.", timeout=10_000)

            await page.reload()
            await page.wait_for_selector("text=Remembered answer.", timeout=5_000)
        finally:
            await browser.close()
            server.stop()
            store.close()


async def test_a_page_without_a_backend_offers_setup_instead_of_a_dead_box(tmp_path):
    server, url, store, _, _ = await serve(tmp_path, chat=False)
    async with async_playwright() as pw:
        browser, page, errors = await browser_page(pw)
        try:
            await page.goto(url)
            await page.wait_for_selector("#chat-setup", state="visible", timeout=5_000)
            assert "shift-agent run" in await page.inner_text("#chat-setup")
            assert errors == []
        finally:
            await browser.close()
            server.stop()
            store.close()
