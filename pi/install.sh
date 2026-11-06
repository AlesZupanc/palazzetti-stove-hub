#!/usr/bin/env bash
# install.sh - install or update the stove master on a Raspberry Pi (Raspberry Pi OS).
#
#   git clone https://github.com/AlesZupanc/palazzetti-stove-hub.git
#   cd palazzetti-stove-hub && sudo pi/install.sh
#
# Safe to re-run for updates: code is replaced, config and database are kept.
#   --reconfigure   run the setup wizard again
#   --port N        web port to use if port 80 is taken (default: ask; kept for later runs)
#   --auto-update   a systemd timer pulls the tested "deploy" branch from GitHub every minute
#                   and re-runs this script when it changed (--no-auto-update removes it)
#
# What it does:
#   * installs python3-cryptography (needed for remote access) if it is missing
#   * creates the system user "stove" and /var/lib/stove-master (database)
#   * copies the code and the web app to /opt/stove-master and adds the
#     "stove-master" command
#   * runs the setup wizard (Connection Box, stove.local, website address)
#     -> /etc/stove-master/stove.ini (readable by root and the stove group only)
#   * makes the local API token for programs on the Pi itself
#     (/etc/stove-master/local-api-token, see [web] local_token_file in stove.ini)
#   * installs and starts the systemd service "stove-master" and prints how to
#     add the stove to your online account
set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP=/opt/stove-master
ETC=/etc/stove-master
CONFIG=$ETC/stove.ini
DATA=/var/lib/stove-master
SVC_USER=stove
SERVICE=stove-master
UNIT=/etc/systemd/system/$SERVICE.service
WRAPPER=/usr/local/bin/stove-master
LOCAL_TOKEN=/etc/stove-master/local-api-token   # [web] local_token_file
RECONFIGURE=0
PORT_OVERRIDE=""
AUTO_UPDATE=""      # "", install or remove
AUTO_BRANCH=""
UPDATE_SERVICE=stove-master-update

if [ -t 1 ]; then B=$'\e[1m'; GRN=$'\e[32m'; YEL=$'\e[33m'; RED=$'\e[31m'; N=$'\e[0m'
else B=""; GRN=""; YEL=""; RED=""; N=""; fi
say()  { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '%s  ok%s %s\n' "$GRN" "$N" "$*"; }
warn() { printf '%swarn%s %s\n' "$YEL" "$N" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$RED" "$N" "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case $1 in
        --reconfigure) RECONFIGURE=1; shift ;;
        --port) PORT_OVERRIDE=${2:?--port needs a value}; shift 2 ;;
        --auto-update) AUTO_UPDATE=install; shift ;;
        --auto-update=*) AUTO_UPDATE=install; AUTO_BRANCH=${1#*=}; shift ;;
        --no-auto-update) AUTO_UPDATE=remove; shift ;;
        -h|--help) sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option '$1'" ;;
    esac
done
if [ -n "$PORT_OVERRIDE" ]; then
    [[ $PORT_OVERRIDE =~ ^[0-9]{1,5}$ ]] || die "--port needs a port number"
    PORT_OVERRIDE=$((10#$PORT_OVERRIDE))
    if [ "$PORT_OVERRIDE" -lt 1 ] || [ "$PORT_OVERRIDE" -gt 65535 ]; then die "--port needs a port number (1-65535)"; fi
fi

auto_update() {
    local updater="$REPO/tools/auto-update.sh" unit="/etc/systemd/system/$UPDATE_SERVICE.service"
    if [ "$AUTO_UPDATE" = remove ]; then
        if [ -f "$updater" ]; then
            bash "$updater" --remove "$UPDATE_SERVICE"
        elif [ -f "$unit" ]; then
            systemctl disable --now "$UPDATE_SERVICE.timer" >/dev/null 2>&1 || true
            rm -f "$unit" "${unit%.service}.timer"
            systemctl daemon-reload
            ok "$UPDATE_SERVICE removed - updates are manual again"
        fi
        return 0
    fi
    local refresh=0
    if [ -z "$AUTO_UPDATE" ]; then
        [ -f "$unit" ] || return 0     # not wanted
        refresh=1                      # installed earlier: keep its unit current
    fi
    say "Automatic updates from GitHub"
    [ -f "$updater" ] || die "automatic updates need a git clone of the repository (git clone https://github.com/AlesZupanc/palazzetti-stove-hub.git)"
    local args=(--install "$UPDATE_SERVICE")
    [ -n "$AUTO_BRANCH" ] && args+=(--branch "$AUTO_BRANCH")
    args+=(--request "$DATA/update-request" --status "$DATA/update-status.json")    # "Update now" on the web page
    if bash "$updater" "${args[@]}" -- /bin/bash "$REPO/pi/install.sh"; then return 0; fi
    if [ "$refresh" = 1 ]; then
        warn "could not refresh the automatic updates (see above) - the hub itself is installed"
        return 0
    fi
    die "automatic updates were not switched on"
}

# load_config: CFG_* from stove.ini (defaults for what is missing); CFG_PUBLIC is [web] public_port,
# the port browsers use when a proxy in front of the hub forwards it (empty: the hub's own)
load_config() {
    CFG_PORT=80 CFG_PUBLIC="" CFG_NAME=stove CFG_CLOUD=""
    eval "$("$PYTHON" - "$CONFIG" "$APP" <<'PY'
import configparser, shlex, sys
path, app = sys.argv[1:]
sys.path.insert(0, app)
from stovemaster import config
cp = configparser.ConfigParser(interpolation=None)
cp.read_dict(config.DEFAULTS)
cp.read(path, encoding="utf-8")
g = lambda section, key: cp.get(section, key).strip()
name = g("mdns", "name").lower().rstrip(".") or "stove"
name = name[:-6] if name.endswith(".local") else name
out = {"CFG_PORT": g("web", "port"), "CFG_PUBLIC": g("web", "public_port"), "CFG_NAME": name,
       "CFG_CLOUD": g("cloud", "url")}
print("\n".join(f"{k}={shlex.quote(v)}" for k, v in out.items()))
PY
)" || true
}

# set_port PORT: change [web] port in stove.ini, keep every other setting
set_port() {
    "$PYTHON" - "$CONFIG" "$APP" "$1" <<'PY' || return 1
import configparser, os, sys
path, app, port = sys.argv[1:]
sys.path.insert(0, app)
from stovemaster import config
os.umask(0o077)
cp = configparser.ConfigParser(interpolation=None)
cp.read_dict(config.DEFAULTS)
cp.read(path, encoding="utf-8")
if cp.get("web", "port").strip() == port:
    sys.exit(0)
values = {(s, k): cp.get(s, k) for s in config.DEFAULTS for k in config.DEFAULTS[s]}
values[("web", "port")] = port
with open(path + ".new", "w", encoding="utf-8") as f:
    f.write(config.render(values))
os.chmod(path + ".new", 0o640)
os.replace(path + ".new", path)
PY
    chown root:"$SVC_USER" "$CONFIG"
}

# http_ok URL: the URL answers 200 (asked directly, not through a proxy from the environment)
http_ok() {
    "$PYTHON" - "$1" <<'PY' 2>/dev/null
import sys, urllib.request
with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(sys.argv[1], timeout=3) as r:
    r.read()
PY
}

# port_owner PORT: nothing when the TCP port is free, "hub" when the stove master has it, else
# the name of the program that has it
port_owner() {
    command -v ss >/dev/null 2>&1 || return 0
    local line name pid hub
    line=$(ss -ltnpH "sport = :$1" 2>/dev/null | sed -n 1p) || true
    [ -n "$line" ] || return 0
    name=$(printf '%s\n' "$line" | sed -n 's/.*users:(("\([^"]*\)".*/\1/p')
    pid=$(printf '%s\n' "$line" | sed -n 's/.*users:(("[^"]*",pid=\([0-9]*\).*/\1/p')
    hub=$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null) || true
    if [ -n "$pid" ] && [ "$pid" = "$hub" ]; then echo hub; else echo "${name:-another program}"; fi
}

[ "$(id -u)" = 0 ] || die "run as root: sudo $0"
if [ "$AUTO_UPDATE" = remove ]; then
    auto_update
    exit 0
fi
# A run that does not get to the end (stopped part-way, or the service did not start) asks
# tools/auto-update.sh to run it again later: .git/stove-auto-update.retry says why. Removed at
# the end.
RETRY_FILE=""
if [ -d "$REPO/.git" ]; then RETRY_FILE=$REPO/.git/stove-auto-update.retry; fi
retry_later() {   # retry_later [WHY]: ask for another run (WHY), or no longer (nothing)
    [ -n "$RETRY_FILE" ] || return 0
    if [ -n "${1:-}" ]; then printf '%s\n' "$1" >"$RETRY_FILE" 2>/dev/null || true; else rm -f "$RETRY_FILE"; fi
}
retry_later "the last run of pi/install.sh did not finish"
PYTHON=$(command -v python3) || die "python3 is missing (sudo apt install python3)"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 7))' || die "python3 >= 3.7 is required"
"$PYTHON" -c 'import sqlite3' 2>/dev/null || die "python3 lacks sqlite3 (sudo apt install python3)"
"$PYTHON" -c 'import sqlite3, sys; sys.exit(sqlite3.sqlite_version_info < (3, 24, 0))' ||
    die "SQLite 3.24 or newer is required (Raspberry Pi OS Buster or newer)"
command -v systemctl >/dev/null 2>&1 || die "systemd is required"
for f in palazzetti.py stove_master.py stovemaster/__init__.py www/app/index.html pi/stove-master.service; do
    [ -f "$REPO/$f" ] || die "missing $REPO/$f - run this from a full checkout of the repository"
done

say "Python packages"
if "$PYTHON" -c 'import cryptography' 2>/dev/null; then
    ok "cryptography is installed"
elif command -v apt-get >/dev/null 2>&1; then
    apt-get install -y python3-cryptography >/dev/null || warn "could not install python3-cryptography - remote access stays off"
    "$PYTHON" -c 'import cryptography' 2>/dev/null && ok "installed python3-cryptography"
else
    warn "install the Python 'cryptography' package for remote access"
fi

say "System user and folders"
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
    ok "created user $SVC_USER"
fi
install -d -m 750 -o "$SVC_USER" -g "$SVC_USER" "$DATA"
install -d -m 755 "$ETC"
ok "$DATA (database), $ETC (config)"

say "Installing the code to $APP"
install -d -m 755 "$APP"
install -m 644 "$REPO/palazzetti.py" "$REPO/stove_master.py" "$REPO/stove_web.py" "$REPO/cbox_probe.py" "$APP/"
for part in stovemaster:stovemaster www/app:app; do      # replace whole folders: removed files disappear
    src=${part%%:*}; dst=${part#*:}
    rm -rf "$APP/$dst.new"
    cp -r "$REPO/$src" "$APP/$dst.new"
    find "$APP/$dst.new" -name __pycache__ -prune -exec rm -rf {} +
    chmod -R u=rwX,go=rX "$APP/$dst.new"
    rm -rf "${APP:?}/${dst:?}"
    mv "$APP/$dst.new" "$APP/$dst"
done
cat >"$WRAPPER" <<EOF
#!/bin/sh
# stove-master: runs $APP/stove_master.py with the system config.
# As root it switches to the "$SVC_USER" user (except for "setup") so the
# database files keep the right owner.
CONFIG=$CONFIG
if [ "\$(id -u)" = 0 ] && [ "\${1:-}" != "setup" ]; then
    exec runuser -u $SVC_USER -- $PYTHON $APP/stove_master.py --config "\$CONFIG" "\$@"
fi
exec $PYTHON $APP/stove_master.py --config "\$CONFIG" "\$@"
EOF
chmod 755 "$WRAPPER"
ok "code installed, command: sudo stove-master --help"

say "Configuration"
if [ ! -f "$CONFIG" ] || [ "$RECONFIGURE" = 1 ]; then
    STOVE_INSTALLER=1 "$WRAPPER" setup      # this run restarts the service itself, below
else
    ok "keeping $CONFIG (run with --reconfigure to change it)"
fi
chown root:"$SVC_USER" "$CONFIG"
chmod 640 "$CONFIG"
# The secret of the local API for programs on the Pi itself: made once, readable by the hub
# (and root); never shown anywhere (the dashboard's token is another one)
if [ ! -s "$LOCAL_TOKEN" ]; then
    (umask 077 && "$PYTHON" -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(24))' >"$LOCAL_TOKEN.new")
    mv -f "$LOCAL_TOKEN.new" "$LOCAL_TOKEN"
    ok "made the local API token $LOCAL_TOKEN"
fi
chown root:"$SVC_USER" "$LOCAL_TOKEN"
chmod 640 "$LOCAL_TOKEN"
load_config

say "Service"
sed "s|/usr/bin/python3|$PYTHON|" "$REPO/pi/stove-master.service" >"$UNIT"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
port=${PORT_OVERRIDE:-$CFG_PORT}
if [ -n "$PORT_OVERRIDE" ]; then
    set_port "$port" || warn "could not change $CONFIG"
fi
# Port 80 already taken (lighttpd, apache, nginx, pi-hole...)? Then use another one: the hub
# would only crash-loop on it. Decided by who has the port, not by whether the hub runs.
owner=$(port_owner "$port")
if [ -z "$PORT_OVERRIDE" ] && [ -n "$owner" ] && [ "$owner" != hub ]; then
    warn "port $port is already used by $owner"
    read -r -p "Use port 8080 for the stove app instead? [Y/n] " a </dev/tty || a=y
    if [[ ! $a =~ ^[Nn] ]]; then
        port=8080
        set_port "$port"
        ok "web port set to 8080 (http://stove.local:8080/)"
    fi
fi
systemctl restart "$SERVICE"
for _ in $(seq 1 30); do
    if http_ok "http://127.0.0.1:$port/api/health"; then
        ok "service $SERVICE is running"
        break
    fi
    sleep 1
done
systemctl is-active --quiet "$SERVICE" || { journalctl -u "$SERVICE" -n 30 --no-pager; die "the service did not start"; }
auto_update
retry_later

if [ "$(timedatectl show -p NTP --value 2>/dev/null || echo yes)" != "yes" ]; then
    warn "network time sync is off - the schedule needs a correct clock: sudo timedatectl set-ntp true"
fi

ip=$(hostname -I 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) {print $i; exit}}') || true
[ -n "$ip" ] || ip="<the Pi's IP address>"
shown=${CFG_PUBLIC:-$port}
suffix=""; [ "$shown" != 80 ] && suffix=":$shown"
cat <<EOF

${GRN}${B}Done.${N} Open  ${B}http://$CFG_NAME.local$suffix/${N}  (or http://$ip$suffix/) on your phone or PC
at home - no password needed on the home network.
EOF
if [ -n "$CFG_CLOUD" ]; then
    cat <<EOF

To reach the stove from anywhere: on the local dashboard open ${B}Online${N} and tap
"Open the website with the token" (or: sudo stove-master token), sign in at
$CFG_CLOUD and confirm "Add".
EOF
fi
cat <<EOF

  logs:      journalctl -u $SERVICE -f
  settings:  sudo stove-master setup && sudo systemctl restart $SERVICE
  online:    sudo stove-master token  |  sudo stove-master links
  update:    git pull && sudo pi/install.sh   (by itself: sudo pi/install.sh --auto-update)
EOF
