#!/usr/bin/env bash
# mail-server.sh - let the stove website send its e-mail from this server itself.
#
#   sudo ./mail-server.sh             # set up (or update), then send a test e-mail
#   sudo ./mail-server.sh --check     # only check DNS, reverse DNS and port 25
#
# Sets up a send-only mail server on this VPS (Debian / Ubuntu):
#   * Postfix, listening on 127.0.0.1 only: it takes mail from programs on this
#     server (the website) and delivers it to the recipients' mail servers. It
#     accepts nothing from the internet, so nobody can misuse it as a relay.
#   * OpenDKIM: signs every mail from your domain, so receivers can tell it
#     really comes from you (DKIM).
#   * the website's e-mail settings: 127.0.0.1 port 25, sender stove@<domain>.
# Then it shows the DNS records that make receivers trust the mail - SPF, DKIM,
# DMARC and the reverse DNS (PTR) of the server's IP - and which are missing.
# It refuses to change a mail server that was set up for something else, and
# backs up what it changes to /var/backups/stove-cloud.
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOMAIN=""
FROM_ADDR=""
MAILHOST=""
IP=""
SELECTOR="stove"
TEST_TO=""
NO_TEST=0
CHECK=0
FORCE=0
ASSUME_YES=0

CONFIG=/etc/stove-cloud/cloud.ini
WRAPPER=/usr/local/bin/stove-cloud
SERVICE=stove-cloud
SVC_USER=stove-cloud
BACKUP_ROOT=/var/backups/stove-cloud/mail-$(date +%Y%m%d-%H%M%S)
DKIM_DIR=/etc/opendkim
DKIM_PORT=8891
MARKER="# stove website: send-only mail server (set up by mail-server.sh)"

if [ -t 1 ]; then B=$'\e[1m'; RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; N=$'\e[0m'
else B=""; RED=""; GRN=""; YEL=""; N=""; fi
say()  { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '%s  ok%s %s\n' "$GRN" "$N" "$*"; }
info() { printf '     %s\n' "$*"; }
warn() { printf '%swarn%s %s\n' "$YEL" "$N" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$RED" "$N" "$*" >&2; exit 1; }
trap 'warn "mail-server.sh stopped at line $LINENO"' ERR

have_tty() { { : </dev/tty; } 2>/dev/null; }

confirm() {  # confirm "question" [risky] - a risky change needs --force when not asked on a terminal
    if [ "$ASSUME_YES" = 1 ] && { [ "${2:-}" != risky ] || [ "$FORCE" = 1 ]; }; then return 0; fi
    have_tty || die "no terminal to ask \"$1\" - re-run with ${2:+--force }--yes"
    local a=""
    read -r -p "$1 [y/N] " a </dev/tty || true
    [[ $a =~ ^[Yy] ]]
}

usage() {
    sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  --domain NAME      domain the mail comes from (default: the website's domain)
  --from ADDR        sender address (default: stove@DOMAIN)
  --hostname NAME    name the server introduces itself with (default: its reverse DNS name)
  --ip ADDR          this server's public IPv4 address (default: found automatically)
  --selector NAME    DKIM selector, the "NAME._domainkey" DNS record (default: stove)
  --test-to ADDR     send the test e-mail there (default: the website's administrator)
  --no-test          do not send a test e-mail
  --check            change nothing; check port 25 and the DNS records
  --force            set up even when port 25 is blocked or another mail setup exists
  -y, --yes          do not ask
EOF
}

while [ $# -gt 0 ]; do
    case $1 in
        --domain) DOMAIN=${2:?--domain needs a value}; shift 2 ;;
        --domain=*) DOMAIN=${1#*=}; shift ;;
        --from) FROM_ADDR=${2:?--from needs a value}; shift 2 ;;
        --from=*) FROM_ADDR=${1#*=}; shift ;;
        --hostname) MAILHOST=${2:?--hostname needs a value}; shift 2 ;;
        --hostname=*) MAILHOST=${1#*=}; shift ;;
        --ip) IP=${2:?--ip needs a value}; shift 2 ;;
        --ip=*) IP=${1#*=}; shift ;;
        --selector) SELECTOR=${2:?--selector needs a value}; shift 2 ;;
        --selector=*) SELECTOR=${1#*=}; shift ;;
        --test-to) TEST_TO=${2:?--test-to needs a value}; shift 2 ;;
        --test-to=*) TEST_TO=${1#*=}; shift ;;
        --no-test) NO_TEST=1; shift ;;
        --check) CHECK=1; shift ;;
        --force) FORCE=1; shift ;;
        -y|--yes) ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option '$1' (see --help)" ;;
    esac
done
[[ $SELECTOR =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || die "--selector: letters, digits and -"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
dns() { "$PYTHON" "$HERE/mail_dns.py" "$@"; }

# ----------------------------------------------------------------- helpers
installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "ok installed"; }

backup() {  # backup FILE|DIR... into this run's backup folder
    local f
    for f in "$@"; do
        [ -e "$f" ] || continue
        install -d -m 700 "$BACKUP_ROOT"
        cp -a --parents "$f" "$BACKUP_ROOT/"
    done
}

listening() {  # listening PORT [banner-prefix]: something answers on 127.0.0.1:PORT
    "$PYTHON" - "$@" <<'PY'
import socket, sys
port, want = int(sys.argv[1]), (sys.argv[2] if len(sys.argv) > 2 else "")
try:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        if want:
            s.settimeout(10)
            sys.exit(0 if s.recv(200).decode("ascii", "replace").startswith(want) else 1)
except OSError:
    sys.exit(1)
PY
}

wait_listening() {
    local _
    for _ in $(seq 1 30); do
        listening "$@" && return 0
        sleep 0.5
    done
    return 1
}

restart() {
    systemctl enable "$1" >/dev/null 2>&1 || true
    systemctl restart "$1"
}

postfix_state() {  # none | ours | pristine | custom
    [ -f /etc/postfix/main.cf ] || { echo none; return; }
    if grep -qF "$MARKER" /etc/postfix/main.cf; then echo ours; return; fi
    # settings a fresh Debian/Ubuntu install writes; anything else means someone set Postfix up
    local defaults='alias_database|alias_maps|append_dot_mydomain|biff|compatibility_level|delay_warning_time'
    defaults+='|inet_interfaces|inet_protocols|mailbox_command|mailbox_size_limit|mydestination|myhostname'
    defaults+='|mynetworks|myorigin|readme_directory|recipient_delimiter|relayhost|smtp_tls_CApath|smtp_tls_CAfile'
    defaults+='|smtp_tls_security_level|smtp_tls_session_cache_database|smtpd_banner|smtpd_relay_restrictions'
    defaults+='|smtpd_tls_cert_file|smtpd_tls_key_file|smtpd_tls_security_level|smtpd_tls_session_cache_database'
    defaults+='|smtpd_use_tls|maillog_file'
    local extra
    extra=$(postconf -n 2>/dev/null | cut -d' ' -f1 | grep -vxE "$defaults" || true)
    if [ -n "$extra" ] || [ -n "$(postconf -h relayhost 2>/dev/null)" ]; then echo custom; else echo pristine; fi
}

opendkim_state() {  # none | ours | pristine | custom
    [ -f /etc/opendkim.conf ] || { echo none; return; }
    if grep -qF "$MARKER" /etc/opendkim.conf; then echo ours; return; fi
    if grep -qiE '^[[:space:]]*(KeyTable|SigningTable|KeyFile|Domain|Selector)[[:space:]]' /etc/opendkim.conf; then
        echo custom
    else
        echo pristine
    fi
}

mail_log() {  # recent Postfix log lines, wherever this system keeps them
    local f
    f=$(postconf -h maillog_file 2>/dev/null || true)
    if [ -n "$f" ] && [ -r "$f" ]; then tail -n 5000 "$f"
    elif [ -r /var/log/mail.log ]; then tail -n 5000 /var/log/mail.log
    else journalctl -q --no-pager -o short -n 5000 SYSLOG_FACILITY=2 2>/dev/null || true
    fi
}

# --------------------------------------------------------------- steps
preflight() {
    [ "$(id -u)" = 0 ] || die "run as root: sudo $0"
    PYTHON=$(command -v python3) || die "python3 not found"
    [ -f "$HERE/mail_dns.py" ] || die "missing $HERE/mail_dns.py - run the script from the www/ folder"
    [ "$CHECK" = 1 ] && return 0
    command -v apt-get >/dev/null 2>&1 ||
        die "this script supports Debian and Ubuntu. Elsewhere install Postfix and OpenDKIM yourself, then:
      sudo stove-cloud setup-mail --host 127.0.0.1 --port 25 --security none --from stove@YOUR-DOMAIN"
    command -v systemctl >/dev/null 2>&1 || die "systemd is required"
    if [ ! -x "$WRAPPER" ] || [ ! -f "$CONFIG" ]; then
        die "install the stove website first: sudo ./deploy.sh"
    fi
    "$WRAPPER" setup-mail --help 2>/dev/null | grep -q -- "--host" ||
        die "the installed website is older than this script - update it first: sudo ./deploy.sh"
}

find_domain() {
    if [ -z "$DOMAIN" ] && [ -r "$CONFIG" ]; then
        DOMAIN=$(sed -n 's#^public_url *= *https\{0,1\}://\([^/:]*\).*#\1#p' "$CONFIG" | head -n1)
        DOMAIN=${DOMAIN#www.}
    fi
    [ -n "$DOMAIN" ] || die "pass --domain (the domain the mail comes from, e.g. example.com)"
    DOMAIN=$(printf '%s' "${DOMAIN%.}" | tr '[:upper:]' '[:lower:]')
    [[ $DOMAIN =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$ ]] || die "'$DOMAIN' is not a domain name"
    FROM_ADDR=${FROM_ADDR:-stove@$DOMAIN}
    [[ ${FROM_ADDR,,} =~ ^[a-z0-9._+-]+@"$DOMAIN"$ ]] ||
        die "--from must be an address at $DOMAIN (the signature is made for $DOMAIN)"
    DKIM_TXT=$DKIM_DIR/keys/$DOMAIN/$SELECTOR.txt
}

find_ip() {
    if [ -z "$IP" ]; then
        IP=$(dns public-ip 2>"$TMP/how") || die "$(cat "$TMP/how")"
        info "public IPv4 address $IP ($(cat "$TMP/how"))"
    fi
    [[ $IP =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "--ip: '$IP' is not an IPv4 address"
}

local_firewall() {  # "ok", "ok-ufw", or why this server's own firewall blocks outgoing port 25
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        local policy
        policy=$(ufw status verbose 2>/dev/null | sed -n 's/^Default:.*[ ,]\([a-z]*\) (outgoing).*/\1/p')
        if [ -n "$policy" ] && [ "$policy" != allow ]; then
            echo "ufw blocks outgoing connections ($policy) - allow this one: sudo ufw allow out 25/tcp"
        elif ufw status 2>/dev/null | grep -Eq '^25(/tcp)?[[:space:]]+(DENY|REJECT)[[:space:]]+OUT'; then
            echo "a ufw rule blocks outgoing port 25 (see: sudo ufw status numbered)"
        else
            echo ok-ufw
        fi
        return 0
    fi
    local rules
    rules=$(iptables -S OUTPUT 2>/dev/null || true)
    if printf '%s\n' "$rules" | grep -q '^-P OUTPUT DROP'; then
        echo "iptables drops outgoing connections (-P OUTPUT DROP; see: sudo iptables -S OUTPUT)"
    elif printf '%s\n' "$rules" | grep -Eq -- '--dport (25|smtp)( |$).*-j (DROP|REJECT)'; then
        echo "an iptables rule blocks outgoing port 25 (see: sudo iptables -S OUTPUT)"
    else
        echo ok
    fi
}

check_port25() {
    say "Outgoing port 25"
    local out fw
    if out=$(dns port25 2>&1); then
        ok "this server can reach other mail servers ($out)"
        return 0
    fi
    warn "this server cannot reach other mail servers on port 25: $out"
    fw=$(local_firewall)
    case $fw in
        ok-ufw) info "This server's firewall (ufw) lets outgoing connections through, so the block is in" ;;
        ok) info "No firewall rule on this server blocks it, so the block is in" ;;
        *) warn "the block is on this server: $fw" ;;
    esac
    if [ "${fw#ok}" != "$fw" ]; then
        info "your provider's network. Providers open it on request (usually through their support; the"
        info "firewall in their panel normally covers incoming connections only, and this mail server"
        info "needs no incoming port). Most ask for reverse DNS (PTR) and an SPF record first (see DNS below)."
    fi
    if out=$(dns port587 2>&1); then
        info "Port 587 is open ($out), so until then the website can send through an"
        info "e-mail provider such as Gmail:  sudo stove-cloud setup-mail"
    else
        warn "port 587 is blocked too ($out): e-mail providers cannot be reached either - ask your provider"
    fi
    [ "$CHECK" = 1 ] && return 0
    if [ "$FORCE" = 1 ]; then
        warn "--force: setting up anyway; mail stays queued until port 25 is open"
    else
        PORT25_BLOCKED=1
    fi
}

safety_checks() {
    say "Existing mail software"
    local p other=""
    for p in exim4-base sendmail-bin opensmtpd nullmailer msmtp-mta ssmtp dma; do
        installed "$p" && other+="${other:+, }$p"
    done
    if [ -n "$other" ]; then
        local risky=risky
        if [ "$other" = exim4-base ] && grep -qs "^dc_eximconfig_configtype='local'" /etc/exim4/update-exim4.conf.conf; then
            risky=""      # Debian's default: local mail only
            info "Exim is installed for local mail only; Postfix replaces it (local mail keeps working)."
        else
            warn "another mail program is installed: $other"
        fi
        confirm "Replace $other with Postfix?" $risky || die "nothing was changed"
    fi
    case $(postfix_state) in
        none) ok "no mail server yet" ;;
        ours) ok "Postfix was set up by this script before - updating it" ;;
        pristine) ok "Postfix is installed with its default settings" ;;
        custom)
            warn "Postfix on this server was set up for something else:"
            postconf -n | sed 's/^/       /' >&2
            info "This script turns it into a send-only server for 127.0.0.1: it would no longer"
            info "receive mail from the internet. A backup goes to $BACKUP_ROOT."
            confirm "Change this Postfix setup?" risky || die "nothing was changed"
            ;;
    esac
    if [ "$(opendkim_state)" = custom ]; then
        warn "OpenDKIM on this server already signs mail (/etc/opendkim.conf has keys configured)"
        confirm "Replace its configuration (backup to $BACKUP_ROOT)?" risky || die "nothing was changed"
    fi
    if listening "$DKIM_PORT" && ! systemctl is-active --quiet opendkim; then
        die "port $DKIM_PORT on 127.0.0.1 is used by another program (OpenDKIM needs it)"
    fi
}

install_packages() {
    say "Installing Postfix and OpenDKIM"
    local pkgs=(postfix opendkim opendkim-tools) missing=() p
    command -v dig >/dev/null 2>&1 || pkgs+=(dnsutils)
    for p in "${pkgs[@]}"; do installed "$p" || missing+=("$p"); done
    if [ ${#missing[@]} = 0 ]; then
        ok "already installed"
        return 0
    fi
    printf '%s\n' "postfix postfix/main_mailer_type select Internet Site" \
                  "postfix postfix/mailname string $DOMAIN" | debconf-set-selections
    if ! DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${missing[@]}" >"$TMP/apt.log" 2>&1; then
        info "updating the package lists"
        apt-get update -q >>"$TMP/apt.log" 2>&1 || true
        DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${missing[@]}" >>"$TMP/apt.log" 2>&1 ||
            { tail -n 20 "$TMP/apt.log" >&2; die "apt-get install ${missing[*]} failed"; }
    fi
    ok "installed ${missing[*]}"
}

choose_hostname() {
    # the name Postfix greets other servers with; it should be the reverse DNS name of the IP
    local how ptr
    if [ -n "$MAILHOST" ]; then
        how="--hostname"
    elif [ "$CHECK" = 1 ] && [ "$(postfix_state)" = ours ]; then
        MAILHOST=$(postconf -h myhostname); how="what Postfix uses now"
    elif ptr=$(dns ptr "$IP" 2>/dev/null) && [ -n "$ptr" ]; then
        MAILHOST=$ptr; how="the reverse DNS name of $IP"
    elif [ "$(postfix_state)" = ours ] && [ "$(postconf -h myhostname)" != localhost ]; then
        MAILHOST=$(postconf -h myhostname); how="kept from the last run"
    elif dns a "$DOMAIN" 2>/dev/null | grep -qxF "$IP"; then
        MAILHOST=$DOMAIN; how="$DOMAIN points to this server"
    else
        MAILHOST=mail.$DOMAIN; how="$DOMAIN does not point to $IP (a CDN in front?)"
    fi
    MAILHOST=$(printf '%s' "${MAILHOST%.}" | tr '[:upper:]' '[:lower:]')
    [[ $MAILHOST =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$ ]] || die "'$MAILHOST' is not a host name"
    info "the mail server's name: $MAILHOST ($how)"
}

setup_dkim() {
    say "DKIM signatures (OpenDKIM)"
    local dir=$DKIM_DIR/keys/$DOMAIN
    install -d -m 750 -o opendkim -g opendkim "$DKIM_DIR" "$DKIM_DIR/keys" "$dir"
    if [ -s "$dir/$SELECTOR.private" ] && [ -s "$dir/$SELECTOR.txt" ]; then
        ok "keeping the key $dir/$SELECTOR.private (it is in DNS already, or will be)"
    else
        opendkim-genkey -b 2048 -h sha256 -s "$SELECTOR" -d "$DOMAIN" -D "$dir"
        ok "made a new 2048-bit key: $dir/$SELECTOR.private"
    fi
    chown opendkim:opendkim "$dir/$SELECTOR.private" "$dir/$SELECTOR.txt"
    chmod 600 "$dir/$SELECTOR.private"
    backup /etc/opendkim.conf /etc/default/opendkim "$DKIM_DIR/key.table" "$DKIM_DIR/signing.table" \
           "$DKIM_DIR/trusted.hosts"
    printf '%s._domainkey.%s %s:%s:%s\n' "$SELECTOR" "$DOMAIN" "$DOMAIN" "$SELECTOR" "$dir/$SELECTOR.private" \
        >"$DKIM_DIR/key.table"
    printf '*@%s %s._domainkey.%s\n' "$DOMAIN" "$SELECTOR" "$DOMAIN" >"$DKIM_DIR/signing.table"
    printf '127.0.0.1\n::1\nlocalhost\n' >"$DKIM_DIR/trusted.hosts"
    cat >/etc/opendkim.conf <<EOF
$MARKER
# Signs the mail from @$DOMAIN that programs on this server send through Postfix.
Syslog                  yes
UMask                   007
UserID                  opendkim
Mode                    s
Canonicalization        relaxed/simple
OversignHeaders         From
Socket                  inet:$DKIM_PORT@127.0.0.1
PidFile                 /run/opendkim/opendkim.pid
KeyTable                $DKIM_DIR/key.table
SigningTable            refile:$DKIM_DIR/signing.table
InternalHosts           $DKIM_DIR/trusted.hosts
EOF
    if [ -f /etc/default/opendkim ]; then      # older packages pass the socket on the command line
        sed -i "s|^SOCKET=.*|SOCKET=inet:$DKIM_PORT@127.0.0.1|" /etc/default/opendkim
        if [ -x /lib/opendkim/opendkim.service.generate ]; then
            /lib/opendkim/opendkim.service.generate && systemctl daemon-reload
        fi
    fi
    install -d -m 750 -o opendkim -g opendkim /run/opendkim
    restart opendkim
    wait_listening "$DKIM_PORT" || die "OpenDKIM does not listen on 127.0.0.1:$DKIM_PORT (see: journalctl -u opendkim)"
    ok "OpenDKIM signs mail from @$DOMAIN (selector $SELECTOR)"
}

setup_postfix() {
    say "Postfix (send-only)"
    choose_hostname
    backup /etc/postfix
    if ! grep -qF "$MARKER" /etc/postfix/main.cf; then
        { printf '%s\n%s\n\n' "$MARKER" "# It sends for programs on this server only: nothing is received from outside."
          cat /etc/postfix/main.cf; } >"$TMP/main.cf"
        cat "$TMP/main.cf" >/etc/postfix/main.cf
    fi
    # myorigin/mydestination = localhost: mail for "root" (cron, ...) stays on this server
    postconf -e \
        "myhostname = $MAILHOST" \
        "mydomain = $DOMAIN" \
        "myorigin = localhost" \
        "mydestination = localhost" \
        "relayhost =" \
        "inet_interfaces = loopback-only" \
        "inet_protocols = ipv4" \
        "mynetworks = 127.0.0.0/8" \
        "smtpd_relay_restrictions = permit_mynetworks, reject" \
        "disable_vrfy_command = yes" \
        "smtpd_banner = \$myhostname ESMTP" \
        "smtp_tls_security_level = may" \
        "smtp_tls_loglevel = 1" \
        "smtpd_milters = inet:127.0.0.1:$DKIM_PORT" \
        "non_smtpd_milters = \$smtpd_milters" \
        "milter_default_action = accept"
    postfix check >/dev/null || die "postfix check failed (see: postfix check)"
    restart postfix
    wait_listening 25 220 || die "Postfix does not answer on 127.0.0.1:25 (see: journalctl -u postfix@-)"
    ok "Postfix sends as $MAILHOST and accepts mail only from this server"
}

setup_site_mail() {
    say "The stove website"
    local current
    current=$(sed -n 's/^smtp_host = //p' "$CONFIG" | head -n1)
    if [ -n "$current" ] && [ "$current" != 127.0.0.1 ] && [ "$current" != localhost ]; then
        if ! confirm "The website sends its e-mail through $current now. Send it from this server instead?"; then
            info "left the website's e-mail settings as they are"
            return 0
        fi
    fi
    "$WRAPPER" setup-mail --host 127.0.0.1 --port 25 --security none --from "Stove <$FROM_ADDR>" >/dev/null
    chown root:"$SVC_USER" "$CONFIG"
    chmod 640 "$CONFIG"
    if systemctl is-active --quiet "$SERVICE"; then
        systemctl restart "$SERVICE"
    fi
    ok "the website sends as \"Stove <$FROM_ADDR>\" through this server"
}

local_status() {
    say "This server"
    choose_hostname
    local ours=0
    [ "$(postfix_state)" = ours ] && ours=1
    if [ "$ours" = 1 ]; then
        if listening 25 220; then ok "Postfix answers on 127.0.0.1:25 (as $(postconf -h myhostname))"
        else warn "Postfix does not answer on 127.0.0.1:25 (sudo systemctl restart postfix)"; fi
        if listening "$DKIM_PORT"; then ok "OpenDKIM listens on 127.0.0.1:$DKIM_PORT"
        else warn "OpenDKIM does not run (sudo systemctl restart opendkim)"; fi
    else
        info "the mail server is not set up yet (this script does it when run without --check)"
    fi
    local host
    host=$(sed -n 's/^smtp_host = //p' "$CONFIG" 2>/dev/null | head -n1)
    case $host in
        127.0.0.1|localhost)
            if [ "$ours" = 1 ]; then ok "the website sends through this server"
            else warn "the website sends through 127.0.0.1, but this script did not set up a mail server there"; fi ;;
        "") warn "the website has no e-mail set up (sudo stove-cloud setup-mail, or this script)" ;;
        *) ok "the website sends through $host" ;;
    esac
    if [ "$ours" = 1 ]; then
        local queued
        queued=$(postqueue -p 2>/dev/null | grep -c '^[0-9A-F]' || true)
        [ "${queued:-0}" -gt 0 ] && warn "$queued mail(s) wait in the queue (sudo postqueue -p shows why)"
    fi
    return 0
}

dns_report() {
    say "DNS"
    if ! command -v dig >/dev/null 2>&1; then
        warn "checking DNS needs dig: sudo apt install dnsutils"
        DNS_OK=0
        return 0
    fi
    [ -n "$MAILHOST" ] || choose_hostname
    local dkim=()
    [ -f "$DKIM_TXT" ] && dkim=(--dkim "$DKIM_TXT")     # no key before the first setup
    DNS_OK=0
    if dns check --domain "$DOMAIN" --ip "$IP" --hostname "$MAILHOST" --selector "$SELECTOR" ${dkim[@]+"${dkim[@]}"} &&
       [ ${#dkim[@]} -gt 0 ]; then
        DNS_OK=1
        ok "all records are in place"
    fi
}

test_mail() {
    [ "$NO_TEST" = 1 ] && return 0
    local to=$TEST_TO out mid
    if [ -z "$to" ]; then
        [ "$CHECK" = 1 ] && return 0
        to=$("$WRAPPER" admin-emails 2>/dev/null | head -n1) || true
    fi
    if [ -z "$to" ]; then
        info "no administrator e-mail yet - send a test later with: sudo stove-cloud test-mail ADDRESS"
        return 0
    fi
    say "Test e-mail to $to"
    if ! out=$("$WRAPPER" test-mail "$to" 2>&1); then
        warn "$out"
        return 0
    fi
    mid=$(printf '%s\n' "$out" | grep -o '<[^>]*>' | tail -n1)
    follow_delivery "$mid" "$to"
}

follow_delivery() {  # watch the Postfix log until the receiving server answered
    local mid=$1 to=$2 qid="" line="" _
    info "handed to Postfix (Message-ID $mid); waiting for $to's mail server..."
    for _ in $(seq 1 60); do
        if [ -z "$qid" ] && [ -n "$mid" ]; then
            qid=$(mail_log | grep -F "message-id=$mid" | grep -oE ' [0-9A-Za-z]{6,}: message-id=' | head -n1 |
                  tr -d ' :' | sed 's/message-id=$//') || true
        fi
        if [ -n "$qid" ]; then
            line=$(mail_log | grep -F " $qid: to=<" | grep -F "status=" | tail -n1) || true
            case $line in *status=sent*|*status=bounced*) break ;; esac
        fi
        sleep 1
    done
    if [ -z "$qid" ]; then
        info "could not find it in the mail log; check the inbox (and spam) of $to"
        return 0
    fi
    if mail_log | grep -F "$qid" | grep -q "DKIM-Signature field added"; then
        ok "signed with DKIM ($qid)"
    fi
    local reason=${line#*status=}
    reason=${reason#* }; reason=${reason#(}; reason=${reason%)}     # the receiver's answer
    case $line in
        *status=sent*)
            ok "delivered: $reason"
            info "In Gmail open the mail, then More (three dots) > Show original: SPF, DKIM and DMARC"
            info "should all say PASS. If it landed in spam, mark it as not spam."
            ;;
        *status=bounced*)
            warn "refused: $reason"
            mail_hint "$line"
            ;;
        *status=deferred*)
            warn "not delivered yet: $reason"
            mail_hint "$line"
            info "Postfix keeps trying for 5 days; the queue: sudo postqueue -p"
            ;;
        *)
            info "still on its way; follow it with: journalctl -f -t postfix/smtp (or /var/log/mail.log)"
            ;;
    esac
}

mail_hint() {
    case $1 in
        *5.7.25*|*PTR*|*"reverse DNS"*)
            info "-> the receiver wants the reverse DNS (PTR) of $IP to be set (see DNS above)" ;;
        *5.7.26*|*5.7.27*|*5.7.1*[Uu]nauthenticated*|*SPF*|*DKIM*|*DMARC*)
            info "-> the receiver could not verify the sender: add the SPF and DKIM records (see DNS above)" ;;
        *"blocked using"*|*[Ss]pamhaus*|*[Bb]locklist*|*[Bb]lacklist*)
            info "-> $IP is on a blocklist; check it at https://check.spamhaus.org and ask your provider" ;;
        *"Connection timed out"*|*"Network is unreachable"*|*"Connection refused"*)
            info "-> outgoing port 25 looks blocked (see the start of this output)" ;;
    esac
}

summary() {
    cat <<EOF

${GRN}${B}Done.${N} The website sends its e-mail as ${B}Stove <$FROM_ADDR>${N} from this server ($MAILHOST, $IP).
EOF
    if [ "${DNS_OK:-0}" != 1 ]; then
        cat <<EOF
Add the DNS records listed above, and the reverse DNS where it says so; receivers like Gmail
refuse or file as spam mail without them. Changes can take up to an hour. Then check again:
  sudo $0 --check --test-to YOUR-ADDRESS
EOF
    fi
    cat <<EOF

  test e-mail:  sudo stove-cloud test-mail ADDRESS
  mail log:     journalctl -t postfix/smtp -f   (or /var/log/mail.log)
  queue:        sudo postqueue -p
EOF
    [ -d "$BACKUP_ROOT" ] && echo "  backup of the changed files: $BACKUP_ROOT"
    return 0
}

# -------------------------------------------------------------------- main
preflight
find_domain
find_ip
check_port25
if [ "${PORT25_BLOCKED:-0}" = 1 ]; then
    dns_report
    die "nothing was changed. Ask your VPS provider to open outgoing port 25 (and to set the reverse DNS
      if you cannot set it yourself), then run this again. --force sets the mail server up anyway;
      mail then waits in its queue until the port is open."
fi
if [ "$CHECK" = 1 ]; then
    local_status
    dns_report
    test_mail
    [ "${DNS_OK:-0}" = 1 ] || exit 1
    exit 0
fi
safety_checks
install_packages
setup_dkim
setup_postfix
setup_site_mail
dns_report
test_mail
summary
