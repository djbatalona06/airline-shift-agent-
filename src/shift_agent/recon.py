"""One-shot portal reconnaissance.

Design constraint: we get ONE run of roughly five minutes with the account
holder present, and no retry. Everything here follows from that.

  * Data is streamed to disk as it arrives, never assembled at the end, so a
    browser closed early still leaves a usable capture.
  * Cookies and storage are snapshotted periodically, not just on exit, for the
    same reason.
  * Every capture step is independently guarded; one failure cannot take the
    run down.
  * Both a HAR (complete) and a JSONL event log (crash-resilient) are recorded.
    Redundant on purpose.

Two hard rules:
  * The harness NEVER types credentials and never touches the login form. The
    account holder logs in herself.
  * The harness NEVER clicks a claim control. It records the selector; it does
    not exercise it.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

MAX_BODY_BYTES = 262_144

SSO_SIGNATURES = {
    "okta": ("okta.com", "oktapreview.com"),
    "ping": ("pingidentity.com", "pingone.com", "ping-eng.com"),
    "azure-ad": ("login.microsoftonline.com", "sts.windows.net", "b2clogin.com"),
    "adfs": ("/adfs/ls", "/adfs/oauth2"),
    "auth0": ("auth0.com",),
    "onelogin": ("onelogin.com",),
    "shibboleth": ("/idp/profile/SAML2",),
}

CAPTCHA_SIGNATURES = {
    "recaptcha": ("www.google.com/recaptcha", "recaptcha/api.js", "g-recaptcha"),
    "hcaptcha": ("hcaptcha.com", "h-captcha"),
    "turnstile": ("challenges.cloudflare.com", "cf-turnstile"),
    "funcaptcha": ("funcaptcha.com", "arkoselabs.com"),
}

MFA_SIGNATURES = (
    "verification code", "one-time", "one time passcode", "otp",
    "authenticator", "two-factor", "two factor", "2fa", "mfa",
    "duo security", "push notification", "security code", "verify your identity",
)

SHIFT_KEY_HINTS = (
    "shift", "trip", "pairing", "assignment", "duty", "roster",
    "opentime", "open_time", "available", "schedule", "sequence",
)
TIME_KEY_HINTS = ("start", "end", "begin", "finish", "depart", "arrive", "date", "time")

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[EMAIL]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "[JWT]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"\+?\d[\d\s().-]{8,}\d"), "[PHONE]"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "[HEX-TOKEN]"),
    (re.compile(r"(?i)\b(bearer|token|session|api[_-]?key)\b\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
)


# --- pure helpers (unit-tested without a browser) ----------------------------

def infer_sso(urls: Iterable[str]) -> str | None:
    """Identify an identity provider from a redirect chain.

    This is the cheapest available answer to the question that gates the whole
    project estimate: whether login can ever be unattended.
    """
    for url in urls:
        low = url.lower()
        for provider, needles in SSO_SIGNATURES.items():
            if any(n in low for n in needles):
                return provider
    return None


def detect_captcha(*texts: str) -> list[str]:
    found = []
    for name, needles in CAPTCHA_SIGNATURES.items():
        blob = " ".join(texts).lower()
        if any(n.lower() in blob for n in needles):
            found.append(name)
    return sorted(found)


def detect_mfa(*texts: str) -> list[str]:
    blob = " ".join(texts).lower()
    return sorted({sig for sig in MFA_SIGNATURES if sig in blob})


def looks_like_shift_data(payload: Any) -> int:
    """Score how likely a JSON payload is to be a list of shifts.

    A portal with an internal JSON API makes the adapter dramatically simpler
    than HTML scraping, so surfacing one is the single highest-value discovery
    this capture can make.
    """
    rows = _candidate_rows(payload)
    if not rows:
        return 0

    score = 0
    sample = rows[:5]
    keys = {k.lower() for row in sample if isinstance(row, dict) for k in row}
    if not keys:
        return 0

    if any(any(h in k for h in SHIFT_KEY_HINTS) for k in keys):
        score += 3
    time_keys = [k for k in keys if any(h in k for h in TIME_KEY_HINTS)]
    score += min(len(time_keys), 3)
    if any(k in keys for k in ("id", "shiftid", "shift_id", "tripid", "trip_id")):
        score += 1
    if len(rows) > 1:
        score += 1
    return score


def _candidate_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list) and any(isinstance(r, dict) for r in value):
                return [r for r in value if isinstance(r, dict)]
    return []


def rank_json_endpoints(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = []
    for rec in records:
        body = rec.get("json")
        if body is None:
            continue
        score = looks_like_shift_data(body)
        if score > 0:
            ranked.append({
                "url": rec.get("url"),
                "method": rec.get("method"),
                "status": rec.get("status"),
                "score": score,
                "rows": len(_candidate_rows(body)),
            })
    return sorted(ranked, key=lambda r: (-r["score"], -r["rows"]))


def redact(text: str, names: Iterable[str] = ()) -> str:
    """Mask obvious PII and credentials before anything becomes a test fixture.

    Best-effort, not a guarantee. The raw capture should still be deleted once
    fixtures are extracted rather than relied on being clean.

    `names` exists because pattern matching cannot find a person's name — a
    crew portal renders it as ordinary text ("Requests by Surname, Firstname")
    and it would otherwise survive straight into a committed fixture. Pass the
    account holder's name parts explicitly.
    """
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    for name in names:
        cleaned = str(name).strip()
        if len(cleaned) >= 3:
            text = re.sub(re.escape(cleaned), "[NAME]", text, flags=re.IGNORECASE)
    return text


def cookie_verdict(status: int | None, body: str | None, login_markers: Iterable[str] = ()) -> str:
    """Interpret the replayed-cookie response.

    Decides whether polling can use cheap HTTP or must stay in Playwright,
    which in turn decides whether a 1GB VPS is sufficient.
    """
    if status is None:
        return "INCONCLUSIVE - request failed"
    if status in (401, 403):
        return "LIKELY FAILS - rejected as unauthenticated"
    if status >= 500:
        return "INCONCLUSIVE - server error"
    if 300 <= status < 400:
        return "LIKELY FAILS - redirected, probably to login"
    text = (body or "").lower()
    markers = [m.lower() for m in login_markers] or ["sign in", "log in", "login", "password", "username"]
    if any(m in text for m in markers):
        return "LIKELY FAILS - response looks like a login page"
    if status == 200 and text:
        return "LIKELY WORKS - authenticated content returned outside the browser"
    return "INCONCLUSIVE - empty response"


# --- capture -----------------------------------------------------------------

@dataclass
class Capture:
    outdir: Path
    events: list[dict[str, Any]] = field(default_factory=list)
    navigations: list[str] = field(default_factory=list)
    page_count: int = 0
    frames_captured: int = 0

    def __post_init__(self) -> None:
        self.outdir.mkdir(parents=True, exist_ok=True)
        (self.outdir / "pages").mkdir(exist_ok=True)
        self._events_fh = (self.outdir / "network.jsonl").open("a", encoding="utf-8")

    def write_event(self, record: dict[str, Any]) -> None:
        self.events.append(record)
        try:
            self._events_fh.write(json.dumps(record, default=str) + "\n")
            self._events_fh.flush()  # flushed per event: a crash must not lose the log
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._events_fh.close()
        except Exception:
            pass


WARNING = """\
=============================================================================
 THIS FOLDER CONTAINS LIVE SESSION COOKIES AND PERSONAL INFORMATION.
 Anyone holding these cookies may be able to act as the account holder.

 - Do NOT commit this folder to git or upload it anywhere.
 - Extract the fixtures you need, then DELETE this folder.
 - Run the redaction pass before using any HTML as a test fixture.
=============================================================================
"""


def _instructions(url: str) -> str:
    return f"""
-----------------------------------------------------------------------------
 RECON CAPTURE - about 5 minutes, one time
-----------------------------------------------------------------------------
 A browser window is opening at:
   {url}

 Please do this yourself - I will not type your password:

   1. Log in normally.
   2. Go to the page that lists OPEN / AVAILABLE shifts.
   3. Open ONE shift's detail view.  DO NOT CLAIM IT.
   4. If you have a page showing YOUR assigned schedule, open that too.
   5. Come back here and press ENTER.

 Nothing is claimed, changed, or submitted. This only watches and records.
-----------------------------------------------------------------------------
"""


async def run_capture(
    url: str,
    outdir: Path,
    snapshot_seconds: float = 10.0,
    headless: bool = False,
    auto_seconds: float | None = None,
) -> dict[str, Any]:
    """Capture a session.

    `auto_seconds` runs unattended for a fixed duration instead of waiting for a
    keypress — used to rehearse the harness against a public site so her five
    minutes are never spent debugging this script.
    """
    from playwright.async_api import async_playwright  # lazy: tests need no browser

    cap = Capture(outdir)
    (outdir / "READ-ME-FIRST.txt").write_text(WARNING, encoding="utf-8")
    print(WARNING)
    if auto_seconds is None:
        print(_instructions(url))
    else:
        print(f"[rehearsal] capturing {url} unattended for {auto_seconds}s\n")

    summary: dict[str, Any] = {
        "url": url,
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
    }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            record_har_path=str(outdir / "session.har"),
            ignore_https_errors=True,
        )
        page = await context.new_page()

        async def on_response(response) -> None:
            record: dict[str, Any] = {
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                "url": response.url,
                "method": response.request.method,
                "status": response.status,
                "type": (response.headers or {}).get("content-type", ""),
            }
            try:
                if "json" in record["type"].lower():
                    body = await response.body()
                    if len(body) <= MAX_BODY_BYTES:
                        record["json"] = json.loads(body)
            except Exception:
                pass
            cap.write_event(record)

        snapshot_lock = asyncio.Lock()

        async def snapshot_after_nav() -> None:
            """Capture on navigation, not only on the timer.

            The timer alone is not enough: she may click from the open-shifts
            list into a shift detail well inside one tick, and with a single
            five-minute run there is no chance to go back for the page we
            missed. The short delay lets client-rendered content settle.
            """
            await asyncio.sleep(1.5)
            if snapshot_lock.locked():
                return
            async with snapshot_lock:
                try:
                    await _snapshot_page(page, cap)
                    await _dump_cookies(context, cap.outdir)
                except Exception:
                    pass

        def on_nav(frame) -> None:
            if frame is not page.main_frame:
                return
            cap.navigations.append(frame.url)
            asyncio.create_task(snapshot_after_nav())

        page.on("response", lambda r: asyncio.create_task(on_response(r)))
        page.on("framenavigated", on_nav)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            print(f"  (initial navigation reported: {exc})")

        stop = asyncio.Event()
        snapshots = asyncio.create_task(
            _snapshot_loop(page, context, cap, stop, snapshot_seconds, snapshot_lock)
        )
        if auto_seconds is None:
            await _wait_for_user(context)
        else:
            await asyncio.sleep(auto_seconds)
        stop.set()
        await asyncio.gather(snapshots, return_exceptions=True)

        final = await _final_snapshot(page, context, cap)
        summary.update(final)

        try:
            await context.close()
            await browser.close()
        except Exception:
            pass

    summary.update(_analyse(cap))
    summary["cookie_replay"] = await _cookie_survival_test(outdir, summary.get("last_url"))
    cap.close()

    (outdir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    (outdir / "summary.md").write_text(_render_summary(summary), encoding="utf-8")
    print(_render_summary(summary))
    return summary


async def _wait_for_user(context) -> None:
    """Return when she presses Enter OR closes the browser, whichever comes first."""
    closed = asyncio.Event()
    context.on("close", lambda _=None: closed.set())

    prompt = asyncio.create_task(asyncio.to_thread(input, "\n>>> Press ENTER when finished: "))
    shut = asyncio.create_task(closed.wait())
    await asyncio.wait({prompt, shut}, return_when=asyncio.FIRST_COMPLETED)
    for task in (prompt, shut):
        if not task.done():
            task.cancel()


async def _snapshot_loop(
    page, context, cap: Capture, stop: asyncio.Event, every: float,
    lock: asyncio.Lock | None = None,
) -> None:
    """Periodic capture so an early browser close still yields cookies and DOM.

    Complements the navigation-triggered snapshot: this one catches pages that
    change content without navigating (single-page apps updating in place).
    """
    lock = lock or asyncio.Lock()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        if lock.locked():
            continue
        async with lock:
            try:
                await _snapshot_page(page, cap)
                await _dump_cookies(context, cap.outdir)
            except Exception:
                pass


def _write_snapshot(stem, html: str) -> None:
    stem.with_suffix(".html").write_text(html, encoding="utf-8")
    stem.with_suffix(".redacted.html").write_text(redact(html), encoding="utf-8")


async def _snapshot_page(page, cap: Capture) -> None:
    """Capture the document AND every child frame.

    `page.content()` returns only the top-level document. Frame-based portals —
    FLICA serves its open-time list inside four iframes — would otherwise yield
    a 75KB snapshot containing zero tables, with the actual data invisible. The
    HAR still records frame documents, but relying on that means hand-digging
    through a 26MB file to find them.
    """
    if page.is_closed():
        return
    cap.page_count += 1
    slug = re.sub(r"[^a-z0-9]+", "-", (page.url or "page").lower())[:60].strip("-")
    base = cap.outdir / "pages" / f"{cap.page_count:03d}-{slug or 'page'}"

    try:
        _write_snapshot(base, await page.content())
    except Exception:
        pass
    try:
        await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception:
        pass

    try:
        frames = [f for f in page.frames if f is not page.main_frame]
    except Exception:
        frames = []
    for index, frame in enumerate(frames, start=1):
        try:
            if not frame.url or frame.url.startswith("about:"):
                continue
            fslug = re.sub(r"[^a-z0-9]+", "-", frame.url.lower())[:40].strip("-")
            _write_snapshot(
                cap.outdir / "pages" / f"{cap.page_count:03d}f{index}-{fslug or 'frame'}",
                await frame.content(),
            )
            cap.frames_captured += 1
        except Exception:
            pass


async def _dump_cookies(context, outdir: Path) -> None:
    cookies = await context.cookies()
    (outdir / "cookies.json").write_text(json.dumps(cookies, indent=2), encoding="utf-8")


async def _final_snapshot(page, context, cap: Capture) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        out["last_url"] = page.url
        out["last_title"] = await page.title()
    except Exception:
        out["last_url"] = cap.navigations[-1] if cap.navigations else None
    try:
        await _snapshot_page(page, cap)
    except Exception:
        pass
    try:
        await _dump_cookies(context, cap.outdir)
    except Exception:
        pass
    try:
        storage = await page.evaluate(
            "() => ({local: {...localStorage}, session: {...sessionStorage}})"
        )
        (cap.outdir / "storage.json").write_text(json.dumps(storage, indent=2), encoding="utf-8")
        out["storage_keys"] = sorted(storage.get("local", {}))
    except Exception:
        pass
    return out


def _analyse(cap: Capture) -> dict[str, Any]:
    html_blobs = []
    for path in sorted((cap.outdir / "pages").glob("*.html")):
        if path.name.endswith(".redacted.html"):
            continue
        try:
            html_blobs.append(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            pass

    urls = cap.navigations + [e.get("url", "") for e in cap.events]
    return {
        "sso_provider": infer_sso(urls),
        "captcha": detect_captcha(*html_blobs, *urls),
        "mfa_signals": detect_mfa(*html_blobs),
        "json_endpoints": rank_json_endpoints(cap.events)[:10],
        "requests_seen": len(cap.events),
        "pages_captured": cap.page_count,
        "navigations": cap.navigations[:40],
    }


async def _cookie_survival_test(outdir: Path, last_url: str | None) -> dict[str, Any]:
    """Replay the last authenticated URL with cookies only, outside the browser."""
    import httpx

    if not last_url:
        return {"verdict": "INCONCLUSIVE - no URL captured"}
    try:
        cookies = json.loads((outdir / "cookies.json").read_text(encoding="utf-8"))
    except Exception:
        return {"verdict": "INCONCLUSIVE - no cookies captured"}

    jar = {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}
    status: int | None = None
    body: str | None = None
    try:
        async with httpx.AsyncClient(
            cookies=jar, timeout=20, follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            resp = await client.get(last_url)
            status, body = resp.status_code, resp.text[:20_000]
    except Exception as exc:
        return {"verdict": f"INCONCLUSIVE - {exc}"}

    return {
        "verdict": cookie_verdict(status, body),
        "status": status,
        "cookie_count": len(jar),
    }


def _render_summary(s: dict[str, Any]) -> str:
    lines = [
        "# Recon summary",
        "",
        f"- Captured: {s.get('started')}",
        f"- Final URL: {s.get('last_url')}",
        f"- Requests seen: {s.get('requests_seen')}",
        f"- Pages captured: {s.get('pages_captured')}",
        "",
        "## Gating questions",
        "",
        f"- **SSO provider:** {s.get('sso_provider') or 'none detected (direct login)'}",
        f"- **Captcha:** {', '.join(s.get('captcha') or []) or 'none detected'}",
        f"- **MFA signals:** {', '.join(s.get('mfa_signals') or []) or 'none detected'}",
        f"- **Cookies work outside the browser:** {(s.get('cookie_replay') or {}).get('verdict')}",
        "",
        "## Candidate JSON endpoints",
        "",
    ]
    endpoints = s.get("json_endpoints") or []
    if not endpoints:
        lines.append("None found - the adapter will need HTML parsing.")
    for ep in endpoints:
        lines.append(f"- score {ep['score']} ({ep['rows']} rows) {ep['method']} {ep['url']}")
    lines += ["", "## Next", "",
              "Extract fixtures from `pages/*.redacted.html`, then DELETE this folder."]
    return "\n".join(lines)
