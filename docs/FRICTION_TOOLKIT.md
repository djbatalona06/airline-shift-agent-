# The friction toolkit

A built-in, portal-agnostic capability for handling web-auth friction
generically — a vision-model action loop and an IMAP one-time-code reader.
It ships with the app; there is nothing extra to install.

**It is not used by FLICA, or by any portal adapter.** Read
[The boundary](#the-boundary-non-negotiable) before reaching for this.

## What this is

Three pieces, composable independently:

1. **Screenshot → vision model → structured action loop.** Show a vision
   model a screenshot of a page, get back one structured action (click, type,
   press a key, or declare done/failed), execute it, repeat. This is the core
   loop behind handling almost any visual web-auth challenge generically.
2. **IMAP one-time-code reader.** Poll a mailbox for a recent message
   matching a sender/subject filter and pull a numeric or alphanumeric code
   out of it with a regex.
3. **Everything above is portal-agnostic on purpose.** It knows nothing about
   FLICA, or any specific portal — it takes a Playwright `page` and a goal
   prompt, nothing more.

## Why it exists

Modern web services add friction at nearly every step — "are you human"
challenges, email verification, SMS/authenticator codes. Handling each one
with bespoke code doesn't scale; the pattern that generalizes is showing a
vision model what a human would see and letting it act, plus reading codes
out of a mailbox instead of a phone. This toolkit is that pattern, built as a
reusable capability for whichever future portal adapter needs a piece of it —
not aimed at FLICA specifically.

## Architecture

```
friction/actions.py        Action schema (click/type/key/wait/done/fail) + parse_action()
                           `key` is allow-listed; `text` is shape-checked only
friction/vision_client.py  VisionClient Protocol + AnthropicVisionClient
friction/vision_loop.py    run_vision_loop(): screenshot -> vision -> execute -> repeat
friction/imap_otp.py       extract_code() (pure) + fetch_latest_otp() (network)
friction/config.py         FrictionConfig/FrictionImapSettings + secret loading
friction/cli.py            secret-setup handlers used by main.py's subcommands
friction/bench_recaptcha.py the MVP benchmark
```

`VisionClient` is a `typing.Protocol`, not a concrete class — `vision_loop.py`
is tested against a hand-written fake with no network and no dependency on
the real `anthropic` client. `AnthropicVisionClient` imports `anthropic`
inside `__init__`, not at module top, so importing the loop or the schema
never has a side effect of touching the network or constructing a real API
client.

## Configuring secrets

Same mechanism as everything else in this app — the OS keychain, never a
file:

```bash
shift-agent friction-set-vision-key --user me
shift-agent friction-set-imap-password --user me   # only if you need IMAP OTP reading
```

Optional per-profile settings (vision model override, IMAP host/mailbox/
filters) go in `friction.yaml` next to your other profile data; the toolkit
works with just the vision key and no file at all.

```yaml
# %LOCALAPPDATA%\shift-agent\profiles\<you>\friction.yaml
vision_model: claude-sonnet-5
imap:
  host: imap.gmail.com
  username: you@example.com
  subject_contains: verification code
  since_minutes: 10
```

## Reading a one-time code

```bash
shift-agent friction-otp --user me
```

Waits for a message matching your `imap:` filters and prints the code, or exits
non-zero if nothing arrives in time (`--timeout-s`, default 120).

Age is enforced on the message's `Date` header, not by IMAP's `SINCE`, which is
only date-granular — without that check a six-hour-old code from the same day
would be returned as if it were fresh, and the login would then fail with no
useful diagnostic. A message whose date cannot be parsed is skipped rather than
accepted.

## Running the benchmark

```bash
shift-agent friction-bench --user me
```

Drives the vision loop against Google's public reCAPTCHA demo page
(`https://www.google.com/recaptcha/api2/demo`) — a legitimate, intentionally
solvable target, never a production login. Runs **headless by default**: no
human ever needs to watch it solve anything, since the vision model does the
"seeing." Prints `PASS`/`FAIL`, elapsed time, and step count; `PASS` within
five minutes is the MVP bar this toolkit was built to clear. Pass `--headed`
to watch it locally for debugging — no human input is required either way.

The bench raises the loop's own defaults, because an image challenge is a long
sequence of tile clicks and a vision call carrying a full-page screenshot
regularly runs past 15 seconds: 40 steps and a 60-second per-step ceiling, with
the 5-minute total as the real bound. Left at the defaults the run ends on its
own harness rather than on the challenge.

Every exit is a reported result, not a traceback. A model reply that isn't
parseable JSON, a hung screenshot, and an unreachable API all end the run with
a `FAIL` line explaining which one happened.

## The boundary (non-negotiable)

From [`adapters/base.py`](../src/shift_agent/adapters/base.py)'s `login()`
contract, unchanged by anything in this document:

> Return `AuthState.NEEDS_HUMAN` with a `challenge_url` when blocked by a
> captcha, MFA prompt, or anything else requiring the user. Do not attempt to
> defeat such a challenge; the poller will hand off to the human.

Nothing in `friction/` is imported by `adapters/`, and nothing in `friction/`
imports from `adapters/`. `tests/test_friction_boundary.py` enforces this —
it fails the build the moment any adapter module so much as mentions
`friction`. FLICA's captcha hand-off (`docs/SECURITY.md`'s "Captchas and bot
detection") is completely unaffected by this toolkit shipping in the app.

Wiring the friction toolkit into any live `PortalAdapter.login()` would
require a new, explicit, written decision — recorded in `docs/SECURITY.md`
and the adapter's own module docstring — never a side effect of this toolkit
being installed by default. See
[`.claude/skills/portal-adapter-dev/SKILL.md`](../.claude/skills/portal-adapter-dev/SKILL.md)
for the guidance a future adapter author (human or Claude session) should
follow.

## What this doesn't cover

Clearing a public demo page proves the loop mechanics work — screenshot,
reasoning, action execution. It does **not** prove this defeats a production
adversarial system:

- **Adaptive reCAPTCHA (v3 / Enterprise)** scores a session on behavior over
  time, silently. Solving an image challenge doesn't touch that score —
  defeating it would need realistic fingerprinting and human-like interaction
  patterns, a different and harder problem than this toolkit addresses.
- **Cloudflare Turnstile** is similarly behavior-based with no image
  challenge to solve.
- **Rate limits.** Services that watch for many challenges solved quickly may
  throttle or flag the account regardless of how each individual challenge
  was cleared. This toolkit does not add pacing/jitter on its own — anything
  that uses it should.

## Testing philosophy

`tests/test_friction_*.py` are pure-function/fixture tests — `parse_action()`
JSON handling and the `key` allow-list, `extract_code()` regex extraction and
`_is_recent()` age filtering, `FrictionConfig` validation, and
`run_vision_loop()` against a fake page and a fake `VisionClient` (including
its unparseable-reply and hung-browser exits). None of them touch the network, a real browser, or the
`anthropic` API, matching `.github/workflows/ci.yml`'s "no network, no
browser" rule for the whole suite. `friction-bench` itself is a manual-run
tool — it needs a real API key and hits a live public page, so it is never
invoked by `pytest`.
