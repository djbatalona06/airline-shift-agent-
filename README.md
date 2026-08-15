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

**Status:** core complete, 356 tests, plus 21 browser-driven tests that run the
real adapter and the real dashboard against a fake portal on loopback. Every
command in this README and in
[docs/INSTALL.md](docs/INSTALL.md) has been executed and checked — see
[docs/VERIFICATION.md](docs/VERIFICATION.md) for what passed and, more usefully,
what is still unproven. The FLICA adapter's parsers are tested against fixtures;
**no live sign-in to a real portal has happened yet.**

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

## Three ways to run it

| | Desktop app | Localhost | Server, 24/7 |
|---|---|---|---|
| For | Anyone. No technical setup | Developers, or trying it out | Running it when your PC is off |
| Needs | Nothing — Windows only | Python 3.11+ and git | An Ubuntu box, 2 GB RAM, ~45 min |
| Get it | Download the release, unzip, run | Clone and `pip install -e .` | One setup script |
| Guide | [INSTALL.md](docs/INSTALL.md) | [INSTALL.md](docs/INSTALL.md) | **[VPS.md](docs/VPS.md)** |

**On picking the server option:** it is the only one that survives your computer
being off, and it costs a real trade. FLICA challenges the sign-in with a
"confirm you are human" box, this agent refuses to solve those, and a server has
no screen — so clearing one means opening a remote view rather than clicking
once. Datacenter addresses also get challenged *more* than home connections. If
you have a computer that can stay on, that is the easier answer.
[docs/VPS.md](docs/VPS.md) opens with the full comparison, then walks the whole
setup for someone who has never used Linux.

### Quickest look

```bash
python -m shift_agent.main demo
```

Runs the whole pipeline on fabricated data — no config, credentials, or network.

---

## The dashboard

Three tabs — Overview, Settings, Calendar — with four colour themes and a
theme switcher that remembers your choice, plus a chat bubble you can ask
questions.

- **Overview** — status, counts, and a card per shift showing its position grade
  and why it was or wasn't taken.
- **Settings** — every rule currently in force, read-only.
- **Calendar** — month grid, or an agenda list on a phone. Exports `.ics` for
  Apple Calendar, Outlook and Google Calendar, and markdown for Obsidian or
  Notion.

It is served on `127.0.0.1` with a random token in the URL, never on a public
interface. Verified: a missing token, a wrong token, and a path-traversal
attempt all return the same 404, and the port refuses connections from anything
but loopback.

### Asking it questions

The cards tell you *what* happened. The chat bubble tells you *why*, and will
answer a follow-up:

> **why did you skip M8W77?**
> It starts at 23:40 and your Friday window closes at 22:00.

It can read what the agent saw, skipped and picked up, explain the rules in
force, and propose a change to them — shown as a diff you press **Apply** on.
It cannot claim a shift, sign in, or clear a challenge. Same assistant answers
`/ask` in Telegram.

```bash
shift-agent set-llm-key        # stored in the OS keychain, never in a file
```

Claude by default. It also speaks to any OpenAI-compatible endpoint, and
pointing `llm.base_url` at a local Ollama means **nothing leaves your machine**
and no key is needed at all — see [docs/SECURITY.md](docs/SECURITY.md), which is
explicit about what a hosted model does and does not receive.

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
  Manager). A headless Linux server has no keychain, so there it falls back to a
  `0600` file owned by the service account — weaker, and
  [documented as such](docs/SECURITY.md#where-things-are-stored).
- The database and dashboard hold your schedule and **no secrets**.
- **The chat assistant is off until you set it up**, and if you enable it with a
  hosted model your questions and shift data go to that provider. A local model
  keeps everything on your machine.
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

`pytest -q` runs with no network, no browser, and no account.

The browser-driven tests are a second tier, deselected by default:

```bash
pytest -m e2e          # add xvfb-run -a on a headless Linux box
```

They start a fake FLICA on loopback — the real fixtures, plus the login,
challenge and expired pages the fixtures never had — and drive the **real**
adapter through Chromium: frames, the persistent profile, challenge detection
and recovery, and whether a poll cycle actually sees new data. The same tier
drives the dashboard's chat bubble against the real server and asserts the API
key reaches neither the DOM nor `localStorage`, and that nothing leaves
loopback. Building it found seven defects in the browser layer, listed in
[docs/PARSING.md](docs/PARSING.md).

The server path is verified in a throwaway Ubuntu container rather than by
reading the script:

```bash
docker run -d --name sa-test ubuntu:24.04 sleep infinity
```

then copy the repo in and run `scripts/setup-vps.sh` with
`SHIFT_AGENT_SOURCE` pointed at it. [docs/VERIFICATION.md](docs/VERIFICATION.md)
records what that run covers.

---

## Legal and employment

Automated shift pickup may be restricted by your employer's terms, your union
agreement, or the portal's terms of service. **Check before you use this.** You
are responsible for how you use it. Confirm-before-claiming keeps a human
approving each pickup, which reduces but does not remove that exposure.

Not affiliated with, endorsed by, or connected to any airline, healthcare
provider, or scheduling vendor.

MIT licensed.
