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
- **Your chosen model endpoint, if you turn on the chat assistant.** See below.

There is no telemetry, and nothing is sent to the author.

- `api.anthropic.com`, if you enable the chat panel or the friction toolkit.

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

Three things follow:

- **It is off unless you turn it on.** No key is stored by default, and with no
  key the bubble shows setup instructions instead of a chat box. Nothing is sent.
- **A local model keeps the original guarantee intact.** Point
  `llm.base_url` at Ollama or llama.cpp on `127.0.0.1` and nothing leaves the
  machine — the assistant works exactly the same way. If the original privacy
  promise is what you value about this project, this is the configuration to
  pick, and it needs no API key at all.
- **A hosted model is a considered trade, not a default you drift into.** You
  have to run `shift-agent set-llm-key` to make it happen.

What is *not* sent, in either configuration: your portal password (this software
never has it), session cookies, the Telegram bot token, or the API key of any
other service. The assistant reads the same database the dashboard renders, and
that database holds no secrets by design.

### Where the API key lives

In the OS keychain, alongside the Telegram token — never in the config file,
never in the dashboard page, never in the database. In the default `proxy` mode
the loopback server holds it and the browser only ever receives rendered text; a
test asserts the key appears in neither the DOM, nor `localStorage`, nor the
embedded payload.

`llm.mode: browser` is the exception, and it says so in the panel: it exists for
opening the saved dashboard file directly with no server behind it, and there
the key is stored in the browser rather than the keychain. That mode also has no
tools — it can only discuss what is already on the page.

### The dashboard server's write path

The chat feature gave a previously read-only server its first `POST` routes.
Loopback alone is not sufficient protection for a write path — any local process
can reach the port, and a web page in another tab can be pointed at
`127.0.0.1` — so a request must satisfy all four of:

1. The random path token, compared in constant time.
2. A `Host` header of exactly `127.0.0.1:<port>`, which defeats DNS rebinding.
3. An `Origin`, if present, matching the server's own.
4. A custom `X-Shift-Agent` header, which forces a CORS preflight that is never
   answered.

Every failure returns the identical 404 the read path already used, so the token
stays unguessable. With no chat backend attached the routes do not exist at all.

### What the assistant is allowed to do

It reads. There is no claim tool and no portal tool — it cannot pick up a shift,
sign in, clear a challenge, or change your home base.

The one write it can reach is editing your config file, and that is deliberately
split in two: the model can *propose* a change, which validates it and returns a
diff; only pressing **Apply** writes anything, and the id authorising the write
comes from the UI rather than from the model. A backup is kept alongside.

This split exists because shift titles and verdict details are strings the portal
produced, which means anyone who can get text onto your open-time page can get
text into the assistant's prompt. That text travels inside a delimited block the
system prompt identifies as data rather than instructions — but prompt-level
defences are mitigation, not a boundary. The boundary is the narrow tool surface:
a successful injection can produce a wrong answer or a proposed config change you
will see as a diff. It cannot claim a shift.

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
- Config edits written with `yaml.safe_dump` destroyed every comment in the
  file — the comments are the documentation, so this was treated as a defect,
  not cosmetics.
- A patch value of `20:00` written unquoted is base-60 in YAML 1.1, and parsed
  back as **00:20**. Asking to move a window to 8pm would silently have set it
  to twenty past midnight, and the validation step would not have caught it
  because it checked a parallel value rather than the file being written.
  Validation now re-reads exactly what will be written.
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
  is the tool surface, not the prompt: read tools plus a proposal that needs a
  human click. Worst case is a wrong answer or an unwanted diff you can see.
- **A hosted model receives your roster.** Unavoidable if you choose one; use a
  local endpoint if that matters to you.
- **The Linux secrets file is still unencrypted**, for the reasons already given
  above. The API key now sits in it alongside the Telegram token on such a box.
- **`browser` mode stores the key in the browser.** It is opt-in, labelled in
  the UI, and exists only for a configuration where no server is running.
- **The claim path remains unproven.** See [PARSING.md](PARSING.md); keep
  `dry_run: true` until it has been watched once.

## Reporting a problem

Open an issue. Please don't include real captures, cookies, or screenshots of
your roster.
