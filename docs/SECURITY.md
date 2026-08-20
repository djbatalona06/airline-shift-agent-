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

Nothing is sent to the author or any third party. There is no telemetry.

- `api.anthropic.com`, if you enable the chat panel or the friction toolkit.

The dashboard server **binds `127.0.0.1` only** and puts a random token in the
URL path. It is never exposed on a public interface, including for the chat
routes. On a VPS, reach it through an SSH tunnel rather than opening a port.

This is why chat history reaches your phone through Telegram rather than by
opening the dashboard to your network: your phone genuinely cannot connect to
the dashboard, and that is the intended property, not a limitation to work
around.

## Logging

Log output passes through a scrubber that removes labelled secrets
(`password=`, `token=`, `Authorization:`), JWTs, long hex strings, and email
addresses. This matters because portal error messages routinely embed session
tokens, and those messages otherwise land verbatim in a log file that gets
rotated to disk or pasted into a support thread.

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

The agent does not solve captchas and does not use fingerprint-evasion tooling.
When challenged it pauses and asks you.

This is a deliberate refusal, not a missing feature. Those services exist to
defeat bot detection, and being flagged on an employer's scheduling system is a
disciplinary matter rather than an inconvenience. Requests are jittered and run
at human rates for the same reason.

## The friction toolkit (built in, not used by any adapter)

[docs/FRICTION_TOOLKIT.md](FRICTION_TOOLKIT.md) documents a screenshot-vision-
model action loop and an IMAP one-time-code reader, run via `shift-agent
friction-bench` and `shift-agent friction-set-vision-key`/
`friction-set-imap-password`. It ships with the app — no separate install
step — but is not imported by `main.py`'s `run`/`poller` path or by any
`PortalAdapter`, including FLICA. It exists as a reusable, portal-agnostic
capability for whatever future adapter needs it, not as something aimed at
FLICA specifically.

The policy above is unchanged: a portal adapter's `login()` still returns
`NEEDS_HUMAN` and hands off to you on any captcha or MFA challenge. Wiring the
friction toolkit into a live adapter would require a new, explicit, written
decision — not a side effect of it being installed by default.

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
| Detail page unreachable | Alert only | A network blip must not become a claim |
| Confirmation times out | Treated as no | Silence is not consent |
| Three failed sign-ins | Stop and alert | A changed password must not lock your account |
| Dashboard build fails | Log and continue polling | A broken page must not stop shift monitoring |

## Reporting a problem

Open an issue. Please don't include real captures, cookies, or screenshots of
your roster.
