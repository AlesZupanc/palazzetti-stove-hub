#!/usr/bin/env bash
# deploy.sh - install or update the stove website on a VPS that already runs nginx.
#
#   sudo ./deploy.sh                         # asks which of your HTTPS sites to use
#   sudo ./deploy.sh --domain example.com    # URL path defaults to /mystove
#   sudo ./deploy.sh --dry-run               # only show what would change
#   sudo ./deploy.sh --uninstall [--purge]   # remove it again
#   sudo ./deploy.sh --auto-update           # + follow the tested "deploy" branch on GitHub by itself
#
# It leaves your existing nginx routing alone and is safe to re-run:
#   1. reads the live nginx config (nginx -T) and finds the HTTPS server block
#      of the domain; refuses if that block already routes the path;
#   2. installs the website (stove_cloud.py, stovecloud/ and the app) to
#      /opt/stove-cloud as a hardened systemd service (user stove-cloud,
#      127.0.0.1 only); accounts are kept in /var/lib/stove-cloud;
#   3. on the first run asks for the administrator and how to send e-mail: from
#      this server itself (mail-server.sh) or through an e-mail provider (SMTP);
#   4. writes /etc/nginx/snippets/stove-cloud.conf (incl. the WebSocket route)
#      and adds ONE include line to that server block, after a backup to
#      /var/backups/stove-cloud;
#   5. runs nginx -t, rolls back automatically if it fails, else reloads nginx;
#   6. checks https://<domain><path>/ and prints the administrator's link to
#      choose a password, and what to enter on the Raspberry Pi.
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOMAIN=""
PREFIX="/mystove"
PORT="8471"
SERVER_FILE=""
ADMIN_USER=""
ADMIN_EMAIL=""
SKIP_MAIL=0
OWN_MAIL=0
ASSUME_YES=0
DRY_RUN=0
UNINSTALL=0
PURGE=0
SETUP_LINK=""
AUTO_UPDATE=""      # "", install or remove
AUTO_BRANCH=""

INSTALL_DIR=/opt/stove-cloud
CONF_DIR=/etc/stove-cloud
CONFIG=$CONF_DIR/cloud.ini
DATA_DIR=/var/lib/stove-cloud
SERVICE=stove-cloud
SVC_USER=stove-cloud
WRAPPER=/usr/local/bin/stove-cloud
SNIPPET_DIR=/etc/nginx/snippets
SNIPPET=$SNIPPET_DIR/stove-cloud.conf
BACKUP_DIR=/var/backups/stove-cloud
UNIT=/etc/systemd/system/$SERVICE.service
UPDATE_SERVICE=stove-cloud-update
NGINX=${NGINX:-nginx}

if [ -t 1 ]; then B=$'\e[1m'; RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; N=$'\e[0m'
else B=""; RED=""; GRN=""; YEL=""; N=""; fi
say()  { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '%s  ok%s %s\n' "$GRN" "$N" "$*"; }
info() { printf '     %s\n' "$*"; }
warn() { printf '%swarn%s %s\n' "$YEL" "$N" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$RED" "$N" "$*" >&2; exit 1; }
trap 'warn "deploy.sh stopped at line $LINENO"' ERR

have_tty() { { : </dev/tty; } 2>/dev/null; }

confirm() {
    [ "$ASSUME_YES" = 1 ] && return 0
    have_tty || die "no terminal to ask \"$1\" - re-run with --yes"
    local a=""
    read -r -p "$1 [y/N] " a </dev/tty || true
    [[ $a =~ ^[Yy] ]]
}

ask() {  # ask "question" default -> REPLY
    local q=$1 d=${2:-} a=""
    have_tty || die "no terminal to ask \"$q\" - pass it as an option (see --help)"
    read -r -p "$q${d:+ [$d]}: " a </dev/tty || true
    REPLY=${a:-$d}
}

usage() {
    sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  --domain NAME        site to attach to (default: pick from nginx's HTTPS server_names)
  --path /PATH         URL path of the stove website (default /mystove)
  --port N             local port of the service (default 8471)
  --server-file F      only consider server blocks in this nginx file
  --admin-user NAME    administrator's user name (asked on the first run)
  --admin-email ADDR   administrator's e-mail; account requests are sent there
  --own-mail           send e-mail from this server itself (sets up Postfix + DKIM with mail-server.sh)
  --skip-mail          do not set up e-mail now (later: sudo ./mail-server.sh, or sudo stove-cloud setup-mail)
  --dry-run            print the nginx change and the snippet, change nothing
  --uninstall          remove the website, its service and the nginx include
  --purge              with --uninstall: also delete the config, ALL ACCOUNTS and the system user
  --auto-update[=BR]   a systemd timer pulls branch BR (default: deploy, moved by GitHub Actions when
                       the tests pass) and re-runs this script when it changed; needs a git clone here
  --no-auto-update     remove that timer again
  -y, --yes            do not ask
EOF
}

while [ $# -gt 0 ]; do
    case $1 in
        --domain) DOMAIN=${2:?--domain needs a value}; shift 2 ;;
        --domain=*) DOMAIN=${1#*=}; shift ;;
        --path) PREFIX=${2:?--path needs a value}; shift 2 ;;
        --path=*) PREFIX=${1#*=}; shift ;;
        --port) PORT=${2:?--port needs a value}; shift 2 ;;
        --port=*) PORT=${1#*=}; shift ;;
        --server-file) SERVER_FILE=${2:?--server-file needs a value}; shift 2 ;;
        --server-file=*) SERVER_FILE=${1#*=}; shift ;;
        --admin-user) ADMIN_USER=${2:?--admin-user needs a value}; shift 2 ;;
        --admin-user=*) ADMIN_USER=${1#*=}; shift ;;
        --admin-email) ADMIN_EMAIL=${2:?--admin-email needs a value}; shift 2 ;;
        --admin-email=*) ADMIN_EMAIL=${1#*=}; shift ;;
        --skip-mail) SKIP_MAIL=1; shift ;;
        --own-mail) OWN_MAIL=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        --purge) PURGE=1; shift ;;
        --auto-update) AUTO_UPDATE=install; shift ;;
        --auto-update=*) AUTO_UPDATE=install; AUTO_BRANCH=${1#*=}; shift ;;
        --no-auto-update) AUTO_UPDATE=remove; shift ;;
        -y|--yes) ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option '$1' (see --help)" ;;
    esac
done

PREFIX="/${PREFIX#/}"
PREFIX="${PREFIX%/}"
[[ $PREFIX =~ ^(/[A-Za-z0-9._-]+)+$ ]] || die "--path must look like /mystove"
if ! [[ $PORT =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1024 ] || [ "$PORT" -gt 65535 ]; then
    die "--port must be 1024-65535"
fi

# ----------------------------------------------------------------- helpers
patch_cfg() { "$PYTHON" "$HERE/nginx_patch.py" --nginx "$NGINX" "$@"; }
site() { "$WRAPPER" "$@"; }

json_get() {
    "$PYTHON" -c 'import json, sys
d = json.loads(sys.argv[1] or "{}")
v = d.get(sys.argv[2])
print("" if v is None else v if isinstance(v, str) else json.dumps(v))' "$1" "$2"
}

render() {
    sed -e "s|@DOMAIN@|$DOMAIN|g" -e "s|@PREFIX@|$PREFIX|g" -e "s|@PORT@|$PORT|g" \
        -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" -e "s|@SNIPPET@|$SNIPPET|g" -e "s|@CONFIG@|$CONFIG|g" \
        -e "s|@DATA_DIR@|$DATA_DIR|g" -e "s|@SVC_USER@|$SVC_USER|g" -e "s|@PYTHON@|$PYTHON|g" "$1"
}

local_health() {  # prints the website's health JSON, fails if it does not answer
    "$PYTHON" - "http://127.0.0.1:$PORT$PREFIX/_health" <<'EOF'
import sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=2) as r:
    body = r.read().decode()
if '"ok":true' not in body.replace(" ", ""):
    sys.exit(1)
print(body)
EOF
}

reload_nginx() {
    if systemctl is-active --quiet nginx 2>/dev/null; then systemctl reload nginx; else "$NGINX" -s reload; fi
}

# --------------------------------------------------------------- preflight
preflight() {
    [ "$(id -u)" = 0 ] || die "run as root: sudo $0"
    command -v "$NGINX" >/dev/null 2>&1 || die "nginx not found"
    PYTHON=$(command -v python3) || die "python3 not found (apt install python3)"
    "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 7))' || die "python3 >= 3.7 is required"
    "$PYTHON" -c 'import sqlite3, sys; sys.exit(sqlite3.sqlite_version_info < (3, 24, 0))' ||
        die "SQLite 3.24 or newer is required"
    command -v systemctl >/dev/null 2>&1 || die "systemd is required"
    local f
    for f in stove_cloud.py stovecloud/__init__.py app/index.html nginx_patch.py nginx-stove-cloud.conf.in \
             stove-cloud.service.in; do
        [ -f "$HERE/$f" ] || die "missing $HERE/$f - upload the whole www/ directory"
    done
    if ! "$NGINX" -t >/dev/null 2>&1; then
        "$NGINX" -t || true
        die "your current nginx configuration does not pass 'nginx -t' - fix that first"
    fi
}

crypto_package() {
    "$PYTHON" -c 'import cryptography' 2>/dev/null && return 0
    [ "$UNINSTALL" = 1 ] && return 0
    warn "the Python package 'cryptography' is missing - push notifications need it"
    local cmd=""
    if command -v apt-get >/dev/null 2>&1; then cmd="apt-get install -y python3-cryptography"
    elif command -v dnf >/dev/null 2>&1; then cmd="dnf install -y python3-cryptography"
    elif command -v yum >/dev/null 2>&1; then cmd="yum install -y python3-cryptography"
    fi
    if [ -n "$cmd" ] && confirm "Install it now ($cmd)?"; then
        $cmd >/dev/null && ok "python3-cryptography installed"
    else
        warn "continuing without push notifications (install python3-cryptography and re-run later)"
    fi
}

choose_domain() {
    if [ -z "$DOMAIN" ]; then
        local lines=() names=() others=() pick line i
        mapfile -t lines < <(patch_cfg servers --ssl-only --names)
        for line in "${lines[@]}"; do
            names+=("${line%%$'\t'*}")
            others+=("${line#*$'\t'}")
        done
        if [ ${#names[@]} -eq 0 ]; then
            die "nginx has no HTTPS site yet. Set one up first (e.g. certbot --nginx -d example.com) - the stove website needs HTTPS for its encryption."
        elif [ ${#names[@]} -eq 1 ]; then
            DOMAIN=${names[0]}
            info "HTTPS site found: $DOMAIN${others[0]:+ (also ${others[0]})}"
        elif [ "$ASSUME_YES" = 1 ] || ! have_tty; then
            die "nginx serves several HTTPS sites (${names[*]}) - pick one with --domain"
        else
            echo "HTTPS sites served by this nginx:"
            for i in "${!names[@]}"; do
                printf '  %d) %s%s\n' $((i + 1)) "${names[$i]}" "${others[$i]:+  (also ${others[$i]})}"
            done
            read -r -p "Put the stove website on which one? [1] " pick </dev/tty || true
            pick=${pick:-1}
            if [[ $pick =~ ^[0-9]+$ ]] && [ "$pick" -ge 1 ] && [ "$pick" -le ${#names[@]} ]; then
                DOMAIN=${names[$((pick - 1))]}
            else
                DOMAIN=$pick
            fi
        fi
    fi
    [[ $DOMAIN =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || die "invalid domain '$DOMAIN'"
}

ours_on_port() {  # our website already listens on this port (maybe for another --path)
    [ -f "$CONFIG" ] && grep -q "^port = $PORT$" "$CONFIG" && systemctl is-active --quiet "$SERVICE"
}

check_port() {
    if local_health >/dev/null 2>&1 || ours_on_port; then
        return 0  # our website already runs there (re-run)
    fi
    if command -v ss >/dev/null 2>&1 && ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q .; then
        die "port $PORT is already used by another program - choose another with --port"
    fi
}

# ------------------------------------------------------------------- nginx
PLAN_ARGS=()
plan_nginx() {
    say "Finding the HTTPS server block of $DOMAIN"
    PLAN_ARGS=(--domain "$DOMAIN" --prefix "$PREFIX" --include "$SNIPPET" --backup-dir "$BACKUP_DIR")
    [ -n "$SERVER_FILE" ] && PLAN_ARGS+=(--file "$SERVER_FILE")
    local plan
    if ! plan=$(patch_cfg plan "${PLAN_ARGS[@]}"); then
        local msg
        msg=$(json_get "$plan" error 2>/dev/null || true)
        die "${msg:-could not analyse the nginx configuration (see above)}"
    fi
    "$PYTHON" -c 'import json, sys
p = json.loads(sys.argv[1])
for t in p["targets"]:
    print("     add include to  %s:%s  (server_name %s; listen %s)" % (t["file"], t["line"], " ".join(t["names"]), ", ".join(t["listen"])))
for t in p["already"]:
    print("     already present  %s:%s  (server_name %s)" % (t["file"], t["line"], " ".join(t["names"])))' "$plan"
    if [ "$DRY_RUN" = 1 ]; then
        say "Dry run - nginx changes"
        patch_cfg apply "${PLAN_ARGS[@]}" --dry-run || true
        say "Dry run - $SNIPPET would contain"
        render "$HERE/nginx-stove-cloud.conf.in"
        exit 0
    fi
    confirm "Publish the stove website at https://$DOMAIN$PREFIX/ like this?" || die "aborted, nothing changed"
}

SNIP_BACKUP=""
restore_snippet() {
    if [ -n "$SNIP_BACKUP" ] && [ -f "$SNIP_BACKUP" ]; then mv -f "$SNIP_BACKUP" "$SNIPPET"; else rm -f "$SNIPPET"; fi
    SNIP_BACKUP=""
}

validate_snippet() {
    # Check the generated snippet with nginx in a throw-away config before the live one is touched.
    local dir
    dir=$(mktemp -d)
    mkdir -p "$dir/tmp"
    render "$HERE/nginx-stove-cloud.conf.in" >"$dir/snippet.conf"
    cat >"$dir/nginx.conf" <<CONF
pid $dir/nginx.pid;
error_log $dir/error.log;
events {}
http {
    access_log off;
    client_body_temp_path $dir/tmp/body;
    proxy_temp_path $dir/tmp/proxy;
    fastcgi_temp_path $dir/tmp/fastcgi;
    uwsgi_temp_path $dir/tmp/uwsgi;
    scgi_temp_path $dir/tmp/scgi;
    server {
        listen 127.0.0.1:9;
        server_name snippet.test;
        include $dir/snippet.conf;
    }
}
CONF
    if ! "$NGINX" -t -p "$dir/" -c "$dir/nginx.conf" >"$dir/out" 2>&1; then
        cat "$dir/out" >&2
        rm -rf "$dir"
        die "the generated nginx snippet does not pass nginx -t - nothing was changed"
    fi
    rm -rf "$dir"
}

NGINX_DONE=1          # 0 while the live config is being changed: any exit then rolls back
ROLLBACK_MANIFEST=""
rollback_nginx() {
    [ "$NGINX_DONE" = 1 ] && return 0
    NGINX_DONE=1
    warn "rolling back the nginx changes"
    if [ -n "$ROLLBACK_MANIFEST" ]; then patch_cfg restore --manifest "$ROLLBACK_MANIFEST" >/dev/null 2>&1 || true; fi
    restore_snippet
}
trap rollback_nginx EXIT
trap 'exit 130' INT TERM HUP

apply_nginx() {
    say "Updating nginx"
    validate_snippet
    local out msg test_log
    NGINX_DONE=0
    # 1) the snippet first: harmless as long as nothing includes it
    install -d -m 755 "$SNIPPET_DIR"
    if [ -f "$SNIPPET" ]; then SNIP_BACKUP=$(mktemp); cp -p "$SNIPPET" "$SNIP_BACKUP"; fi
    render "$HERE/nginx-stove-cloud.conf.in" >"$SNIPPET.new"
    chmod 644 "$SNIPPET.new"
    mv -f "$SNIPPET.new" "$SNIPPET"
    # 2) then the include line
    if ! out=$(patch_cfg apply "${PLAN_ARGS[@]}"); then
        msg=$(json_get "$out" error 2>/dev/null || true)
        die "could not edit the nginx config: ${msg:-see the message above}"
    fi
    ROLLBACK_MANIFEST=$(json_get "$out" manifest)
    [ -n "$ROLLBACK_MANIFEST" ] && info "backup of the edited file(s): $(dirname "$ROLLBACK_MANIFEST")"
    # 3) test, then reload
    test_log=$(mktemp)
    if ! "$NGINX" -t >"$test_log" 2>&1; then
        cat "$test_log" >&2
        rm -f "$test_log"
        rollback_nginx
        if "$NGINX" -t >/dev/null 2>&1; then
            die "nginx -t failed with the change - rolled back, nothing was reloaded."
        fi
        die "nginx -t still fails after the rollback - compare with the backups in $BACKUP_DIR"
    fi
    rm -f "$test_log"
    reload_nginx
    NGINX_DONE=1
    if [ -n "$SNIP_BACKUP" ]; then rm -f "$SNIP_BACKUP"; fi
    SNIP_BACKUP=""
    ok "nginx reloaded: https://$DOMAIN$PREFIX/ -> 127.0.0.1:$PORT"
}

selinux() {
    command -v getenforce >/dev/null 2>&1 || return 0
    [ "$(getenforce 2>/dev/null)" = Enforcing ] || return 0
    getsebool httpd_can_network_connect 2>/dev/null | grep -q 'on$' && return 0
    warn "SELinux does not let nginx connect to local ports (httpd_can_network_connect is off)"
    if confirm "Allow it now (setsebool -P httpd_can_network_connect 1)?"; then
        setsebool -P httpd_can_network_connect 1
    else
        warn "nginx will answer 502 until you run: setsebool -P httpd_can_network_connect 1"
    fi
}

# ----------------------------------------------------------------- website
install_site() {
    say "Installing the website"
    id -u "$SVC_USER" >/dev/null 2>&1 ||
        useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$SVC_USER"
    install -d -m 755 "$INSTALL_DIR"
    install -d -m 750 -o root -g "$SVC_USER" "$CONF_DIR"
    install -d -m 750 -o "$SVC_USER" -g "$SVC_USER" "$DATA_DIR"
    install -m 644 "$HERE/stove_cloud.py" "$INSTALL_DIR/"
    [ -f "$HERE/README.md" ] && install -m 644 "$HERE/README.md" "$INSTALL_DIR/"
    local d
    for d in stovecloud app; do       # replace whole folders, so removed files disappear too
        rm -rf "$INSTALL_DIR/$d.new"
        cp -r "$HERE/$d" "$INSTALL_DIR/$d.new"
        find "$INSTALL_DIR/$d.new" -name __pycache__ -prune -exec rm -rf {} +
        chmod -R u=rwX,go=rX "$INSTALL_DIR/$d.new"
        rm -rf "${INSTALL_DIR:?}/${d:?}"
        mv "$INSTALL_DIR/$d.new" "$INSTALL_DIR/$d"
    done
    cat >"$WRAPPER" <<EOF
#!/bin/sh
# stove-cloud: runs $INSTALL_DIR/stove_cloud.py with the system config. As root it
# switches to the "$SVC_USER" user (except for the config commands) so the
# database keeps the right owner.
CONFIG=$CONFIG
case "\${1:-}" in init-config|setup-mail) exec $PYTHON $INSTALL_DIR/stove_cloud.py --config "\$CONFIG" "\$@" ;; esac
if [ "\$(id -u)" = 0 ]; then
    exec runuser -u $SVC_USER -- $PYTHON $INSTALL_DIR/stove_cloud.py --config "\$CONFIG" "\$@"
fi
exec $PYTHON $INSTALL_DIR/stove_cloud.py --config "\$CONFIG" "\$@"
EOF
    chmod 755 "$WRAPPER"
    site init-config --public-url "https://$DOMAIN$PREFIX/" --port "$PORT" --db "$DATA_DIR/cloud.db" \
        --app "$INSTALL_DIR/app" >/dev/null
    chown root:"$SVC_USER" "$CONFIG"
    chmod 640 "$CONFIG"
    ok "files in $INSTALL_DIR, config $CONFIG, command: sudo stove-cloud --help"
}

ask_admin() {
    if site has-admin 2>/dev/null; then
        ADMIN_EMAIL=""
        return 0
    fi
    say "Administrator"
    info "The administrator approves new accounts and gets their requests by e-mail."
    local interactive=0
    if have_tty && [ "$ASSUME_YES" != 1 ]; then interactive=1; fi
    if [ -z "$ADMIN_USER" ]; then
        if [ "$interactive" = 1 ]; then ask "   User name" "admin"; ADMIN_USER=$REPLY; else ADMIN_USER="admin"; fi
    fi
    [[ $ADMIN_USER =~ ^[A-Za-z0-9_.-]{3,32}$ ]] || die "user name: 3-32 letters, digits and . _ -"
    if [ -z "$ADMIN_EMAIL" ] && [ "$interactive" = 0 ]; then
        die "no administrator yet - pass --admin-email (and --admin-user)"
    fi
    while [ -z "$ADMIN_EMAIL" ]; do ask "   E-mail address"; ADMIN_EMAIL=$REPLY; done
    [[ $ADMIN_EMAIL == *@*.* ]] || die "'$ADMIN_EMAIL' does not look like an e-mail address"
}

mail_setup() {
    if grep -q '^smtp_host = .\+' "$CONFIG"; then
        ok "e-mail is set up ($(sed -n 's/^smtp_host = //p' "$CONFIG")); to change it: sudo $HERE/mail-server.sh" \
           "(this server) or sudo stove-cloud setup-mail (a provider)"
        return 0
    fi
    if [ "$OWN_MAIL" = 1 ]; then
        own_mail
        return 0
    fi
    if [ "$SKIP_MAIL" = 1 ] || ! have_tty || [ "$ASSUME_YES" = 1 ]; then
        warn "no e-mail set up: account requests and links are written to the log (journalctl -u $SERVICE)"
        info "set it up later: sudo $HERE/mail-server.sh (from this server) or sudo stove-cloud setup-mail (a provider)"
        return 0
    fi
    say "E-mail"
    info "The website e-mails you account requests, and users their approval and password reset links."
    info "  1  send from this server itself (a small send-only mail server; you add a few DNS records)"
    info "  2  send through an e-mail provider (Gmail, your mail hosting, ... - SMTP server and password)"
    info "  3  later (until then the links are written to the log)"
    ask "   Choose" "1"
    case $REPLY in
        1) own_mail ;;
        2)
            site setup-mail ${ADMIN_EMAIL:+--test-to "$ADMIN_EMAIL"} </dev/tty
            chown root:"$SVC_USER" "$CONFIG"
            chmod 640 "$CONFIG"
            ;;
        *) info "later: sudo $HERE/mail-server.sh (from this server) or sudo stove-cloud setup-mail (a provider)" ;;
    esac
}

own_mail() {
    local args=(--domain "${DOMAIN#www.}")
    [ -n "$ADMIN_EMAIL" ] && args+=(--test-to "$ADMIN_EMAIL")
    [ "$ASSUME_YES" = 1 ] && args+=(--yes)
    if ! bash "$HERE/mail-server.sh" "${args[@]}"; then
        warn "e-mail is not set up (see above); until it is, links are written to the log (journalctl -u $SERVICE)"
        info "try again later: sudo $HERE/mail-server.sh   (or use a provider: sudo stove-cloud setup-mail)"
    fi
}

start_site() {
    render "$HERE/stove-cloud.service.in" >"$UNIT.new"
    mv -f "$UNIT.new" "$UNIT"
    systemctl daemon-reload
    systemctl enable "$SERVICE" >/dev/null 2>&1
    systemctl restart "$SERVICE"
    local i
    for i in $(seq 1 20); do
        if local_health >/dev/null 2>&1; then
            ok "service $SERVICE runs on 127.0.0.1:$PORT"
            return 0
        fi
        sleep 0.5
    done
    journalctl -u "$SERVICE" -n 20 --no-pager 2>/dev/null || true
    die "the website service did not start"
}

create_admin() {
    [ -n "$ADMIN_EMAIL" ] || return 0
    local out
    out=$(site admin "$ADMIN_USER" "$ADMIN_EMAIL") || die "could not create the administrator"
    SETUP_LINK=$(printf '%s\n' "$out" | grep -o 'https\?://[^ ]*#/setup/[A-Za-z0-9_-]*' | head -n1)
}

verify() {
    say "Checking the route"
    local base="https://$DOMAIN$PREFIX" body code
    if ! command -v curl >/dev/null 2>&1; then
        warn "curl is not installed - open $base/ in a browser to check"
        return 0
    fi
    # 1) through this machine's own nginx (no DNS/CDN involved; certificate not checked here).
    #    A reload is asynchronous, so give the new workers a few seconds.
    local i routed=0
    for i in 1 2 3 4 5 6 7 8 9 10; do
        body=$(curl -fsSk --max-time 5 --noproxy '*' --resolve "$DOMAIN:443:127.0.0.1" "$base/_health" 2>/dev/null) || true
        if [[ $body == *'"ok":true'* ]]; then routed=1; break; fi
        sleep 1
    done
    if [ "$routed" = 1 ]; then
        ok "nginx routes $PREFIX/ to the website: $body"
        # the WebSocket route must reach the service too (it answers 401 without device credentials)
        code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 --noproxy '*' --http1.1 \
            --resolve "$DOMAIN:443:127.0.0.1" -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
            -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' "$base/ws/device" || true)
        if [ "$code" = 401 ]; then ok "WebSocket route works ($PREFIX/ws/)"
        else warn "the WebSocket route $PREFIX/ws/ answered $code instead of 401 - check the nginx include"; fi
    else
        warn "the local nginx did not answer $base/_health (does it listen on 127.0.0.1:443?)"
    fi
    # 2) the public way, with certificate checks
    body=$(curl -fsS --max-time 10 "$base/_health" 2>/dev/null) || true
    if [[ $body == *'"ok":true'* ]]; then
        ok "reachable from the internet with a valid certificate: $base/"
    else
        warn "$base/ is not reachable from this machine with a valid certificate (DNS, firewall, CDN?) - try it in a browser"
    fi
}

summary() {
    cat <<EOF

${GRN}${B}Done.${N} The stove website is published at  ${B}https://$DOMAIN$PREFIX/${N}
EOF
    if [ -n "$SETUP_LINK" ]; then
        cat <<EOF

${B}Administrator:${N} open this link to choose your password (valid 7 days, works once):
  $SETUP_LINK
EOF
    fi
    cat <<EOF

${B}On the Raspberry Pi${N} (sudo pi/install.sh, or: sudo stove-master setup) enter as website:
  https://$DOMAIN$PREFIX/
Then open the stove's page at home (http://stove.local) -> Online and use its link,
or enter its token on the website under Stoves -> Add a stove.

  accounts:   sudo stove-cloud users        (approve, disable, ... see --help)
  e-mail:     sudo $HERE/mail-server.sh --check    (own mail server: DNS records, test)
              sudo stove-cloud setup-mail && sudo systemctl restart $SERVICE   (an e-mail provider)
  new admin link:  sudo stove-cloud admin NAME EMAIL
  logs:       journalctl -u $SERVICE -f
EOF
    if [ -f "/etc/systemd/system/$UPDATE_SERVICE.timer" ]; then
        echo "  updates:    automatic (journalctl -u $UPDATE_SERVICE); off: sudo $HERE/deploy.sh --no-auto-update"
    else
        echo "  updates:    git pull && sudo $HERE/deploy.sh      (automatic: sudo $HERE/deploy.sh --auto-update)"
    fi
}

# --------------------------------------------------------------- uninstall
uninstall() {
    say "Removing the nginx include"
    if [ "$DRY_RUN" = 1 ]; then
        patch_cfg remove --include "$SNIPPET" --backup-dir "$BACKUP_DIR" --dry-run
        exit 0
    fi
    confirm "Remove the stove website from this server?" || die "aborted"
    if [ "$PURGE" = 1 ]; then
        confirm "--purge deletes ALL ACCOUNTS ($DATA_DIR) and the config. Really?" || die "aborted"
    fi
    local out manifest
    out=$(patch_cfg remove --include "$SNIPPET" --backup-dir "$BACKUP_DIR")
    manifest=$(json_get "$out" manifest)
    if [ -f "$SNIPPET" ]; then SNIP_BACKUP=$(mktemp); cp -p "$SNIPPET" "$SNIP_BACKUP"; rm -f "$SNIPPET"; fi
    if ! "$NGINX" -t >/dev/null 2>&1; then
        [ -n "$manifest" ] && patch_cfg restore --manifest "$manifest" >/dev/null
        restore_snippet
        die "nginx -t failed without the include (is $SNIPPET included elsewhere by hand?) - restored"
    fi
    rm -f "$SNIP_BACKUP"
    reload_nginx
    ok "nginx no longer routes $PREFIX"
    say "Removing the website service"
    AUTO_UPDATE=remove auto_update
    systemctl disable --now "$SERVICE" >/dev/null 2>&1 || true
    rm -f "$UNIT" "$WRAPPER"
    systemctl daemon-reload
    rm -rf "$INSTALL_DIR"
    if [ "$PURGE" = 1 ]; then
        rm -rf "$CONF_DIR" "$DATA_DIR"
        userdel "$SVC_USER" >/dev/null 2>&1 || true
        ok "config, accounts and user $SVC_USER removed"
    else
        info "kept $CONFIG and the accounts in $DATA_DIR (add --purge to delete them)"
    fi
    ok "uninstalled (nginx backups stay in $BACKUP_DIR)"
}

# ------------------------------------------------------------- auto-update
auto_update() {
    local updater="$HERE/../tools/auto-update.sh" unit="/etc/systemd/system/$UPDATE_SERVICE.service"
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
        refresh=1                      # installed earlier: keep its unit current with this run's options
    fi
    say "Automatic updates from GitHub"
    [ -f "$updater" ] || die "automatic updates need a git clone of the whole repository on this server"         "(git clone https://github.com/AlesZupanc/palazzetti-stove-hub.git), not only the www/ folder"
    local cmd=(/bin/bash "$HERE/deploy.sh" --yes --domain "$DOMAIN" --path "$PREFIX" --port "$PORT")
    [ -n "$SERVER_FILE" ] && cmd+=(--server-file "$SERVER_FILE")
    local args=(--install "$UPDATE_SERVICE")
    [ -n "$AUTO_BRANCH" ] && args+=(--branch "$AUTO_BRANCH")
    args+=(--request "$DATA_DIR/update-request" --status "$DATA_DIR/update-status.json")    # "Update now" on the web page
    if bash "$updater" "${args[@]}" -- "${cmd[@]}"; then return 0; fi
    if [ "$refresh" = 1 ]; then
        warn "could not refresh the automatic updates (see above) - the website itself is deployed"
        return 0
    fi
    die "automatic updates were not switched on"
}

# -------------------------------------------------------------------- main
preflight
if [ "$AUTO_UPDATE" = remove ]; then
    auto_update
    exit 0
fi
if [ "$UNINSTALL" = 1 ]; then
    uninstall
    exit 0
fi
choose_domain
check_port
plan_nginx
crypto_package
install_site
ask_admin
mail_setup
start_site
create_admin
selinux
apply_nginx
verify
auto_update
summary
