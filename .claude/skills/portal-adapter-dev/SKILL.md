---
name: portal-adapter-dev
description: Guidance for building or reviewing a new PortalAdapter (a second employer/portal, e.g. a CNA shift board) in this repo, and for handling login friction (captcha, MFA, OTP) encountered while building one. Use when writing a PortalAdapter subclass, touching adapters/base.py or adapters/flica.py, or deciding how a new adapter should handle a captcha/verification challenge.
---

# Building a portal adapter

`src/shift_agent/adapters/base.py` documents the contract: everything above
the adapter layer (scheduling, polling, notification, storage, dashboard,
packaging) is job-agnostic. Adding a second job — a CNA portal, a different
airline — means writing exactly one `PortalAdapter` subclass and nothing
else. `adapters/flica.py` is the reference implementation.

## The one hard rule

Quoted verbatim from `adapters/base.py`'s `login()` docstring:

> Return `AuthState.NEEDS_HUMAN` with a `challenge_url` when blocked by a
> captcha, MFA prompt, or anything else requiring the user. Do not attempt to
> defeat such a challenge; the poller will hand off to the human.

**Never call into `shift_agent.friction` from an adapter's auth path** —
`login()`, `start()`, or anywhere else in a `PortalAdapter` subclass —
without a new, explicit, written decision recorded in both `docs/SECURITY.md`
and the adapter's own module docstring. This is not a placeholder rule
waiting to be relaxed; it's the same policy `adapters/flica.py` already
implements for FLICA, and it applies to every future adapter by default.

`tests/test_friction_boundary.py` enforces this mechanically: it fails the
moment any file under `adapters/` so much as mentions `friction`. If that
test fails, the fix is almost never "update the test" — it's "don't do that,"
or, if there truly is a considered reason to change the policy for a specific
portal, write that decision down first.

## Where the friction toolkit lives, and what it's actually for

`src/shift_agent/friction/` (see `docs/FRICTION_TOOLKIT.md`) is a
screenshot → vision-model → structured-action loop plus an IMAP OTP reader,
shipped built into the app via `shift-agent friction-bench` and
`friction-set-vision-key`/`friction-set-imap-password`. It exists for
**prototyping and benchmarking friction-handling outside a live adapter** —
e.g. proving the vision loop can clear a public reCAPTCHA demo page, or
reading a code out of a test mailbox — not for embedding into one.

If a new portal's recon findings show friction that seems like a natural fit
for this toolkit (see `docs/RECON-CHECKLIST.md`'s MFA questions for the kind
of case this is for), that's a reason to *write down a decision*, not to
import it directly.

## What "an explicit written decision" means in practice

At minimum, before any adapter imports `shift_agent.friction`:

1. A dated, reasoned entry in `docs/SECURITY.md` explaining which portal,
   which challenge type, and why the human-hand-off default doesn't apply.
2. A matching note in the adapter's own module docstring (see how
   `adapters/flica.py`'s docstring documents its own auth constraints today).
3. An update to this skill file acknowledging the exception, so the next
   adapter author — human or Claude session — sees it before writing new
   code, rather than discovering it by tripping the boundary test.

No such decision exists today. Every current adapter (`flica`, `mock`) hands
off to the human on any challenge, full stop.
