#!/usr/bin/env bash
#
# One-command setup for running Shift Agent 24/7 on an Ubuntu server.
#
# Run it as root on a fresh Ubuntu 22.04 or 24.04 box:
#
#     bash setup-vps.sh
#
# Safe to run again. Every step checks before it acts, so a second run repairs
# whatever is missing and leaves everything else — including your config and
# your signed-in browser session — untouched.
#
# WHY THIS SCRIPT EXISTS AT ALL
#
# FLICA shows a "confirm you are human" box, and this agent will never solve
# one. So the browser has to be visible to a person, and a server has no
# screen. The fix is a virtual screen (Xvfb) plus a way to look at it (x11vnc)
# over an SSH tunnel. Getting that combination right by hand is where this goes
# wrong, so it is scripted.
#
# WHAT IT DELIBERATELY DOES NOT DO
#
#   * It opens no firewall ports. The VNC screen and the dashboard are reachable
#     only through an SSH tunnel you start yourself.
#   * It never asks for, stores, or types your FLICA password.
#   * The agent does not run as root.
#
set -euo pipefail

APP_USER="${APP_USER:-shiftagent}"
APP_HOME="/home/${APP_USER}"
APP_DIR="${APP_DIR:-${APP_HOME}/shift-agent}"
REPO_URL="${REPO_URL:-https://github.com/djbatalona06/airline-shift-agent-.git}"
CONFIG_NAME="${CONFIG_NAME:-me.yaml}"
CONFIG_PATH="config/users/${CONFIG_NAME}"
DISPLAY_NUM="${DISPLAY_NUM:-:99}"
SCREEN_SIZE="${SCREEN_SIZE:-1280x900x24}"
VNC_PORT="${VNC_PORT:-5901}"
VNC_DIR="${APP_HOME}/.vnc"
VNC_PASSFILE="${VNC_DIR}/passwd"

# Set SHIFT_AGENT_SOURCE=/path/to/repo to install from a local copy instead of
# cloning. Used by the test harness; also handy if the box has no GitHub access.
LOCAL_SOURCE="${SHIFT_AGENT_SOURCE:-}"

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; OFF=$'\033[0m'
step() { printf '\n%s==>%s %s%s%s\n' "$GREEN" "$OFF" "$BOLD" "$1" "$OFF"; }
info() { printf '    %s\n' "$1"; }
skip() { printf '    %salready done: %s%s\n' "$DIM" "$1" "$OFF"; }
warn() { printf '%s !! %s%s\n' "$YELLOW" "$1" "$OFF"; }
die()  { printf '\n%s !! %s%s\n\n' "$RED" "$1" "$OFF" >&2; exit 1; }

# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

[ "$(id -u)" -eq 0 ] || die "Run this as root: sudo bash $0
The agent itself will NOT run as root - the script creates a normal
'${APP_USER}' account and installs everything there. Root is only needed to
install system packages and register the background services."

command -v apt-get >/dev/null 2>&1 || die "This script is for Ubuntu/Debian. It needs apt-get."

if [ -r /etc/os-release ]; then
    . /etc/os-release
    info "Detected ${PRETTY_NAME:-unknown}"
    case "${VERSION_ID:-}" in
        22.04|24.04|25.04) : ;;
        *) warn "Tested on Ubuntu 22.04 and 24.04. Continuing on ${VERSION_ID:-unknown}." ;;
    esac
fi

TOTAL_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
if [ "$TOTAL_MB" -gt 0 ] && [ "$TOTAL_MB" -lt 1800 ]; then
    warn "This box has ${TOTAL_MB} MB of RAM. The agent runs a real browser and needs about 2 GB."
    warn "It may install fine and then be killed while running. Resize the server if you can."
fi

# systemd is absent in containers. Everything else still applies, so the script
# installs and configures, then tells you what it could not start.
HAVE_SYSTEMD=0
[ -d /run/systemd/system ] && HAVE_SYSTEMD=1

# --------------------------------------------------------------------------
# System packages
# --------------------------------------------------------------------------

step "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    python3 python3-venv python3-pip git ca-certificates curl nano sudo \
    xvfb x11vnc xauth >/dev/null
info "python3, git, Xvfb (virtual screen) and x11vnc (remote view) installed"

# --------------------------------------------------------------------------
# Unprivileged account
# --------------------------------------------------------------------------

step "Setting up the '${APP_USER}' account"
if id -u "$APP_USER" >/dev/null 2>&1; then
    skip "user ${APP_USER} exists"
else
    # useradd, not adduser: adduser is a Debian convenience wrapper and is
    # absent from minimal Ubuntu images. No password is set, so the account
    # cannot be logged into directly - reach it with sudo.
    useradd --create-home --shell /bin/bash "$APP_USER"
    info "created ${APP_USER} (no password - reach it with 'sudo -u ${APP_USER}')"
fi
install -d -o "$APP_USER" -g "$APP_USER" -m 0700 "$VNC_DIR"

as_app() { runuser -u "$APP_USER" -- "$@"; }

# --------------------------------------------------------------------------
# The code
# --------------------------------------------------------------------------

step "Fetching Shift Agent"
if [ -n "$LOCAL_SOURCE" ]; then
    [ -d "$LOCAL_SOURCE" ] || die "SHIFT_AGENT_SOURCE=$LOCAL_SOURCE is not a directory"
    mkdir -p "$APP_DIR"
    # Copied through tar so the excludes apply at the source. Deleting
    # $APP_DIR/.venv afterwards would instead throw away a working environment
    # on every re-run, which is the opposite of idempotent.
    tar -C "$LOCAL_SOURCE" \
        --exclude=./.venv --exclude=./.git --exclude=./__pycache__ \
        --exclude=./.pytest_cache -cf - . | tar -C "$APP_DIR" -xf -
    chown -R "$APP_USER:$APP_USER" "$APP_DIR"
    info "installed from local copy $LOCAL_SOURCE"
elif [ -d "$APP_DIR/.git" ]; then
    as_app git -C "$APP_DIR" pull --ff-only --quiet || warn "could not update - keeping the existing copy"
    skip "repository present, pulled latest"
else
    as_app git clone --quiet "$REPO_URL" "$APP_DIR"
    info "cloned into $APP_DIR"
fi

step "Creating the Python environment"
if [ -x "$APP_DIR/.venv/bin/python" ]; then
    skip "virtual environment exists"
else
    as_app python3 -m venv "$APP_DIR/.venv"
    info "created $APP_DIR/.venv"
fi
as_app "$APP_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
as_app "$APP_DIR/.venv/bin/python" -m pip install --quiet -e "$APP_DIR"
info "Shift Agent installed"

# --------------------------------------------------------------------------
# Chromium
# --------------------------------------------------------------------------

step "Installing the browser (this is the slow part - a few minutes)"
# Split deliberately: the system libraries need root, the browser itself must
# land in the app user's home or the service cannot read it.
"$APP_DIR/.venv/bin/python" -m playwright install-deps chromium >/dev/null
as_app "$APP_DIR/.venv/bin/python" -m playwright install chromium >/dev/null
info "Chromium ready"

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

step "Preparing your settings file"
if [ -f "$APP_DIR/$CONFIG_PATH" ]; then
    skip "$CONFIG_PATH already exists - not touching it"
else
    as_app cp "$APP_DIR/config/users/example.yaml" "$APP_DIR/$CONFIG_PATH"
    info "created $APP_DIR/$CONFIG_PATH from the example"
    warn "You still have to edit it: home base, availability, and portal settings."
fi

# --------------------------------------------------------------------------
# VNC password
# --------------------------------------------------------------------------

step "Securing the remote view"
if [ -f "$VNC_PASSFILE" ]; then
    skip "VNC password already set (see the end of this output for how to read it)"
else
    VNC_PLAIN="$(head -c 9 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 8)"
    as_app x11vnc -storepasswd "$VNC_PLAIN" "$VNC_PASSFILE" >/dev/null 2>&1
    chmod 600 "$VNC_PASSFILE"
    printf '%s\n' "$VNC_PLAIN" > "${VNC_DIR}/passwd.txt"
    chown "$APP_USER:$APP_USER" "${VNC_DIR}/passwd.txt"
    chmod 600 "${VNC_DIR}/passwd.txt"
    info "generated a random VNC password"
fi

# --------------------------------------------------------------------------
# Services
# --------------------------------------------------------------------------

step "Registering the background services"

cat > /etc/systemd/system/shift-agent-xvfb.service <<EOF
[Unit]
# The virtual screen. Chromium draws here; nobody is watching unless the VNC
# service below is running and you have an SSH tunnel open.
Description=Shift Agent virtual display
After=network.target

[Service]
Type=simple
User=${APP_USER}
# -nolisten tcp: the X server accepts no network connections at all. The only
# way in is x11vnc, running locally as the same user.
ExecStart=/usr/bin/Xvfb ${DISPLAY_NUM} -screen 0 ${SCREEN_SIZE} -nolisten tcp
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/shift-agent-vnc.service <<EOF
[Unit]
Description=Shift Agent remote view (loopback only)
After=shift-agent-xvfb.service
Requires=shift-agent-xvfb.service

[Service]
Type=simple
User=${APP_USER}
# -localhost is the security boundary: x11vnc binds 127.0.0.1 and refuses every
# other interface, so this port is unreachable from the internet even if the
# firewall were wide open. Reach it through 'ssh -L' instead.
ExecStart=/usr/bin/x11vnc -display ${DISPLAY_NUM} -localhost -rfbauth ${VNC_PASSFILE} \\
    -rfbport ${VNC_PORT} -forever -shared -noxdamage -quiet
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/shift-agent.service <<EOF
[Unit]
Description=Shift Agent
# Ordered after the display because the browser cannot start without one.
After=network-online.target shift-agent-xvfb.service
Requires=shift-agent-xvfb.service

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment=DISPLAY=${DISPLAY_NUM}
ExecStart=${APP_DIR}/.venv/bin/python -m shift_agent.main run --config ${CONFIG_PATH}
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

info "wrote shift-agent-xvfb, shift-agent-vnc and shift-agent services"

if [ "$HAVE_SYSTEMD" -eq 1 ]; then
    systemctl daemon-reload
    systemctl enable --now shift-agent-xvfb.service >/dev/null 2>&1
    systemctl enable --now shift-agent-vnc.service >/dev/null 2>&1
    systemctl enable shift-agent.service >/dev/null 2>&1
    info "screen and remote view are running and will restart on reboot"
    info "the agent itself is NOT started yet - it needs your settings first"
else
    warn "systemd is not running here (normal inside a container)."
    warn "Service files were written but nothing was started."
fi

# --------------------------------------------------------------------------
# What happens next
# --------------------------------------------------------------------------

SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$SERVER_IP" ] || SERVER_IP="YOUR_SERVER_IP"

cat <<EOF

${GREEN}${BOLD}Setup finished.${OFF}

${BOLD}No firewall ports were opened.${OFF} Nothing on this server is reachable from
the internet except SSH. That is on purpose.

${BOLD}Your VNC password${OFF} (needed in step 3) - read it any time with:

    sudo cat ${VNC_DIR}/passwd.txt

${BOLD}Next, in order:${OFF}

  1. Edit your settings:
         sudo -u ${APP_USER} nano ${APP_DIR}/${CONFIG_PATH}

  2. On YOUR OWN computer, open a SECOND PowerShell window and run:
         ssh -L ${VNC_PORT}:127.0.0.1:${VNC_PORT} root@${SERVER_IP}
     Leave it open. It is the tunnel. Sign in as root here - the ${APP_USER}
     account has no password on purpose, and the tunnel works the same either way.

  3. Open your VNC viewer and connect to:  127.0.0.1:${VNC_PORT}
     You will see an empty grey screen. That is correct - nothing is drawing yet.

  4. Back in your FIRST window, sign in to FLICA once:
         cd ${APP_DIR}
         sudo -u ${APP_USER} env DISPLAY=${DISPLAY_NUM} ${APP_DIR}/.venv/bin/python \\
             -m shift_agent.main run --config ${CONFIG_PATH} --dry-run --once
     Watch the VNC window: FLICA opens, you log in and clear the "confirm you
     are human" box. That sign-in is remembered from then on.

  5. Start it for real:
         sudo systemctl start shift-agent
         sudo systemctl status shift-agent

  Step-by-step version with screenshots and troubleshooting: ${APP_DIR}/docs/VPS.md

EOF
