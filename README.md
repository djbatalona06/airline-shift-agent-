# Shift Agent

A plug-and-playable agent that picks up first-come, first-served shifts from
your employer's portal.

It watches for open shifts, checks each one against your saved availability, and
**asks before it claims anything**.

Built for shift workers who lose good shifts because they were asleep when the
shift was posted. It is deliberately not a "grab everything" bot: it checks
schedule conflicts, minimum rest, home base, and position, and by default it
asks you to confirm before it takes anything.

Job-agnostic by design. Adding a new employer means writing one adapter class;
everything else — scheduling, notifications, dashboard, packaging — is shared.

**Status:** core complete, 220 tests. The FLICA adapter's parsers are tested
against fixtures; the live browser path has not yet run a full week against a
real portal.

---

## What it does

- **Watches** the portal on a jittered interval, only during hours you choose.
- **Filters** every shift against your availability, existing trips, minimum
  rest, weekly caps, home base, and position class.
- **Asks first.** By default it sends the shift to Telegram with Confirm / Skip
  buttons. It only claims on an explicit yes. Silence is never consent.
- **Gives up sensibly.** Three failed attempts on the same shift and it stops.
- **Shows you what happened** in a dashboard — what it saw, what it skipped and
  why, and what it picked up.

### What it deliberately does not do

- **It does not solve captchas.** When the portal challenges, it pauses and asks
  you. Captcha-solving services are how accounts get flagged, and a flagged
  account on a crew system is a disciplinary matter.
- **It does not model legality.** It enforces plain scheduling hygiene, not
  FAA duty limits or nursing ratios. The portal is authoritative; a
  half-correct legality model would be worse than none.
- **It never changes your home base.** Picking up from the wrong domicile means
  being rostered out of a city you do not live in.

---

## Two ways to run it

| | Desktop app | Localhost |
|---|---|---|
| For | Anyone. No technical setup | Developers, or running on a server |
| Needs | Nothing — Windows only | Python 3.11+ and git |
| Get it | Download the release, unzip, run | Clone and `pip install -e .` |

Full instructions, including a VPS for 24/7 operation:
**[docs/INSTALL.md](docs/INSTALL.md)**

### Quickest look

```bash
python -m shift_agent.main demo
```

Runs the whole pipeline on fabricated data — no config, credentials, or network.

---

## The dashboard

Three tabs — Overview, Settings, Calendar — with four colour themes and a
theme switcher that remembers your choice.

- **Overview** — status, counts, and a card per shift showing its position grade
  and why it was or wasn't taken.
- **Settings** — every rule currently in force, read-only.
- **Calendar** — month grid, or an agenda list on a phone. Exports `.ics` for
  Apple Calendar, Outlook and Google Calendar, and markdown for Obsidian or
  Notion.

It is served on `127.0.0.1` with a random token in the URL, never on a public
interface.

---

## Safety model

| Rule | Behaviour |
|---|---|
| Confirm before claiming | Default. Auto-claim exists but stays off until a dry run has proven the matcher |
| Home base lock | Only shifts at your base. Unknown base is refused, not assumed |
| Position grades | Pursue the ones you list; everything else alerts only |
| Three strikes | Stop after three failed attempts on one shift |
| Captcha | Paused, handed to you |
| Login failures | Stops after three, so a changed password cannot lock your account |
| Dry run | Evaluates and notifies without claiming anything |

Everything unknown fails **closed**: an unreadable grade, a missing base, or an
unreachable detail page results in an alert, never a claim.

---

## Privacy

- **Credentials never touch this software.** You sign in yourself; it works with
  the session you create. Passwords are not stored, typed, or transmitted by it.
- Anything it does store goes in the **OS keychain** (Windows Credential
  Manager), not in a file.
- The database and dashboard hold your schedule and **no secrets**.
- Nothing is sent anywhere except the portal you configure and, if you set it
  up, your own Telegram bot.

See [docs/SECURITY.md](docs/SECURITY.md).

---

## Configuration

Copy [`config/users/example.yaml`](config/users/example.yaml) and edit. It is
commented throughout. The essentials:

```yaml
availability:
  timezone: America/New_York
  slots:
    - { day: Monday, start: "08:00", end: "17:00" }

rules:
  min_rest_hours: 10
  max_claim_attempts: 3

home_base:
  code: MCO          # only shifts based here

grades:
  pursue: [E, D, B]  # claim these
  notify_only: [A, C] # just tell me

claim_mode: confirm
dry_run: true        # start here
```

---

## Supported portals

| Adapter | Status |
|---|---|
| `flica` | Parsers tested against fixtures; live path unproven |
| `mock` | Fabricated data, for demos and tests |

Adding one means implementing five methods on `PortalAdapter`. See
[`src/shift_agent/adapters/base.py`](src/shift_agent/adapters/base.py).

---

## Development

```bash
git clone https://github.com/djbatalona06/airline-shift-agent-.git
cd airline-shift-agent-
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m playwright install chromium
.venv/Scripts/python.exe -m pytest -q
```

Tests run with no network, no browser, and no account.

---

## Legal and employment

Automated shift pickup may be restricted by your employer's terms, your union
agreement, or the portal's terms of service. **Check before you use this.** You
are responsible for how you use it. Confirm-before-claiming keeps a human
approving each pickup, which reduces but does not remove that exposure.

Not affiliated with, endorsed by, or connected to any airline, healthcare
provider, or scheduling vendor.

MIT licensed.
