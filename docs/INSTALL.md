# Installing Shift Agent

Three ways to run it. Pick the row that describes you.

| Path | Who it's for | What you need installed |
|---|---|---|
| [Desktop app](#desktop-app) | Anyone. No technical setup | **Nothing.** Windows 10 or 11 |
| [Localhost](#localhost) | Developers, or trying it out | Python 3.11+ and git |
| [VPS](#vps-247) | Running it 24/7 | An Ubuntu server, 2 GB RAM. See [VPS.md](VPS.md) |

---

## Desktop app

**Recommended.** Nothing to install — Python and the browser engine are inside
the download.

1. Download `ShiftAgent-windows.zip` from the
   [latest release](https://github.com/djbatalona06/airline-shift-agent-/releases/latest).
2. Right-click → **Extract All**. Anywhere is fine; the Desktop is fine.
3. Open the folder and double-click **`ShiftAgent.exe`**.
4. A setup window opens. Fill it in and you're done.

### "Windows protected your PC"

You will probably see this on first run. It appears because the app isn't
code-signed — a certificate costs a few hundred dollars a year, which this
project doesn't spend. It is not a virus warning.

Click **More info**, then **Run anyway**. Once only.

### If the window doesn't appear

On Windows 10 you may need the WebView2 runtime, which Windows 11 already has.
Install the "Evergreen Bootstrapper" from
[Microsoft's WebView2 page](https://developer.microsoft.com/microsoft-edge/webview2/),
then run the app again.

### What it needs from you

- **Your portal sign-in**, typed by you into the portal's own login page. The
  app never asks for your password and cannot see it.
- Optionally a **Telegram bot** so it can message your phone — takes about two
  minutes, see [Telegram setup](#telegram-optional).

---

## Localhost

For development, or to run it on a computer you already use.

**Prerequisites:** [Python 3.11 or newer](https://www.python.org/downloads/)
(3.14.5 is what this is tested on) and [git](https://git-scm.com/downloads).
On Windows, tick **"Add Python to PATH"** in the installer.

```bash
git clone https://github.com/djbatalona06/airline-shift-agent-.git
cd airline-shift-agent-

python -m venv .venv
```

Then activate and install. Windows:

```bash
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m playwright install chromium
```

macOS or Linux:

```bash
.venv/bin/python -m pip install -e .
.venv/bin/python -m playwright install chromium
```

Check it works — this needs no config, credentials, or network:

```bash
python -m shift_agent.main demo
```

Then create your config and open the dashboard:

```bash
cp config/users/example.yaml config/users/me.yaml
python -m shift_agent.main dashboard --config config/users/me.yaml --open
```

`--open` serves it on `127.0.0.1` and opens an app window. The clipboard button
works only over HTTP, which is why it is served rather than opened as a file.

Run the agent itself:

```bash
python -m shift_agent.main run --config config/users/me.yaml --dry-run
```

Leave `--dry-run` on for the first week. It evaluates and notifies without
claiming anything, so you can compare its choices against what you'd have picked.

---

## VPS (24/7)

**Full walkthrough: [docs/VPS.md](VPS.md).** It is written for someone who has
never used Linux, and it is one script rather than the page of hand-typed
commands that used to live here.

The short version of what changed and why: the agent drives a **visible**
browser, because FLICA challenges the sign-in with a "confirm you are human" box
and this agent refuses to solve one. A server has no screen, so the setup script
installs a virtual screen and a way to look at it through an SSH tunnel. Doing
that by hand is where this goes wrong, so it is scripted:

```bash
curl -fsSL https://raw.githubusercontent.com/djbatalona06/airline-shift-agent-/main/scripts/setup-vps.sh -o setup-vps.sh && bash setup-vps.sh
```

Requirements: **Ubuntu 22.04 or 24.04**, **at least 2 GB RAM** (a real browser
runs for the agent's whole life — there is no lightweight mode), 10 GB disk.

**Consider not doing this.** A datacenter address gets challenged more often
than a home connection, and each challenge costs you a remote-view session
rather than one click. If any computer you own can stay on, use that instead.
[docs/VPS.md](VPS.md) opens with the comparison.

---

## Telegram (optional)

Lets the agent message your phone and lets you reply Confirm or Skip.

1. In Telegram, message [@BotFather](https://t.me/botfather) and send
   `/newbot`. Follow the prompts. It gives you a token.
2. Store it — the prompt hides what you type, and it goes to the OS keychain:
   ```bash
   python -m shift_agent.main set-token
   ```
3. Generate a one-time linking code:
   ```bash
   python -m shift_agent.main link
   ```
4. Send that code to your bot as instructed.

Until the code is used the bot accepts **no commands from anyone** — that is what
stops a stranger who finds your bot from pausing the agent or confirming claims.

Once linked: `/status`, `/pause`, `/resume`, `/schedule`.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'shift_agent'`** — the package isn't
installed in the interpreter you're using. Run `pip install -e .` inside the
virtual environment, and call `.venv\Scripts\python.exe` rather than a bare
`python`.

**`playwright._impl._errors.Error: Executable doesn't exist`** — run
`python -m playwright install chromium`.

**Windows: `Could not install packages due to an OSError ... No such file or
directory`** with a very long path in the message. Windows caps paths at 260
characters by default and some of Playwright's internal files are deeply nested.
Clone somewhere short, like `C:\dev\shift-agent`, or
[enable long path support](https://pip.pypa.io/warnings/enable-long-paths).

**Nothing happens when I run a command** — you are probably in the wrong folder,
or the error scrolled past. Run from the repository root and read the whole
output.

**The agent says it's paused** — either a captcha appeared or sign-in failed
three times. Check Telegram, fix it, then send `/resume`.

**Dashboard opens but "Copy as markdown" does nothing** — you opened the HTML
file directly. Use `--open`, which serves it over HTTP; browsers block the
clipboard on `file://` pages.
