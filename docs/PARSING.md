# Parsing status

What the FLICA adapter reads, what it does with markup it does not recognise,
and what is still unproven. Written after building the Playwright sandbox in
`tests/e2e/`, which is what turned several of the entries below from "probably
fine" into "was broken, now fixed and pinned by a test".

**Standing caveat, unchanged:** no live sign-in to a real FLICA account has ever
run. Everything below is proven against fixtures and against a fake portal on
loopback. See [VERIFICATION.md](VERIFICATION.md).

---

## The four parsers

Every parser is a pure function from an HTML string to domain objects. That
split is deliberate: the whole data path is testable with no browser, no
network and no account, and the browser layer only fetches text and hands it
over.

Parsing uses the standard library's `html.parser` rather than lxml or
BeautifulSoup, to keep the frozen executable small. `_Table` is deliberately
forgiving — FLICA is a CGI application whose markup is not guaranteed
well-formed, and a parser that raised on a stray tag would take the agent down
over cosmetics.

### 1. `otopentimepot.cgi` — the open time pot

`parse_open_shifts(html, timezone, reference=None)`

Eleven columns, read positionally:

| # | Column | Used for |
|---|---|---|
| 0 | Pairing | `Shift.id`, and the `PID=` in its href becomes `meta["pairing_id"]` |
| 1 | Dates | Start date, via `_parse_ddmon` |
| 2 | Days | Multi-day span — `end += span - 1` days |
| 3 | Report | Start time |
| 5 | Arrive | End time |
| 6 | Blk Hrs | `meta["block_hours"]` |
| 7 | Credit | `meta["credit"]` |
| 9 | Layover | `Shift.location`, with `-` normalised to None |
| 10 | Prem | `meta["premium"]` |

Also `parse_selected_base(html)`, which reads the `baseList` selector
**read-only**. The agent must never write it: picking up from the wrong domicile
means being rostered out of a city you do not live in. `tests/test_flica.py:208`
asserts the adapter's own source text contains no `select_option` or
`baseList` write, which is a blunt guard that has so far held.

Two heuristics worth knowing about:

- **Year inference.** FLICA omits the year. `_parse_ddmon` assumes the next
  occurrence, with a seven-day grace so a pairing that started yesterday
  resolves to this year rather than next.
- **Midnight crossing.** If the computed end is at or before the start, a day is
  added.

### 2. `RBCPair.cgi` — the pairing detail page

`parse_position(html)` returns the position grade, A–E.

Two strategies, in order. First an explicit `Open Position:` label, either in
the same cell or the next one. Failing that, the crew-complement string
(`FA01FB01FC01FD01FE01`) — but **only when every letter in it is the same**.
That restriction matters: the complement describes the whole crew, not the seat
being offered, so reading it as the offer when several letters are present would
be a guess. A wrong guess puts you in a seat you did not ask for.

Returns `None` when it cannot tell, which the poller turns into alert-only.

### 3. `cmschedules.cgi` — your assigned trips

`parse_schedule(html, timezone, reference=None)`. Accepts either an ISO date or
the `14AUG` form. Feeds conflict and rest checks.

### 4. `otrequest.cgi` — request outcomes

`parse_request_statuses(html)` → `{pairing: status}`, then `status_outcome`
maps that to a `ClaimOutcome`.

The pairing id on this page exists **only** inside a checkbox `value` attribute,
never in visible cell text — which is why `Cell` carries a `value` field at all.

---

## Fail-closed behaviour

The rule throughout: anything unreadable becomes an alert, never a claim.

| Situation | Result |
|---|---|
| Grade will not parse | `GRADE_NOTIFY_ONLY` — told about, never claimed |
| Detail page unreachable | Same. `enrich` raising is explicitly safe |
| Base missing or unknown | Refused, not assumed |
| Premium flag unrecognised | Not premium (**fixed** — see below) |
| Ambiguous crew complement | `None`, so alert-only |
| Row will not parse | Skipped |
| Confirmation times out | Treated as no. Silence is never consent |
| Three failed attempts | Stop trying that shift (**now actually works** — see below) |

---

## Defects found and fixed

These were all in the browser layer, which had **zero** test coverage before
`tests/e2e/`. Playwright is a declared runtime dependency that no test exercised;
CI explicitly refused to install Chromium. Each of these is now pinned by a test.

### Fixed

**1. Nothing ever re-navigated after start-up.** `fetch_open_shifts` read
whatever frame HTML happened to exist — no `goto`, no `reload`. Every poll cycle
re-parsed the DOM captured at launch. A shift posted after the agent started
would never be seen, and the agent would report healthy cycles indefinitely while
doing nothing. This is the worst failure mode this project can have, because it
is completely silent. Now each cycle reloads the open-time frame before reading
it. Pinned by `test_a_second_fetch_sees_changed_data`.

**2. `check_outcome` was dead code.** Defined, never called. `claim()` returns
`CLAIMED` optimistically because FLICA accepts a request and decides later, so
every submitted request was recorded as a success. `failed_attempts` counts rows
where `outcome != 'claimed'`, so it could never rise — **the three-strikes rule
the README advertises did nothing on the real adapter.** The poller now
reconciles pending requests at the top of each cycle, replacing the optimistic
row rather than appending, so one real attempt costs exactly one strike. A
still-pending request returns `None`, which is neither a win nor a strike.

**3. `enrich` could never succeed.** FLICA's hrefs are relative
(`RBCPair.cgi?PID=...`). The adapter opened a blank page and navigated straight
to that string, which fails with "Cannot navigate to invalid URL". Every enrich
raised, and the poller's fail-closed path turned *every* shift into alert-only on
an unreadable grade. The agent would have run for a week claiming nothing and
looking fine. Links are now resolved against the frame they were scraped from.

**4. The requests page was only readable by luck.** `check_outcome` looked for a
live frame, which exists only if that tab happens to be open. It now falls back
to fetching the page in a background tab.

**5. `premium` failed open.** The rule was "premium unless blank, `-` or `N`", so
any unanticipated glyph read as premium — while `config.py` promises the exact
opposite: *"a shift whose premium flag cannot be read is skipped rather than
assumed premium"*. With `premium_only` on, that put shifts you did not want into
the claimable set. Now only known markers count, and anything else logs and reads
as not premium.

**6. Captcha markers were a subset of recon's.** The adapter knew four; `recon.py`
knows ten, including `challenges.cloudflare.com` and Arkose. A Cloudflare
challenge served without the `cf-turnstile` class read as "not signed in" rather
than "challenge" — the same pause either way, but with a message sending you to
hunt for a login form that was not there. Both now share one list, and a test
asserts the adapter covers everything recon knows about.

**7. A challenge pause never cleared itself.** See [CAPTCHA.md](CAPTCHA.md).

**8. Frames were read before they attached.** `domcontentloaded` on the outer
frameset fires before child frames exist, so a fetch immediately after start-up
found nothing and reported an empty pot — indistinguishable from a genuinely
empty one.

### Known, not fixed

**`claim()` is still unproven and probably wrong.** It clicks the pairing anchor
— which navigates to `RBCPair.cgi` — and then clicks `btnAdd` on a frame that
has just navigated away, with no `wait_for_navigation`. It also never returns
`LOST_RACE`, contradicting the contract in `adapters/base.py`. This cannot be
resolved without a live account: the correct sequence depends on what FLICA
actually does when that link is clicked. **Keep `dry_run: true` until this path
has been watched once.**

**Positional column indexes.** `cells[1]`, `[3]`, `[5]`, `[9]`, `[10]` with no
header-driven mapping. An inserted column silently shifts `premium` and
`location`. The `len(cells) < 11` guard only catches removals. A header-driven
mapping would be more robust; it is not written yet.

**Rows that fail to parse are dropped silently.** No log, no counter. A format
change therefore degrades to "no open shifts" rather than to an error — the
agent looks healthy and finds nothing. Worth a counter in the cycle report.

**`parse_schedule` ignores multi-day trips.** Unlike the open-time parser it
reads no `Days` column, so an assigned multi-day trip is treated as ending on
day one. Rest and conflict checks therefore under-estimate how long you are
actually away — exactly what `adapters/base.py` warns about.

**`login()`'s fall-through is indiscriminate.** Anything that is neither a
detected challenge nor a readable open-time frame returns `NEEDS_HUMAN` with
"Sign in to FLICA". A portal outage, an expired session and a stray navigation
all produce the same message.

**`docs/RECON-FINDINGS.md` does not exist.** The module docstring cites it as the
authority for every column layout, and it is gitignored. Anyone reading this
code cannot check the layouts against their source.

---

## What the fixtures represent

All four are **synthetic**, hand-written to mirror the recon findings, and
contain none of the portal's real markup — which is what makes them safe to
publish.

| Fixture | Represents |
|---|---|
| `otopentimepot.html` | The pot: `baseList` with 13 domiciles and MCO selected, the 11-column header, four pairing rows linking to `RBCPair.cgi`, and the `btnAdd` control `claim()` targets |
| `RBCPair.html` | Pairing detail: an explicit `Open Position: E` row, the crew complement `FA01FB01FC01FD01FE01`, and a two-leg day table |
| `cmschedules.html` | Assigned trips, plus a `Block 59.42` summary row that must be ignored |
| `otrequest.html` | Three requests — Pending, Unable, Awarded — with pairing ids only inside checkbox values |

None contains captcha markup, an error page, an expired session or a login form.
The e2e sandbox adds those three as generated pages, which is how the challenge
paths get exercised at all.

---

## What is still unproven

- **A real sign-in.** Never performed.
- **A real claim.** Never submitted. Every run has been `--dry-run`.
- **The real column layout.** Fixtures mirror recon notes that are not in the
  repository.
- **Whether FLICA's frames self-refresh.** The adapter now forces a reload, so
  this matters less than it did, but the portal's own behaviour is unknown.
- **How often a challenge appears from a datacenter address.** Expected to be
  more often than from home. Nobody has measured it. See [CAPTCHA.md](CAPTCHA.md).
