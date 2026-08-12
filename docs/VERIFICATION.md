# Verification record

What was actually executed, what passed, and — the part that matters more — what
was **not** proven.

Run 2026-08-12. Windows 11, Python 3.14.5. Server checks in a clean
`ubuntu:24.04` container. No portal, no account, and no network beyond loopback
at any point.

---

## Summary

| Area | Result |
|---|---|
| README and INSTALL.md commands on Windows | All pass. One factual correction |
| Dashboard behaviour claimed in the README | All pass |
| Dashboard server security properties | All pass |
| VPS path before this round | **Broken.** Two blockers, both now fixed |
| VPS provisioning script on Ubuntu 24.04 | Passes, idempotent over three runs |
| Live FLICA sign-in | **Not tested.** Needs her account — see below |

---

## Documentation, on Windows

Each command run verbatim from a fresh `git clone` into an empty directory.

| Step | Source | Result |
|---|---|---|
| `git clone` | INSTALL.md | Pass |
| `python -m venv .venv` | INSTALL.md | Pass |
| `pip install -e ".[dev]"` | INSTALL.md | Pass, 29 packages |
| `playwright install chromium` | INSTALL.md | Pass |
| `pytest -q` | README | Pass — **220 tests, not 218** |
| `main demo` | README | Pass. No config, credentials or network, as claimed |
| `cp example.yaml me.yaml` | INSTALL.md | Pass |
| `main dashboard --config` | INSTALL.md | Pass |
| `main run --config --dry-run` | INSTALL.md | Pass. Correctly skipped for `quiet_hours` |
| `tools/build_demo.py` | CI | Pass, 31 KB, no unreplaced placeholder |

**Correction applied:** the README said 218 tests. The suite was 220 before this
round's changes and is 231 after. The README now states 231.

**Confirmed as written:** INSTALL.md's troubleshooting entry about Windows'
260-character path limit. The first clone attempt failed on exactly that, in
exactly the documented way, and the documented fix — a shorter path — worked.

---

## Dashboard

Driven with Playwright against `tools/build_demo.py` output (invented shifts)
served on `127.0.0.1`.

| README claim | Result |
|---|---|
| Three tabs — Overview, Settings, Calendar | Pass. All three switch and carry content |
| Four colour themes | Pass — contrail, departure, jetway, night |
| Remembers your theme | Pass. Survives reload via `localStorage` |
| Per-shift verdict and reason | Pass. Claimed / Matched / Alert only / Gave up, each with its reason |
| Calendar month grid | Pass. 42 cells with month navigation |
| Agenda list on a phone | Pass at 375 px — grid goes `display:none`, agenda shows 12 items, no sideways scrolling |
| Exports `.ics` | Pass. Valid `VCALENDAR`, 12 `VEVENT`s, `text/calendar` |
| Copy as markdown needs HTTP | Pass. 818 characters copied over `http://` |

### Server security

| Claim | Test | Result |
|---|---|---|
| Binds loopback only | `netstat` and a request to the LAN address | `127.0.0.1:8799` only; LAN address refused |
| Random token required | Request with no token | 404 |
| Token cannot be probed | Request with a wrong token | 404, identical to a missing file |
| No path traversal | `../../../../Windows/win.ini` | 404 |
| Valid URL works | Token path | 200 |

**Cosmetic finding, not fixed:** browsers request `/favicon.ico`, which sits
outside the token path and so returns 404, logging one console error. Harmless —
correct behaviour from the token check.

---

## The two VPS blockers

Both were found by reading the code, then reproduced and fixed.

### 1. Headed browser on a machine with no screen

`FlicaAdapter.start()` launches Chromium **headed** on purpose: FLICA shows a
reCAPTCHA and this agent will not solve one. A stock Ubuntu server has no
display, so following the old INSTALL.md produced a service that crash-looped
with Playwright's internal launch output.

- **Fixed:** `_require_display()` refuses early with a plain-English message
  naming the command that fixes it. Linux-only; Windows and macOS are untouched.
- **Verified:** running the agent on Linux with no `DISPLAY` now prints the
  guidance and exits, with no traceback.
- **Verified the real path works:** under Xvfb, headed Chromium
  151.0.7922.34 launched, rendered, and reported an X screen. Captured as a
  screenshot from inside the container.
- **Verified end to end:** the exact command printed by the setup script started
  the FLICA adapter under `DISPLAY=:99`, opened the browser, and completed a
  poll cycle.

### 2. No keychain on a headless server

`secrets.py` went straight to `keyring`, which has no working backend on a
headless Linux box, and `main.py` called it outside the scrubbed error handler —
so the agent died at startup with a raw Python traceback.

- **Fixed:** on a machine with no keychain, secrets fall back to `secrets.json`,
  created `0600` inside a `0700` directory. Windows and macOS still use the OS
  keychain, asserted by a test so it cannot silently regress.
- **Fixed:** both `secrets` call sites in `main.py` now fail gracefully — the
  notifier falls back to console output rather than taking the agent down.
- **Verified in the container:** backend resolves to `file`, round-trips a
  value, deletes it, and the file is `0600`.

**The fallback is not encrypted, and that is deliberate.** Encrypting it would
be theatre: the key would have to sit unattended on the same disk for the
service to restart without a human, and the browser profile beside it already
holds live portal cookies in plain files. On a single-purpose server the real
boundary is the Unix account plus full-disk encryption. Stated in
[SECURITY.md](SECURITY.md) rather than papered over.

---

## The VPS setup script

`scripts/setup-vps.sh` run in a clean `ubuntu:24.04` container.

| Check | Result |
|---|---|
| `bash -n` | Pass |
| shellcheck | Clean (one SC1091 info for sourcing `/etc/os-release`) |
| Full run on a clean box | Pass |
| Second and third runs | Idempotent — every step reports "already done" |
| Her edited config survives a re-run | Pass |
| VNC password survives a re-run | Pass |
| Saved browser sign-in survives a re-run | Pass |
| `systemd-analyze verify` on all three units | Pass |
| Every `ExecStart` binary exists and is executable | Pass |
| `pytest` inside the container | 231 passed |
| `main demo` inside the container | Pass |
| Firewall ports opened | **None** |

### VNC exposure

| Test | Result |
|---|---|
| Connect to `127.0.0.1:5901` | Connected, `RFB 003.008` |
| Connect to the box's own external address | **Refused** |

`-localhost` is doing what it claims. The only route in is an SSH tunnel.

### Two bugs the container caught

Both would have failed on her live server. Neither was visible by reading the
script.

1. **`adduser` does not exist** on a minimal Ubuntu image. Replaced with
   `useradd`, which is always present.
2. **The local-source install path deleted the virtual environment** on every
   re-run, so "safe to run again" was false. Now excluded at the source.

### Three bugs found in the printed instructions

Found by running the instructions rather than reading them:

1. The tunnel command said `ssh shiftagent@…`, but that account has no password
   by design, so the connection could never succeed. Now `root@…`.
2. `sudo -u shiftagent DISPLAY=:99 …` does not pass the variable through — sudo
   strips it. Now `sudo -u shiftagent env DISPLAY=:99 …`, and the corrected form
   was executed to confirm.
3. Step 1 told her to use `nano`, which is not guaranteed installed. Now
   installed by the script.

---

## What is NOT proven

Read this part before treating any of the above as "it works".

- **No live FLICA sign-in has ever run.** Nothing here touched a real portal or
  a real account. The parsers are tested against saved fixtures, and the browser
  layer is proven only as far as "Chromium starts, navigates, and returns page
  content".
- **The reCAPTCHA flow has not been performed once, end to end.** The mechanism
  is verified — virtual screen, remote view, tunnel — but nobody has yet clicked
  a real FLICA challenge through it.
- **How often the challenge returns from a datacenter address is unknown.** It
  is expected to be more often than from home. Nobody has measured it.
- **A real claim has never been submitted.** Every run above was `--dry-run`.
- **systemd was not run as PID 1.** The unit files pass `systemd-analyze verify`
  and each `ExecStart` was executed by hand, but no container ran them as real
  services. Restart-on-crash and start-on-boot are configured, not observed.
- **Telegram is verified only against a mocked Bot API.** No real bot token has
  been exercised.
- **The packaged Windows `.exe` was not rebuilt or run** in this round.

The first real test of the live path is her first sign-in. Keeping
`dry_run: true` for the first week is not caution for its own sake — it is the
only way that path gets exercised before anything is claimed on her behalf.
