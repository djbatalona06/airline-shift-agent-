# Running it 24/7 on a server

This is the full walkthrough for keeping Shift Agent running around the clock on
a rented Linux server, written for someone who has never used Linux. Every
command can be copied and pasted. Each one says what it does and what a good
result looks like.

Budget about **45 minutes** the first time.

---

## Read this part before you spend any money

A server keeps the agent running when your own computer is off. That is the only
thing it buys you, and it costs something real in return.

**FLICA shows a "confirm you are human" box, and this agent will never click it
for you.** It is a deliberate refusal, not a missing feature — see
[SECURITY.md](SECURITY.md). So whenever FLICA challenges the sign-in, someone has
to see the browser and click it.

On your own PC that is one click. On a server the browser has no screen, so you
have to open a remote view first. This guide sets that up, and it works — but it
is three or four steps rather than one.

**It also happens more often on a server.** Datacenter addresses look more
suspicious to bot protection than a home internet connection, so expect to be
challenged more, not less.

| | Your own always-on PC | Rented server |
|---|---|---|
| Cost | £0 if you have one | ~$5–8/month |
| Setup | Download and run | This guide, ~45 min |
| Clearing a captcha | One click | Open tunnel → open viewer → click |
| How often challenged | Less | More |
| Survives a power cut | No | Yes |

**If you have a computer that can stay on, use it instead** — see
[INSTALL.md](INSTALL.md). An old laptop or a cheap mini-PC left plugged in is
genuinely the easier answer. Come back here if that is not an option.

Still want the server? Carry on.

---

## Step 1 — Get the server

Any provider works. [Hostinger](https://www.hostinger.com/vps-hosting) is one of
the cheaper ones. *No affiliation, no referral, not sponsored.*

When you order, choose:

- **Ubuntu 24.04 LTS** as the operating system (22.04 is fine too). Not
  "Ubuntu with cPanel", not AlmaLinux, not Windows.
- **At least 2 GB of RAM.** This is not optional. The agent runs a real web
  browser, and a 1 GB server will run out of memory and be killed mid-week.
- A **KVM** plan if you are offered the choice.

When it finishes setting up, the provider gives you two things. Write them down:

- The server's **IP address**, four numbers like `203.0.113.45`
- The **root password**

---

## Step 2 — Connect to it

You do not need to install PuTTY or anything else. Windows already has what you
need.

Press the Windows key, type **PowerShell**, and open it. Then type this,
replacing the numbers with your server's IP address:

```bash
ssh root@203.0.113.45
```

The first time, it asks:

```
The authenticity of host '203.0.113.45' can't be established.
Are you sure you want to continue connecting (yes/no)?
```

Type `yes` and press Enter. Then paste your root password.

> **The password will not appear as you type it.** No dots, no stars, nothing.
> That is normal — Linux hides it completely. Paste it and press Enter.

**Good result:** the prompt changes to something like `root@srv123:~#`. You are
now typing commands on the server rather than on your own computer.

---

## Step 3 — Run the setup script

One command. Copy the whole thing:

```bash
curl -fsSL https://raw.githubusercontent.com/djbatalona06/airline-shift-agent-/main/scripts/setup-vps.sh -o setup-vps.sh && bash setup-vps.sh
```

This takes **5–10 minutes**, most of it downloading the browser. You will see a
lot of text scroll past. That is fine.

What it is doing:

- installing Python, git, and a **virtual screen** (the fake monitor the browser
  draws on, since the server has no real one)
- creating a normal user account called `shiftagent`, so the agent never runs
  as the all-powerful root account
- downloading Shift Agent and its browser
- setting up three background services so everything restarts by itself after a
  reboot
- generating a random password for the remote view

**It opens no firewall ports.** Nothing on this server is reachable from the
internet except SSH.

**Good result:** it finishes with `Setup finished.` and a numbered list of what
to do next. That list is the short version of steps 4–8 below.

If you ever need to run it again — after an update, or if something broke — it
is safe. It checks everything before it changes anything and leaves your
settings and your saved FLICA sign-in alone.

---

## Step 4 — Fill in your settings

```bash
sudo -u shiftagent nano /home/shiftagent/shift-agent/config/users/me.yaml
```

`nano` is a plain text editor inside the terminal. Arrow keys to move; there is
no mouse.

The settings that matter most:

| Setting | What to put |
|---|---|
| `adapter` | `flica` |
| `base_url` | your FLICA sign-in address |
| `username` | your FLICA username |
| `timezone` | e.g. `America/New_York` |
| `slots` | the days and hours you actually want trips |
| `code` under `home_base` | your base, e.g. `MCO` |
| `pursue` under `grades` | the position letters worth taking |
| `dry_run` | **leave it `true` for the first week** |

Every line is explained in the file itself.

To save: **Ctrl+O**, then Enter, then **Ctrl+X** to leave.

> **Leave `dry_run: true` to begin with.** The agent then watches and tells you
> what it *would* have taken, without requesting anything. Give it a week and
> compare its choices against what you would have picked yourself. Turning it
> off is a decision you make once you trust it — not on day one.

---

## Step 5 — Install a VNC viewer on your own computer

This is what lets you see the server's browser. You only do this once.

Download **RealVNC Viewer**:
<https://www.realvnc.com/en/connect/download/viewer/>

Pick the Windows version, install it, and open it once so you know where it is.
(TightVNC Viewer works too if you prefer.)

---

## Step 6 — Open the tunnel

The remote view is deliberately locked so it only accepts connections from the
server itself. The tunnel is how you get inside that boundary — nothing is
exposed to the internet.

Open a **second** PowerShell window, leaving your first one connected, and run:

```bash
ssh -L 5901:127.0.0.1:5901 root@203.0.113.45
```

Same IP and password as before.

**Leave this window open.** Closing it closes the tunnel. It will just sit there
looking idle — that is what it is supposed to do.

---

## Step 7 — Sign in to FLICA, once

First get the viewer password. In your **first** window:

```bash
sudo cat /home/shiftagent/.vnc/passwd.txt
```

Copy what it prints.

Now open RealVNC Viewer and connect to:

```
127.0.0.1:5901
```

It asks for a password — paste the one you just copied.

**Good result:** a window opens showing a plain grey screen. Nothing else. That
is correct — nothing has drawn on it yet.

Leave the viewer open. Back in your **first** window, run:

```bash
cd /home/shiftagent/shift-agent
sudo -u shiftagent env DISPLAY=:99 /home/shiftagent/shift-agent/.venv/bin/python \
    -m shift_agent.main run --config config/users/me.yaml --dry-run --once
```

Watch the VNC window. A browser opens on FLICA's sign-in page.

**In the VNC window** — not in PowerShell — type your FLICA username and
password and sign in. If it shows a "confirm you are human" box, click it.

> Your password goes into FLICA's own page in that browser. Shift Agent never
> sees it, never stores it, and has no field to put it in.

Once you are looking at your normal FLICA screen, you are done. That sign-in is
remembered from now on, including across reboots. You can close the VNC viewer
and the second PowerShell window.

---

## Step 8 — Start it for real

Back in your first window:

```bash
sudo systemctl start shift-agent
```

Check it is alive:

```bash
sudo systemctl status shift-agent
```

**Good result:** a green `active (running)`. Press **q** to exit that view.

Watch what it is doing, live:

```bash
sudo journalctl -u shift-agent -f
```

Press **Ctrl+C** to stop watching. Stopping the watching does not stop the
agent.

That is it. It now runs day and night and restarts itself if the server reboots.

---

## Getting messages on your phone

Optional but worth the two minutes — it is how the agent asks permission before
taking anything.

Follow [Telegram setup](INSTALL.md#telegram-optional). On the server, prefix the
commands so they run as the right account:

```bash
cd /home/shiftagent/shift-agent
sudo -u shiftagent .venv/bin/python -m shift_agent.main set-token
sudo -u shiftagent .venv/bin/python -m shift_agent.main link
```

Then restart it so it picks the token up:

```bash
sudo systemctl restart shift-agent
```

> On a server there is no Windows Credential Manager, so the token is stored in
> a file only the `shiftagent` account can read. See
> [SECURITY.md](SECURITY.md#where-things-are-stored) for what that does and does
> not protect against.

---

## Seeing your dashboard

Build it, then look at it through a tunnel — same idea as the VNC one.

On the server:

```bash
cd /home/shiftagent/shift-agent
sudo -u shiftagent .venv/bin/python -m shift_agent.main dashboard --config config/users/me.yaml
```

It prints where it wrote the page. To view it, serve it and tunnel to it:

```bash
ssh -L 8765:127.0.0.1:8765 root@203.0.113.45
```

Then open `http://127.0.0.1:8765` in your own browser.

> **Never open port 8765 in the firewall**, and never let anyone tell you to.
> The dashboard is your full roster. An open port would put it on the public
> internet behind nothing but a web address nobody has guessed *yet*.

---

## When the "confirm you are human" box comes back

**This is the thing that will happen most often.** It is not a fault.

You will notice because Telegram goes quiet, or `systemctl status shift-agent`
mentions needing a human.

The fix is steps 6 and 7 again, and it takes about two minutes:

1. Open a PowerShell window: `ssh -L 5901:127.0.0.1:5901 root@YOUR_IP`
2. Open RealVNC Viewer → `127.0.0.1:5901` → the password from
   `sudo cat /home/shiftagent/.vnc/passwd.txt`
3. Click the box. Sign in again if it asks.
4. Close the viewer. The agent picks up where it left off.

You do **not** need to restart anything or re-run the setup script.

---

## Everyday commands

Run these on the server, in PowerShell after `ssh root@YOUR_IP`.

| What you want | Command |
|---|---|
| Is it running? | `sudo systemctl status shift-agent` |
| Watch it live | `sudo journalctl -u shift-agent -f` |
| What happened today | `sudo journalctl -u shift-agent --since today` |
| Stop it | `sudo systemctl stop shift-agent` |
| Start it | `sudo systemctl start shift-agent` |
| Restart after changing settings | `sudo systemctl restart shift-agent` |
| Stop it starting at boot | `sudo systemctl disable shift-agent` |
| Change settings | `sudo -u shiftagent nano /home/shiftagent/shift-agent/config/users/me.yaml` |
| Read the VNC password | `sudo cat /home/shiftagent/.vnc/passwd.txt` |

**Pause without stopping:** if you have Telegram set up, send `/pause` to your
bot and `/resume` when you want it back. Easier than SSH from a phone.

**Update to a newer version:**

```bash
sudo -u shiftagent git -C /home/shiftagent/shift-agent pull
sudo systemctl restart shift-agent
```

---

## When something is wrong

**`sudo systemctl status shift-agent` says `failed`**

Read the reason:

```bash
sudo journalctl -u shift-agent -n 40
```

The messages are written in plain English rather than as programmer errors. Work
down this list — it is ordered by how often each one is the cause.

---

**"No display is available, so the sign-in window cannot open."**

The virtual screen is not running.

```bash
sudo systemctl start shift-agent-xvfb
sudo systemctl restart shift-agent
```

---

**The VNC viewer says "connection refused"**

The tunnel is not open, or its PowerShell window was closed. Re-run:

```bash
ssh -L 5901:127.0.0.1:5901 root@YOUR_IP
```

and leave it open while you use the viewer. If it still refuses, the view
service is down:

```bash
sudo systemctl restart shift-agent-vnc
```

---

**The VNC window is grey and stays grey**

Correct, unless a browser is meant to be open. The screen only shows something
while the agent has a browser running. Start the sign-in command from step 7 and
watch it appear.

---

**It was working, now the server feels dead or very slow**

Almost always memory. Check:

```bash
free -m
```

If `available` is under ~200 MB, the server is too small. A browser needs about
2 GB total. Resize the plan — this is the one problem you cannot configure your
way out of.

---

**`sudo: command not found` or `Permission denied`**

You are signed in as the wrong user. Type `exit` and reconnect with
`ssh root@YOUR_IP`.

---

**You want to start completely over**

The setup script is safe to re-run and repairs most damage:

```bash
bash setup-vps.sh
```

If you want to wipe the saved FLICA sign-in and start that part fresh:

```bash
sudo systemctl stop shift-agent
sudo rm -rf /home/shiftagent/.shift-agent/profiles
sudo systemctl start shift-agent
```

Then do step 7 again.

---

## What is actually running

For when you want to know what the machine is doing, or someone technical asks.

| Service | Job |
|---|---|
| `shift-agent-xvfb` | The virtual screen. Nothing to look at unless something draws on it |
| `shift-agent-vnc` | Lets you view that screen. **Accepts connections from the server itself only** — the tunnel is the only way in |
| `shift-agent` | The agent. Watches FLICA, applies your rules, asks before claiming |

All three run as `shiftagent`, never as root. All three restart on their own if
they crash or the server reboots.

Files worth knowing:

| Path | What |
|---|---|
| `/home/shiftagent/shift-agent/` | The program |
| `.../config/users/me.yaml` | Your settings |
| `/home/shiftagent/.shift-agent/` | Roster database and the saved browser sign-in |
| `/home/shiftagent/.vnc/passwd.txt` | Your VNC password |
| `/etc/systemd/system/shift-agent*.service` | The three services |
