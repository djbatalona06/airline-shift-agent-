"""FLICA crew-portal adapter.

Structure derived from a real capture; see `docs/RECON-FINDINGS.md`.

Parsing is split from browser work on purpose. Every parser here is a pure
function over an HTML string, so the whole data path is tested against fixtures
with no browser, no network, and no account. The browser layer only fetches and
hands text to them.

Three facts about FLICA shape this file:

* **Cookies do not survive outside the browser** (proven during recon), so there
  is no lightweight HTTP path. Playwright drives everything.
* **Content lives in nested iframes.** Selectors must target a frame, not the
  top-level document.
* **The position grade is behind the flight-number link**, not in the list, so it
  is fetched lazily via `enrich()` and cached.

HTML parsing uses the standard library rather than a third-party parser: it keeps
the shipped executable small and avoids another PyInstaller hidden-import to get
wrong.
"""

from __future__ import annotations

import re
import logging
import os
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from .. import paths
from ..logging_safe import scrub
from ..models import AuthResult, AuthState, ClaimOutcome, ClaimResult, Shift
from .base import PortalAdapter, register

log = logging.getLogger(__name__)

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}

# Statuses on otrequest.cgi that mean the request did not succeed.
FAILED_STATUSES = {"unable", "cancelled", "canceled", "denied", "rejected", "expired"}
AWARDED_STATUSES = {"awarded", "granted", "approved"}

CAPTCHA_MARKERS = ("g-recaptcha", "recaptcha/api.js", "hcaptcha", "cf-turnstile")


class Cell(NamedTuple):
    text: str
    href: str | None = None
    value: str | None = None


class _Table(HTMLParser):
    """Collect table rows as lists of (text, href) pairs.

    Deliberately forgiving: FLICA is a CGI application whose markup is not
    guaranteed well-formed, and a parser that raised on a stray tag would take
    the agent down over cosmetics.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[Cell]] = []
        self._row: list[Cell] | None = None
        self._cell: list[str] | None = None
        self._href: str | None = None
        self._value: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
            self._href = None
            self._value = None
        elif self._cell is not None:
            attributes = dict(attrs)
            if tag == "a":
                self._href = attributes.get("href")
            elif tag == "input":
                # Captured because the pairing id on the requests page lives in
                # a checkbox value attribute, not in any visible cell text.
                self._value = attributes.get("value")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            self._row.append(Cell(text, self._href, self._value))
            self._cell = None
            self._href = None
            self._value = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _rows(html: str) -> list[list[Cell]]:
    parser = _Table()
    parser.feed(html)
    return parser.rows


def _texts(row: list[Cell]) -> list[str]:
    return [cell.text for cell in row]


def has_captcha(html: str) -> bool:
    lowered = html.lower()
    return any(marker in lowered for marker in CAPTCHA_MARKERS)


def parse_selected_base(html: str) -> str | None:
    """Read the base the portal is already set to.

    Read-only by design. The agent must never change this selector — picking up
    from the wrong domicile means being rostered out of a city she does not live
    in, which is the most damaging mistake available here.
    """
    match = re.search(r'<select[^>]*name=["\']baseList["\'](.*?)</select>', html, re.I | re.S)
    if not match:
        return None
    selected = re.search(r'<option[^>]*\bselected\b[^>]*value=["\']([^"\']+)', match.group(1), re.I)
    if selected:
        return selected.group(1).strip().upper()
    selected = re.search(r'<option[^>]*value=["\']([^"\']+)["\'][^>]*\bselected\b', match.group(1), re.I)
    return selected.group(1).strip().upper() if selected else None


def _parse_ddmon(token: str, tz: ZoneInfo, reference: datetime | None = None) -> datetime | None:
    """Turn '14AUG' into a concrete date.

    FLICA omits the year. Assume the next occurrence: if the date has already
    passed by more than a week, it belongs to next year. Guessing the current
    year unconditionally would make every January pairing look long expired.
    """
    match = re.match(r"(\d{1,2})\s*([A-Za-z]{3})", token.strip())
    if not match:
        return None
    day, month_name = int(match.group(1)), match.group(2).upper()
    month = MONTHS.get(month_name)
    if not month:
        return None
    now = reference or datetime.now(tz)
    for year in (now.year, now.year + 1):
        try:
            candidate = datetime(year, month, day, tzinfo=tz)
        except ValueError:
            continue
        if candidate >= now - timedelta(days=7):
            return candidate
    return None


def _apply_hhmm(base: datetime, hhmm: str) -> datetime | None:
    digits = re.sub(r"\D", "", hhmm or "")
    if len(digits) != 4:
        return None
    hour, minute = int(digits[:2]), int(digits[2:])
    if hour > 23 or minute > 59:
        return None
    return base.replace(hour=hour, minute=minute)


def parse_open_shifts(
    html: str, timezone: str = "America/New_York", reference: datetime | None = None
) -> list[Shift]:
    """Parse the Daily Opentime Pot.

    Columns: Pairing Dates Days Report Depart Arrive "Blk Hrs" Credit
             "Time in OT" Layover Prem
    """
    tz = ZoneInfo(timezone)
    base = parse_selected_base(html)
    out: list[Shift] = []

    for row in _rows(html):
        cells = _texts(row)
        if len(cells) < 11:
            continue
        pairing, href = row[0].text, row[0].href
        if not re.fullmatch(r"[A-Z0-9]{4,6}", pairing or ""):
            continue

        day = _parse_ddmon(cells[1], tz, reference)
        if day is None:
            continue
        start = _apply_hhmm(day, cells[3])          # Report
        end = _apply_hhmm(day, cells[5])            # Arrive
        if start is None or end is None:
            continue

        try:
            span = max(1, int(re.sub(r"\D", "", cells[2]) or 1))
        except ValueError:
            span = 1
        end += timedelta(days=span - 1)
        if end <= start:
            end += timedelta(days=1)                # crossed midnight

        premium = bool(cells[10].strip()) and cells[10].strip().upper() not in {"-", "N"}
        pid = None
        if href:
            pid_match = re.search(r"PID=([^&\s]+)", href)
            pid = pid_match.group(1) if pid_match else None

        out.append(
            Shift(
                id=pairing,
                start=start,
                end=end,
                title=f"{pairing} {cells[1]}",
                location=cells[9] if cells[9] not in ("-", "") else None,
                meta={
                    "base": base,
                    "premium": premium,
                    "pairing_id": pid or pairing,
                    "detail_url": href,
                    "credit": cells[7],
                    "block_hours": cells[6],
                },
            )
        )
    return out


def parse_position(html: str) -> str | None:
    """Read the offered position (A-E) from a pairing detail page.

    Prefers an explicit "Open Position" label. Falls back to the crew-complement
    string (FA01FB01…) only when exactly one position is listed, because that
    string describes the whole crew rather than the seat being offered — reading
    it as the offer when several are present would be a guess, and a wrong guess
    puts her in a seat she did not want.
    """
    # Walk parsed cells rather than raw HTML: the label and the value sit in
    # adjacent <td>s, so any regex spanning them has to cross tags.
    for row in _rows(html):
        cells = _texts(row)
        for index, text in enumerate(cells):
            if not re.search(r"open\s*position", text, re.I):
                continue
            same_cell = re.search(r"\b([A-E])\b", re.sub(r"(?i)open\s*position", "", text))
            if same_cell:
                return same_cell.group(1).upper()
            if index + 1 < len(cells):
                neighbour = re.fullmatch(r"\s*([A-E])\s*", cells[index + 1])
                if neighbour:
                    return neighbour.group(1).upper()

    complement = re.search(r"((?:F[A-E]\d{2}){2,})", html)
    if complement:
        letters = re.findall(r"F([A-E])\d{2}", complement.group(1))
        if len(set(letters)) == 1:
            return letters[0].upper()
    return None


def parse_schedule(
    html: str, timezone: str = "America/New_York", reference: datetime | None = None
) -> list[Shift]:
    """Parse assigned trips for conflict and rest checking."""
    tz = ZoneInfo(timezone)
    out: list[Shift] = []

    for row in _rows(html):
        cells = _texts(row)
        if len(cells) < 5:
            continue
        date_match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", cells[1].strip())
        if date_match:
            day = datetime(*(int(g) for g in date_match.groups()), tzinfo=tz)
        else:
            day = _parse_ddmon(cells[1], tz, reference)
        if day is None:
            continue

        pairing = cells[2].strip()
        start = _apply_hhmm(day, cells[3])
        end = _apply_hhmm(day, cells[4])
        if start is None or end is None or not pairing:
            continue
        if end <= start:
            end += timedelta(days=1)

        out.append(
            Shift(id=pairing, start=start, end=end, title=f"{pairing} (assigned)",
                  meta={"assigned": True})
        )
    return out


def parse_request_statuses(html: str) -> dict[str, str]:
    """Map pairing id -> status from the requests page.

    This is the authoritative outcome. "Unable" and "Cancelled" show up here
    after the fact, not in the response to the add itself, so this is what the
    three-strike rule reads.
    """
    statuses: dict[str, str] = {}
    for row in _rows(html):
        cells = _texts(row)
        if len(cells) < 4:
            continue
        status = cells[2].strip()
        if not status or status.lower() == "status":
            continue

        # The pairing id lives in the cancel checkbox's value attribute, which
        # is why cells carry `value` at all - it appears in no visible text.
        pairing = next(
            (c.value for c in row if c.value and re.fullmatch(r"[A-Z0-9]{4,6}", c.value)), None
        )
        if not pairing:
            pairing = next(
                (c.text.strip() for c in row if re.fullmatch(r"[A-Z0-9]{4,6}", c.text.strip())), None
            )
        if pairing:
            statuses[pairing] = status
    return statuses


def status_outcome(status: str) -> ClaimOutcome:
    lowered = (status or "").strip().lower()
    if lowered in AWARDED_STATUSES:
        return ClaimOutcome.CLAIMED
    if lowered in FAILED_STATUSES:
        return ClaimOutcome.REJECTED
    return ClaimOutcome.ERROR


def _headless_shell_available() -> bool:
    """Whether Playwright's separate headless binary is present.

    Only meaningful when browsers come from an explicit folder, which is how
    the packaged app ships them. A source install resolves through Playwright's
    own default location and is assumed complete.
    """
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not root:
        return True
    return any(Path(root).glob("chromium_headless_shell*"))


@register("flica")
class FlicaAdapter(PortalAdapter):
    """Browser-driven FLICA adapter.

    The browser layer is intentionally thin; everything interesting is in the
    parsers above, which are fully tested against fixtures.
    """

    OPENTIME = "otopentimepot.cgi"
    REQUESTS = "otrequest.cgi"
    SCHEDULE = "cmschedules.cgi"

    def __init__(self, config, http=None, secrets=None) -> None:
        super().__init__(config, http, secrets or {})
        self._page: Any = None
        self._context: Any = None
        self._playwright: Any = None
        self._position_cache: dict[str, str | None] = {}
        self._tz = config.availability.timezone

    # --- browser lifecycle --------------------------------------------------

    async def start(self) -> None:
        """Open a persistent browser profile and land on the portal.

        Persistent rather than fresh: the session then survives restarts, so she
        signs in (and clears any captcha) once rather than every time the agent
        is relaunched.

        Headed by default. FLICA presents a reCAPTCHA and the agent will not
        solve it, so a human has to be able to see and click the window. A
        headless run would simply stall at the challenge forever.
        """
        from playwright.async_api import async_playwright

        options = self.config.portal.options or {}
        profile = Path(options.get("browser_profile") or (paths.profile_dir(self.config.name) / "browser"))
        profile.mkdir(parents=True, exist_ok=True)

        headless = bool(options.get("headless", False))
        if headless and not _headless_shell_available():
            # Headless uses a separate `chromium_headless_shell` binary. The
            # packaged app ships only full Chromium, because this portal shows a
            # captcha that a human has to click - a headless run would stall at
            # it forever. Fall back rather than dying on a missing binary.
            log.warning("headless requested but the headless shell is not bundled; running headed")
            headless = False

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

        url = self.config.portal.base_url
        if url:
            try:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as exc:
                log.warning("initial navigation failed: %s", scrub(exc))

    # --- browser plumbing ---------------------------------------------------

    async def _frame_html(self, needle: str) -> str | None:
        """HTML of the first frame whose URL contains `needle`.

        Frame-aware because FLICA nests its content; the top-level document
        contains no tables at all.
        """
        if self._page is None:
            return None
        for frame in self._page.frames:
            if needle in (frame.url or ""):
                try:
                    return await frame.content()
                except Exception:
                    continue
        return None

    async def is_authenticated(self) -> bool:
        if self._page is None:
            return False
        html = await self._frame_html(self.OPENTIME)
        return bool(html) and not has_captcha(html or "")

    async def login(self) -> AuthResult:
        """Open the portal and hand any challenge to the human.

        No captcha solving, ever. A flagged account on a crew system is a
        disciplinary matter, not a retry-tomorrow inconvenience.
        """
        if self._page is None:
            return AuthResult(AuthState.FAILED, "no browser session attached")
        try:
            html = await self._page.content()
        except Exception as exc:
            return AuthResult(AuthState.FAILED, f"could not read page: {exc}")

        if has_captcha(html):
            return AuthResult(
                AuthState.NEEDS_HUMAN,
                "FLICA is showing a verification challenge.",
                challenge_url=self._page.url,
            )
        if await self.is_authenticated():
            return AuthResult(AuthState.OK)
        return AuthResult(AuthState.NEEDS_HUMAN, "Sign in to FLICA, then send /resume.",
                          challenge_url=self._page.url)

    # --- data ---------------------------------------------------------------

    async def fetch_open_shifts(self) -> list[Shift]:
        html = await self._frame_html(self.OPENTIME)
        return parse_open_shifts(html, self._tz) if html else []

    async def fetch_my_schedule(self) -> list[Shift]:
        html = await self._frame_html(self.SCHEDULE)
        return parse_schedule(html, self._tz) if html else []

    async def enrich(self, shift: Shift) -> Shift:
        """Fetch the position from the pairing detail page, cached by id."""
        pid = shift.meta.get("pairing_id") or shift.id
        if pid in self._position_cache:
            position = self._position_cache[pid]
        else:
            url = shift.meta.get("detail_url")
            if not url or self._page is None:
                return shift
            page = await self._page.context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded")
                position = parse_position(await page.content())
            finally:
                await page.close()
            self._position_cache[pid] = position

        if position is None:
            return shift
        return Shift(
            id=shift.id, start=shift.start, end=shift.end, title=shift.title,
            location=shift.location, meta={**shift.meta, "grade": position},
        )

    async def claim(self, shift_id: str) -> ClaimResult:
        """Select the pairing and submit the add request.

        The portal accepts the request without deciding it. The real outcome
        appears later in the Status column, which `check_outcome` reads.
        """
        if self._page is None:
            return ClaimResult(ClaimOutcome.ERROR, "no browser session attached")
        frame = next((f for f in self._page.frames if self.OPENTIME in (f.url or "")), None)
        if frame is None:
            return ClaimResult(ClaimOutcome.ERROR, "open-time frame not found")
        try:
            await frame.click(f'a[href*="PID={shift_id}"]')
            await frame.click('input[name="btnAdd"]')
        except Exception as exc:
            return ClaimResult(ClaimOutcome.ERROR, f"could not submit request: {exc}")
        return ClaimResult(ClaimOutcome.CLAIMED, "request submitted; awaiting status")

    async def check_outcome(self, shift_id: str) -> ClaimResult:
        html = await self._frame_html(self.REQUESTS)
        if not html:
            return ClaimResult(ClaimOutcome.ERROR, "requests page unavailable")
        status = parse_request_statuses(html).get(shift_id)
        if status is None:
            return ClaimResult(ClaimOutcome.ERROR, "no request found")
        return ClaimResult(status_outcome(status), status)

    async def close(self) -> None:
        self._position_cache.clear()
        for resource, closer in ((self._context, "close"), (self._playwright, "stop")):
            if resource is None:
                continue
            try:
                await getattr(resource, closer)()
            except Exception as exc:
                log.debug("cleanup failed: %s", scrub(exc))
        self._context = self._playwright = self._page = None
