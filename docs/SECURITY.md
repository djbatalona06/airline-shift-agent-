# Security and privacy

## Credentials

**This software never receives your password.** You sign in yourself, on the
portal's own login page, in a browser window it opens for you. It then works
with the session you created. There is no field to type a password into and no
config key that holds one.

If a portal password has been pasted somewhere it shouldn't — a chat window, an
email, a note — change it. That applies regardless of this tool.

## Where things are stored

| Data | Location | Protection |
|---|---|---|
| Telegram bot token, API keys | OS keychain (Windows Credential Manager) | Encrypted against your Windows login |
| Shift history, verdicts, claims, chat | SQLite under `%LOCALAPPDATA%` | Filesystem ACLs. **No secrets** |
| **Portal browser session** | Chromium profile under `%LOCALAPPDATA%` | ACLs + Chromium's own DPAPI cookie encryption |
| Dashboard HTML, `.ics`, markdown | Same profile folder | Your schedule. No secrets |
| Config YAML | Wherever you put it | Username and availability. No password |

The split is enforced, not conventional: `store.py` is documented as holding no
secrets, and a test asserts that a built dashboard payload contains no password,
cookie, or token.

**The portal session is the exception worth understanding.** The FLICA adapter
signs in through a *persistent* Chromium profile at
`%LOCALAPPDATA%\shift-agent\profiles\<you>\browser`, so the live session lives
on disk as an ordinary browser profile rather than in the keychain. That is what
lets you clear a captcha once instead of every launch, and it is a real
tradeoff: another process running under your own Windows account could read that
profile, which is not true of the keychain. Treat a copied profile folder as a
live login to your crew account — do not put one in a backup you would share, a
zip, or a support thread.

## Per-user isolation

The same executable can be given to several people. Their data does not mix:

1. `%LOCALAPPDATA%` resolves per Windows account and is ACL-protected from other
   accounts on the machine.
2. Credential Manager entries are encrypted against the login that wrote them.
3. Within one account, each profile gets its own directory and database.

## Network

Outbound connections only, and only to:

- The portal you configure.
- `api.telegram.org`, if you set up a bot.
- Your healthcheck URL, if you set one.
- **`api.anthropic.com`, if you turn on the chat panel or use the friction
  toolkit.** See below.

There is no telemetry, and nothing is sent to the author.

The dashboard server **binds `127.0.0.1` only** and puts a random token in the
URL path. It is never exposed on a public interface, including for the chat
routes. On a VPS, reach it through an SSH tunnel rather than opening a port.

This is why chat history reaches your phone through Telegram rather than by
opening the dashboard to your network: your phone genuinely cannot connect to
the dashboard, and that is the intended property, not a limitation to work
around.

### The chat assistant sends your data to a third party

This is a real change to what this software used to promise, and it deserves
stating plainly rather than being buried.

Earlier versions sent nothing anywhere except the portal, Telegram and your
healthcheck. **If you enable the chat assistant with a hosted model, your
questions and the shift data needed to answer them — pairing ids, dates, times,
verdicts, your availability rules — are sent to that model's provider.** That is
not telemetry and it is not incidental; it is what makes the feature work. It is
still a third party receiving your roster.

Two things follow:

- **It is off unless you turn it on.** No key is stored by default, and with no
  key the Chat tab explains how to enable it instead of offering an input box.
  Nothing is sent, and `run` without `--dashboard` has no chat surface at all.
- **A hosted model is a considered trade, not a default you drift into.** You
  have to run `shift-agent friction-set-vision-key` to make it happen — the same
  key the friction toolkit uses, so there is one place to revoke.

What is *not* sent: your portal password (this software never has it), session
cookies, the Telegram bot token, or the API key of any other service. The
assistant reads the same database the dashboard renders, and that database holds
no secrets by design.

### Where the API key lives

In the OS keychain, alongside the Telegram token — never in the config file,
never in the dashboard page, never in the database. The loopback server holds it
and calls the model; the browser only ever receives rendered text.

### The dashboard server's write path

The chat feature gave a previously read-only server its first `POST` route.
Loopback alone is not sufficient protection for a write path — any local process
can reach the port, and a web page in another tab can be pointed at
`127.0.0.1` — so a request must satisfy all four of:

1. The random path token, compared in constant time.
2. A `Host` header of exactly `127.0.0.1:<port>`, which defeats DNS rebinding:
   a rebound hostname resolves here but is not what the browser sends.
3. An `Origin`, if present, matching the server's own. Checked when present
   rather than required, because a same-origin navigation legitimately omits it.
4. A custom `X-Shift-Agent` header, which forces a CORS preflight that is never
   answered. A cross-origin form POST or a `no-cors` fetch cannot set it.

Every failure returns the identical 404 the read path already used, so the token
stays unguessable and a rejected write reveals nothing about whether the token
was right. With no hub attached the route does not exist at all.

### Prompt injection, and why the tool list is the boundary

Shift titles and verdict details are strings the *portal* produced, which means
anyone who can get text onto your open-time page can get text into the
assistant's prompt. Treat that as a given rather than something to filter away.

Prompt-level defences help and are not a boundary. The boundary is the tool
surface, which is short on purpose — read status, settings and shift history,
pause, resume — and enforced by `tests/test_chat_boundary.py`, which fails the
build if a claiming verb appears in it. So the worst a successful injection buys
is a wrong answer or a paused agent, both of which you can see. It cannot claim
a shift, sign in, clear a challenge, or change your home base. See
[The chat panel and Telegram assistant](#the-chat-panel-and-telegram-assistant)
for the reasoning behind keeping claiming out.

Pausing is in the list rather than out of it because the failure directions are
not symmetric: a wrongly paused agent misses shifts and says so on the
dashboard, while a wrongly *unpausable* one cannot be stopped from the phone
you happen to be holding.

## Logging

Log output passes through a scrubber that removes labelled secrets
(`password=`, `token=`, `Authorization:`), JWTs, long hex strings, vendor API
keys (`sk-…`), and email addresses. This matters because portal error messages
routinely embed session tokens, and those messages otherwise land verbatim in a
log file that gets rotated to disk or pasted into a support thread.

The `sk-` rule was added with the chat assistant and closed a real gap: an
Anthropic key is neither hex nor a JWT, so before it existed a key pasted bare
into an error message passed through every other rule untouched.

The scrubber cleans records; it never drops them. Losing the fact that an error
happened would be worse than the leak it prevents.

**The same scrubber runs on anything sent to Telegram.** Logs stay on your
machine; a Telegram message does not. Three paths carry portal-authored text
outward and all three are scrubbed: the repeated-failure alert, the login-failure
detail, and the "action needed" hand-off. That last one still includes a usable
link to the challenge page — you need it to go and solve the thing — but a
session parameter inside that URL is redacted before it leaves.

## The chat panel and Telegram assistant

If you set an API key, the dashboard grows a Chat tab and the Telegram bot
answers plain messages as well as slash commands. Both are the same
conversation, stored in the same SQLite table as everything else — no secrets,
same as the rest of that database.

**The assistant cannot claim a shift.** Its tools read status, settings and
shift history, and can pause or resume monitoring. There is no claiming tool and
adding one is a deliberate, written decision, not a config change:
`tests/test_chat_boundary.py` fails the build if a claiming verb appears in the
tool list. Claiming still happens only when you press Confirm on an offer.

The reasoning matches the rest of this document: `Notifier.offer` treats silence
as refusal because a stale yes is dangerous, and a model reading consent out of
conversational text is a weaker signal than the silence that interface already
refuses. Being rostered onto a trip you did not agree to is the worst outcome
available here.

Message text is escaped everywhere it is rendered, and the dashboard sends a
restrictive `Content-Security-Policy`, because that text is written by a
language model and relayed from a chat app.

## Captchas and bot detection

Two statements, both true, and they are usually collapsed into one:

**The claiming path does not solve captchas.** A `PortalAdapter.login()` that
meets a challenge returns `NEEDS_HUMAN` with a link and stops. The poller pauses,
Telegram tells you, and it re-probes on a widening interval in case you clear it
in the browser without answering the bot. Nothing in that path clicks a
challenge, and no fingerprint-evasion tooling is used anywhere — an
*inconsistent* fingerprint is a stronger bot signal than a plain one. Requests
are jittered and run at human rates.

**The app also ships something that can work a challenge.** `friction/` is a
screenshot → vision-model → action loop plus an IMAP one-time-code reader,
installed by default and benchmarked by `shift-agent friction-bench` against
Google's public reCAPTCHA demo. Describing this project as "refusing to solve
captchas" full stop stopped being accurate the day that landed.

What separates them is a boundary that is checked, not a gap in the code:
nothing in `friction/` is imported by `adapters/`, nothing in `friction/`
imports from `adapters/`, and `tests/test_friction_boundary.py` fails the build
the moment an adapter module so much as mentions it. See
[docs/FRICTION_TOOLKIT.md](FRICTION_TOOLKIT.md) for what the toolkit is and
[docs/CAPTCHA.md](CAPTCHA.md) for the operational case.

### If you are considering wiring it in

That is a new, explicit, written decision — recorded here and in the adapter's
own module docstring, never a side effect of the toolkit being installed. The
thing to weigh is not whether it works:

- **The account is your employment.** A crew scheduling account flagged for
  automated challenge-solving is a disciplinary matter, not a
  retry-tomorrow inconvenience. The downside is not a missed shift, and it is
  not recoverable by changing a config value.
- **Clearing a demo page is not defeating a production system.** reCAPTCHA v3
  and Enterprise score a session's *behaviour* over time and never show an image
  challenge to solve. Turnstile is the same shape. Passing the bench says the
  loop mechanics work; it says nothing about an adversarial scorer.
- **Volume is its own signal.** A system that sees many challenges cleared
  quickly may flag the account regardless of how each one was cleared. The
  toolkit adds no pacing of its own.
- **The cheaper fix usually works.** Most of what drives challenge frequency is
  where the traffic comes from and how often it arrives — see
  [docs/CAPTCHA.md](CAPTCHA.md). Moving off a datacenter address and raising
  `poll.interval_seconds` are reversible; a flagged account is not.

If you do it anyway, do it knowing that: it is a defensible choice to make for
your own account, and an indefensible one to make silently for someone else's.

## Portal captures

The recon tool records a browsing session to build parsers. Its output contains
**live session cookies and personal data** and is written outside the repository
by design. Extract what you need, then delete the folder.

Fixtures committed to this repository are synthetic — hand-written to match the
structure, containing no real data and none of the portal's own markup.

## Failure modes that are deliberate

| Situation | Behaviour | Why |
|---|---|---|
| Grade unreadable | Alert only, never claim | A markup change must not promote a position you declined |
| Base unknown | Refuse | Wrong domicile means being rostered in another city |
| Premium flag unreadable | Not premium | An unfamiliar value must not make a shift claimable |
| Detail page unreachable | Alert only | A network blip must not become a claim |
| Confirmation times out | Treated as no | Silence is not consent |
| Three failed sign-ins | Stop and alert | A changed password must not lock your account |
| Challenge appears | Pause, alert, re-probe on a widening interval | A human may clear it without messaging the bot |
| Failed sign-in pause | Never self-recovers | Retrying a bad password is how accounts get locked |
| Request still pending | Neither success nor strike | A decision not yet made must not burn a retry |
| Dashboard build fails | Log and continue polling | A broken page must not stop shift monitoring |
| Model unreachable or key rejected | Chat says so, polling continues | The assistant is a convenience; monitoring is the product |

## Security review of this release

The chat assistant is the first feature to add a third-party network
destination and the first to give the dashboard server a write path, so it was
reviewed as a change to the threat model rather than as a feature.

**What was found and fixed while building it:**

- An unlabelled `sk-` API key survived log scrubbing entirely.
- The chat `POST` route was reachable by any page that knew the URL. Loopback
  and a path token are enough for a read; a write also needs `Host` pinning
  against DNS rebinding and a custom header to force an unanswered CORS
  preflight. Both are now required, and both are covered by a test that fails
  when either is removed.
- Building the chat client could take the whole agent down. The failure-mode
  table below promises that a model problem costs you the chat panel and
  nothing else; an `anthropic` import that failed instead escaped
  `_build_chat_hub` and killed `run --dashboard` before the first poll cycle.
  Found by running the real CLI, not by a test — the same way the last one was.
- `premium` failed open against a documented promise that it fails closed.
- A stale challenge flag could let the agent silently undo a `/pause` a human
  had asked for.
- `enrich()` followed whatever href the scraped markup contained. Fixing the
  relative-URL bug meant those links started working, so link resolution is now
  pinned to the portal's own origin — an absolute off-portal href would
  otherwise have sent a browser holding live crew-session cookies wherever the
  markup pointed.

**Accepted risks, stated rather than mitigated:**

- **Prompt injection from portal text** can reach the assistant. The mitigation
  is the tool surface, not the prompt. Worst case is a wrong answer or a paused
  agent, both visible on the dashboard.
- **A hosted model receives your roster.** Unavoidable once you enable the chat
  panel; the only way not to accept it is not to store a key.
- **The Linux secrets file is still unencrypted**, for the reasons already given
  above. The API key now sits in it alongside the Telegram token on such a box.
- **A captcha-solving loop ships in the box.** `friction/` is installed by
  default and is one written decision away from an adapter's `login()`. The
  boundary is a test, not an absence of code — see
  [Captchas and bot detection](#captchas-and-bot-detection) for what that
  decision costs if you make it.
- **The claim path remains unproven.** See [PARSING.md](PARSING.md); keep
  `dry_run: true` until it has been watched once.

## Reporting a problem

Open an issue. Please don't include real captures, cookies, or screenshots of
your roster.
