# Installing Shift Agent

Three ways to run it. Pick the row that describes you.

| Path | Who it's for | What you need installed |
|---|---|---|
| [Desktop app](#desktop-app) | Anyone. No technical setup | **Nothing.** Windows 10 or 11 |
| [Localhost](#localhost) | Developers, or trying it out | Python 3.11+ and git |
| [VPS](#vps-247) | Running it 24/7 | An Ubuntu server, ~2 GB RAM |

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

Only worth it if the machine at home can't stay on. **Read the warning below
first — for some portals a VPS is actively worse.**

### Before you choose this

**A captcha will stop the agent until someone can reach that server's browser.**
The challenge is tied to the session running on the VPS, so it cannot be
forwarded to your phone. On your own PC you just click it.

Datacenter IP addresses also score far worse with reCAPTCHA and look more
suspicious to portals that watch for automation. **If your portal shows
captchas, run it on a home computer instead.**

### Requirements

- **Ubuntu LTS** (22.04 or 24.04)
- **At least 2 GB RAM.** The agent drives a real browser — session cookies do
  not work outside one, so there is no lightweight mode
- 10 GB disk

Any provider works. [Hostinger](https://www.hostinger.com/vps-hosting) is one of
the cheaper ones — pick a **KVM** plan whose RAM meets the above. *No
affiliation, no referral, not sponsored.*

### Setup

Create the VPS with the Ubuntu LTS template, then connect:

```bash
ssh root@YOUR_SERVER_IP
```

Make a normal user rather than running as root:

```bash
adduser shiftagent
usermod -aG sudo shiftagent
su - shiftagent
```

Install dependencies. `--with-deps` pulls the system libraries Chromium needs —
there are a lot, and guessing them by hand is how this goes wrong:

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip git

git clone https://github.com/djbatalona06/airline-shift-agent-.git
cd airline-shift-agent-
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m playwright install --with-deps chromium
```

Create your config:

```bash
cp config/users/example.yaml config/users/me.yaml
nano config/users/me.yaml
```

### Run it as a service

```bash
sudo nano /etc/systemd/system/shift-agent.service
```

```ini
[Unit]
Description=Shift Agent
After=network-online.target

[Service]
Type=simple
User=shiftagent
WorkingDirectory=/home/shiftagent/airline-shift-agent-
ExecStart=/home/shiftagent/airline-shift-agent-/.venv/bin/python -m shift_agent.main run --config config/users/me.yaml
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now shift-agent
journalctl -u shift-agent -f
```

### Reaching the dashboard — do not open a port

The dashboard binds to `127.0.0.1` and stays there. Reach it through an SSH
tunnel from your own machine:

```bash
ssh -L 8765:127.0.0.1:8765 shiftagent@YOUR_SERVER_IP
```

Then open `http://127.0.0.1:8765` locally.

**Do not open port 8765 in the firewall.** The dashboard contains your full
roster, and an open port would put it on the internet behind nothing but a URL
nobody has guessed yet.

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
