# Surviving reCAPTCHA on a 24/7 agent

FLICA shows a "confirm you are human" box, and this agent will never click it
for you. That is a deliberate refusal, not a missing feature — captcha-solving
services are how accounts get flagged, and a flagged account on a crew system is
a disciplinary matter rather than a retry-tomorrow inconvenience.

So the question is not "how do we solve it". It is **how often does it appear,
and how quickly do we recover when it does**. Everything below is about
lowering the first number and the second one.

---

## What changed

Two things were wrong before, and both are now fixed.

**A challenge pause never cleared itself.** The pause is persistent and is
checked before anything else in the cycle, so once a challenge appeared the
agent woke every ~45 seconds, saw `paused`, and returned immediately — forever,
until a human sent `/resume`. If you cleared the challenge in the browser and
did not also message the bot, the agent sat there indefinitely. On an unattended
box that is the difference between a two-minute interruption and a silent outage
lasting until someone happened to look.

Now the agent re-probes on a widening interval: 60s, then 120s, 240s, up to 15
minutes. If the session is live again it resumes on its own and says so. The
Telegram hand-off is unchanged, so you still get told immediately — you just no
longer *have* to answer for the agent to recover.

The backoff matters as much as the probe. Re-probing every 45 seconds against a
portal that just challenged you is precisely the traffic that earns another
challenge.

**A manual pause could be undone by it.** A stale challenge flag meant `/pause`
could be silently reversed by the recovery logic. A human pausing now always
outranks the agent's own recovery. Pinned by
`test_manual_pause_is_not_undone_by_a_stale_challenge_flag`.

A failed-login pause is deliberately **not** self-recoverable. Retrying a bad
password is how an account gets locked, which is far worse than any missed shift.

---

## Reducing how often you are challenged

Ordered by how much they actually matter.

### 1. Where your traffic comes from — by far the biggest lever

Datacenter IP ranges are the single strongest bot signal most protection
products use. A VPS is, by definition, a datacenter address. `docs/VPS.md`
already warns about this, and it deserves restating: **the same agent doing the
same things gets challenged more from a VPS than from a home connection.**

In rough order of preference:

1. **Run it on a machine at home.** A mini PC or an always-on laptop keeps a
   residential address and removes the problem rather than mitigating it. This
   is genuinely the best answer, and it is cheaper than a VPS.
2. **Tunnel the VPS's browser traffic through home.** A WireGuard tunnel from
   the VPS to a home router means the portal sees a residential address while
   the agent still runs somewhere that survives a power cut. More moving parts,
   and the tunnel becomes a dependency — if it drops, the agent is suddenly
   browsing from the datacenter again.
3. **Accept it and optimise recovery.** Expect challenges, keep the remote
   viewer set up, and lean on the self-resume above.

Note what is *not* on this list: residential proxy services. They are sold for
exactly this purpose, they are frequently made of compromised consumer devices,
and routing an employer's crew credentials through one is a bad idea on both
security and employment grounds.

### 2. Session longevity

The best-behaved session is one that never has to sign in again. The adapter
uses `launch_persistent_context` with a real on-disk Chromium profile, so
cookies and a cleared challenge survive restarts — this part was already right,
and `test_session_survives_a_browser_restart` now proves it.

Things that quietly throw that away, worth avoiding:

- Deleting or moving the profile directory (`~/.shift-agent/profiles/<name>/browser`).
- Running two agents against the same profile — Chromium will not share it.
- Clearing cookies "to fix" something. It will cause a fresh challenge.

Back the profile directory up before you touch it.

### 3. Browser fingerprint consistency

Currently the launch passes exactly one argument,
`--disable-blink-features=AutomationControlled`, and no context options at all —
no user agent, no viewport, no locale, no timezone.

The default is not obviously wrong: Playwright's bundled Chromium with a real
persistent profile looks fairly ordinary, and a headed browser under Xvfb looks
more ordinary still. But two things are worth setting on a server, because they
are the mismatches most likely to be noticed:

- **Locale and timezone matching your base.** A browser reporting UTC while the
  account is based in Orlando is an easy inconsistency to spot.
- **A viewport that matches the virtual screen.** Xvfb's default geometry and the
  browser's window size should agree.

What not to do: install a stealth plugin, spoof a different browser, or rotate
user agents. Fingerprint-evasion tooling is exactly the "arms race" this project
declines to enter, and an *inconsistent* fingerprint is a stronger bot signal
than a plain one.

### 4. Request rhythm

Already reasonable, and worth not making worse:

- Polling is jittered (45s ± 15s) so requests are not perfectly periodic.
- Quiet hours stop overnight traffic entirely.
- Failures back off exponentially to a 15-minute cap.
- Since the staleness fix, each cycle reloads one frame rather than none —
  slightly more traffic than before, but it is the traffic that makes the agent
  work at all. The assigned-schedule frame is deliberately *not* reloaded each
  cycle: a roster changes on the scale of days, and a second reload per cycle
  would double the footprint for nothing.

If you are being challenged often, **raise `poll.interval_seconds` before trying
anything else.** 45 seconds is aggressive for a portal that updates in minutes;
90 or 120 halves or thirds your request count and will rarely cost you a shift.

### 5. Fast hand-off when it does happen

The remaining cost of a challenge is how long it takes you to notice. Telegram
already carries the alert with the challenge URL, and health events are now
marked `🩺` so a stalled agent is visually distinct from a shift offer.

Worth having set up *before* you need it: the SSH tunnel and VNC viewer from
`docs/VPS.md`, tested once so you are not learning it at 5am.

---

## Recommended configuration for 24/7

```yaml
poll:
  interval_seconds: 90       # gentler than the 45s default
  jitter_seconds: 30
  quiet_hours:
    start: "23:00"
    end: "05:00"

rules:
  max_login_failures: 3      # never retry a bad password indefinitely

dry_run: true                # for the first week, at minimum
```

Plus, in order: run it at home if you possibly can; keep the browser profile
intact; set up the remote viewer once and check it works.

---

## What is still unmeasured

Being honest about the load-bearing unknown: **nobody has measured how often
FLICA challenges this agent, from either a home or a datacenter address.** The
whole operational design rests on the assumption that a persistent session plus
gentle polling produces an occasional challenge rather than a constant one. That
assumption is reasonable and it is still an assumption.

The first week of real running is what turns it into a number. Worth recording:
how many challenges, at what times of day, and whether they cluster after
restarts. If they turn out to be frequent from a VPS, that is the evidence for
moving to option 1 or 2 above rather than tuning anything further.
