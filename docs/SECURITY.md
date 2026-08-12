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
| Telegram bot token, session material | OS keychain (Windows Credential Manager) | Encrypted against your Windows login |
| Shift history, verdicts, claims | SQLite under `%LOCALAPPDATA%` | Filesystem ACLs. **No secrets** |
| Dashboard HTML, `.ics`, markdown | Same profile folder | Your schedule. No secrets |
| Config YAML | Wherever you put it | Username and availability. No password |

The split is enforced, not conventional: `store.py` is documented as holding no
secrets, and a test asserts that a built dashboard payload contains no password,
cookie, or token.

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

The dashboard server **binds `127.0.0.1` only** and puts a random token in the
URL path. It is never exposed on a public interface. On a VPS, reach it through
an SSH tunnel rather than opening a port.

## Logging

Log output passes through a scrubber that removes labelled secrets
(`password=`, `token=`, `Authorization:`), JWTs, long hex strings, and email
addresses. This matters because portal error messages routinely embed session
tokens, and those messages otherwise land verbatim in a log file that gets
rotated to disk or pasted into a support thread.

The scrubber cleans records; it never drops them. Losing the fact that an error
happened would be worse than the leak it prevents.

## Captchas and bot detection

The agent does not solve captchas and does not use fingerprint-evasion tooling.
When challenged it pauses and asks you.

This is a deliberate refusal, not a missing feature. Those services exist to
defeat bot detection, and being flagged on an employer's scheduling system is a
disciplinary matter rather than an inconvenience. Requests are jittered and run
at human rates for the same reason.

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
