#!/usr/bin/env bash
# auto-update.sh - follow a GitHub branch: pull it and re-run the installer when it changed.
#
# GitHub Actions moves the branch "deploy" to the newest commit of main whose tests passed
# (.github/workflows/ci.yml). A systemd timer made by this script checks that branch every
# minute, so the VPS and the Pi run tested code without anybody logging in:
#
#   sudo tools/auto-update.sh --install stove-cloud-update  -- /bin/bash www/deploy.sh --yes --domain example.com
#   sudo tools/auto-update.sh --install stove-master-update -- /bin/bash pi/install.sh
#   sudo tools/auto-update.sh --remove  stove-cloud-update
#   sudo tools/auto-update.sh [--repo DIR] [--branch deploy] -- COMMAND...      # one check, by hand
#
# "Update now" on a web page: with --request FILE (and --status FILE) the timer also gets a
# systemd path unit that starts a run as soon as FILE exists. The web service, which runs as an
# unprivileged user, only creates FILE in its own data folder; the run removes it and writes how
# it went to the status FILE (JSON) for the page to show.
#
# (www/deploy.sh --auto-update and pi/install.sh --auto-update call the first two for you.)
#
# One run: fetch origin/<branch> as the user who owns the checkout (that user's credentials,
# never a prompt); nothing new -> exit quietly. Otherwise refuse if tracked files were edited
# locally, move the checkout to the new commit and run COMMAND as root. If COMMAND fails, go
# back to the previous commit, run COMMAND again and exit with an error; that commit is not
# tried again, the next one is. Only one run at a time (flock); output goes to the journal.
#
# Runs that did not get where they wanted are taken up again, with nothing new on the branch:
#   - a run stopped part-way (shutdown, systemctl stop) installs its commit again at the next check;
#   - COMMAND can ask to be run again later by leaving .git/stove-auto-update.retry in the checkout
#     (first line: why), e.g. pi/install.sh when a run of it was cut off. It is run again
#     10 minutes after that, then 20, 40, ... at most every 6 hours; "Update now" runs it at once.
#     COMMAND removes the file once it got there.
set -Eeuo pipefail

REQUEST="" STATUS="" STATUS_ON=0 MANUAL=0
PENDING="" RETRY="" TRIES=""      # in the checkout's .git (update)
INSTALLING=0                      # 1 while COMMAND runs: a stop then leaves it half done

die() { echo "auto-update: $*" >&2; status failed "$*"; exit 1; }

# status RESULT MESSAGE [COMMIT]: how the last run went, for the web page (only during a run)
status() {
    [ "$STATUS_ON" = 1 ] && [ -n "$STATUS" ] || return 0
    python3 - "$STATUS" "$1" "$2" "${3:-}" "$MANUAL" <<'PY' || true
import json, os, sys, tempfile, time
path, result, message, commit, manual = sys.argv[1:]
data = {"result": result, "message": message, "commit": commit, "at": int(time.time()), "manual": manual == "1"}
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".update-status.")
# the mode on the open file, not by name: the folder belongs to the web service's user, who
# could put a symlink in place of tmp between close() and a chmod(tmp) run as root
os.fchmod(fd, 0o644)
with os.fdopen(fd, "w") as f:
    json.dump(data, f)
os.replace(tmp, path)
PY
}

main() {
    local repo="" branch="" mode=run name=""
    while [ $# -gt 0 ]; do
        case $1 in
            --repo) repo=${2:?--repo needs a value}; shift 2 ;;
            --branch) branch=${2:?--branch needs a value}; shift 2 ;;
            --request) REQUEST=${2:?--request needs a file}; shift 2 ;;
            --status) STATUS=${2:?--status needs a file}; shift 2 ;;
            --install) mode=install; name=${2:?--install needs the unit name}; shift 2 ;;
            --remove) mode=remove; name=${2:?--remove needs the unit name}; shift 2 ;;
            --) shift; break ;;
            -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
            *) die "unknown option '$1' (see --help)" ;;
        esac
    done
    if [ "$mode" = remove ]; then remove_timer "$name"; return 0; fi
    [ $# -gt 0 ] || die "no command after -- (see --help)"
    [ -n "$repo" ] || repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
    repo=$(cd "$repo" && pwd)
    [ -d "$repo/.git" ] || die "$repo is not a git checkout"
    OWNER=$(stat -c %U "$repo")
    if [ "$mode" = install ]; then install_timer "$name" "$repo" "${branch:-}" "$@"; else update "$repo" "${branch:-deploy}" "$@"; fi
}

# git as the owner of the checkout: its credentials, no "dubious ownership", no prompt ever
git_() {
    if [ "$(id -u)" = 0 ] && [ "$OWNER" != root ]; then
        runuser -u "$OWNER" -- env GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="ssh -o BatchMode=yes" git "$@"
    else
        GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="ssh -o BatchMode=yes" git "$@"
    fi
}

update() {
    local repo=$1 branch=$2; shift 2
    # asked for on the web page: take the request away first, or systemd starts the next run at once
    if [ -n "$REQUEST" ] && [ -e "$REQUEST" ]; then rm -f "$REQUEST"; MANUAL=1; echo "auto-update: asked for on the web page"; fi
    local lock="/tmp/stove-auto-update-$(printf '%s' "$repo" | tr -c 'A-Za-z0-9' _).lock"
    exec 9>"$lock"
    if ! flock -n 9; then echo "auto-update: another run is still going on"; return 0; fi
    cd "$repo"
    STATUS_ON=1
    # "running" while no run holds the lock: one was stopped (a script without a trap wrote it)
    if [ -n "$STATUS" ] && grep -qs '"result": *"running"' "$STATUS"; then
        status failed "The last update stopped before it finished - it is taken up again by itself"
    fi
    [ "$MANUAL" = 1 ] && status running "Checking GitHub for a new version"
    local failed_file="$repo/.git/stove-auto-update.failed" old new p_old p_new
    PENDING="$repo/.git/stove-auto-update.pending" RETRY="$repo/.git/stove-auto-update.retry"
    TRIES="$repo/.git/stove-auto-update.tries"
    # stopped (shutdown, systemctl stop): only a COMMAND cut off is worth a word on the page; it is
    # taken up again at the next check (PENDING, or the retry request it leaves)
    trap 'if [ "$INSTALLING" = 1 ]; then status failed "The update was stopped before it finished - it goes on at the next check";
          elif [ "$MANUAL" = 1 ]; then status failed "The check was stopped before it finished - nothing was changed"; fi
          exit 143' TERM INT HUP
    old=$(git_ -C "$repo" rev-parse HEAD)
    git_ -C "$repo" fetch -q origin "+refs/heads/$branch:refs/remotes/origin/$branch" ||
        die "cannot fetch origin/$branch (network? credentials of user $OWNER?)"
    new=$(git_ -C "$repo" rev-parse "refs/remotes/origin/$branch")
    if [ "$new" = "$old" ]; then
        # a run that moved the checkout to this commit was stopped before COMMAND finished
        if [ -f "$PENDING" ] && read -r p_old p_new <"$PENDING" && [ "$p_new" = "$new" ] && [ -n "$p_old" ]; then
            echo "auto-update: the update to ${new:0:10} stopped before it finished - installing it again"
            install_commit "$repo" "$branch" "$p_old" "$new" "$failed_file" "$@"
            return 0
        fi
        rm -f "$PENDING"
        if retry_due; then
            retry "$new" "$@"
            return 0
        fi
        # nothing new: say nothing
        [ "$MANUAL" = 1 ] && status current "Already up to date: $(git_ -C "$repo" log -1 --format=%s "$new")" "${new:0:10}"
        return 0
    fi
    if [ -f "$failed_file" ] && [ "$(cat "$failed_file")" = "$new" ]; then
        # the run that went back to this commit may itself have been cut off, or asked for another run
        if retry_due; then
            retry "$old" "$@"
            return 0
        fi
        die "origin/$branch is still ${new:0:10}, which failed to install - waiting for a new commit"
    fi
    if [ -n "$(git_ -C "$repo" status --porcelain --untracked-files=no)" ]; then
        if git_ -C "$repo" diff --quiet "$new" -- && git_ -C "$repo" diff --cached --quiet "$new" --; then
            # files and index already ARE that commit: a run was stopped while git moved there
            echo "auto-update: the checkout already holds ${new:0:10} - a run was stopped while it moved there"
        else
            git_ -C "$repo" status --short --untracked-files=no >&2
            die "$repo has local changes - not updating until they are committed, stashed or reverted"
        fi
    fi
    echo "auto-update: ${old:0:10} -> ${new:0:10}: $(git_ -C "$repo" log -1 --format=%s "$new")"
    printf '%s %s\n' "$old" "$new" >"$PENDING"
    git_ -C "$repo" checkout -q -B "$branch" --track "origin/$branch"
    install_commit "$repo" "$branch" "$old" "$new" "$failed_file" "$@"
}

# install_commit REPO BRANCH OLD NEW FAILED_FILE COMMAND...: the checkout is on NEW; run COMMAND,
# and when it fails go back to OLD and run it there
install_commit() {
    local repo=$1 branch=$2 old=$3 new=$4 failed_file=$5; shift 5
    rm -f "$TRIES"
    status running "Installing ${new:0:10}: $(git_ -C "$repo" log -1 --format=%s "$new")" "${new:0:10}"
    INSTALLING=1
    if "$@"; then
        INSTALLING=0
        rm -f "$failed_file" "$PENDING"
        echo "auto-update: now on ${new:0:10}"
        status updated "Updated to ${new:0:10}: $(git_ -C "$repo" log -1 --format=%s "$new")$(retry_note)" "${new:0:10}"
        return 0
    fi
    echo "auto-update: the command failed on ${new:0:10} - going back to ${old:0:10}" >&2
    echo "$new" >"$failed_file"
    git_ -C "$repo" checkout -q -B "$branch" "$old"
    rm -f "$PENDING"        # (only now: stopped before the checkout, the next run tries NEW again)
    if "$@"; then
        die "back on ${old:0:10}; ${new:0:10} is not tried again - push a fix to $branch"
    fi
    die "the command failed on ${old:0:10} as well - this needs a look"
}

# retry_note: " - not complete yet: WHY (tried again later)" while COMMAND asks to be run again
retry_note() {
    [ -f "$RETRY" ] || return 0
    printf ' - not complete yet: %s (tried again later)' "$(head -n 1 "$RETRY" 2>/dev/null | cut -c1-200)"
}

# retry_due: COMMAND asked to be run again (.git/stove-auto-update.retry) and its time has come:
# 10 minutes after its last run, then 20, 40, ... at most 6 hours; at once for "Update now"
retry_due() {
    local tries wait age
    [ -f "$RETRY" ] || { rm -f "$TRIES"; return 1; }
    [ "$MANUAL" = 1 ] && return 0
    tries=$(cat "$TRIES" 2>/dev/null) || tries=0
    [[ $tries =~ ^[0-9]+$ ]] || tries=0
    [ "$tries" -le 6 ] || tries=6
    wait=$((600 << tries))
    [ "$wait" -le 21600 ] || wait=21600
    age=$(($(date +%s) - $(stat -c %Y "$RETRY" 2>/dev/null || echo 0)))
    [ "$age" -ge "$wait" ]
}

# retry CURRENT COMMAND...: run COMMAND again on the current commit (no going back when it fails:
# this commit installed before, and the next try comes later)
retry() {
    local cur=$1 why tries; shift
    why=$(head -n 1 "$RETRY" 2>/dev/null | cut -c1-200)
    tries=$(cat "$TRIES" 2>/dev/null) || tries=0
    [[ $tries =~ ^[0-9]+$ ]] || tries=0
    echo $((tries + 1)) >"$TRIES"
    echo "auto-update: running the command on ${cur:0:10} again (${why:-the last run asked for it})"
    status running "Installing ${cur:0:10} again: ${why:-the last run asked for it}" "${cur:0:10}"
    INSTALLING=1
    if "$@"; then
        INSTALLING=0
        if [ -f "$RETRY" ]; then
            echo "auto-update: still not complete: $(head -n 1 "$RETRY" 2>/dev/null | cut -c1-200)"
            status updated "Installed ${cur:0:10} again$(retry_note)" "${cur:0:10}"
        else
            rm -f "$TRIES"
            echo "auto-update: ${cur:0:10} is complete now"
            status updated "Installed ${cur:0:10} again: complete now" "${cur:0:10}"
        fi
        return 0
    fi
    die "the command failed again on ${cur:0:10} - it is tried again later"
}

install_timer() {
    local name=$1 repo=$2 branch=$3; shift 3
    local unit=/etc/systemd/system/$name.service timer=/etc/systemd/system/$name.timer
    local path=/etc/systemd/system/$name.path extra=""
    [ -z "$REQUEST" ] || extra+=" --request $REQUEST"
    [ -z "$STATUS" ] || extra+=" --status $STATUS"
    [[ "$REQUEST$STATUS" =~ ^[A-Za-z0-9_./-]*$ ]] || die "--request/--status: plain paths only"
    [ "$(id -u)" = 0 ] || die "--install needs root"
    command -v systemctl >/dev/null 2>&1 || die "systemd is required"
    if [ -z "$branch" ] && [ -f "$unit" ]; then branch=$(sed -n 's/.*--branch \([^ ]*\).*/\1/p' "$unit" | head -n1); fi
    branch=${branch:-deploy}
    [[ $name =~ ^[A-Za-z0-9_.-]+$ ]] || die "unit name '$name': letters, digits, . _ -"
    git_ -C "$repo" ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1 ||
        die "cannot read origin/$branch as user $OWNER without a prompt. Either the branch does not exist yet" \
            "(GitHub Actions creates it once the tests on main pass: after a push to main, or with Actions ->" \
            "CI -> Run workflow in a new fork), or git needs credentials that work" \
            "unattended: a stored token (git config credential.helper store, then git pull once) or an SSH" \
            "deploy key without a passphrase."
    cat >"$unit.new" <<UNIT
[Unit]
Description=Pull branch $branch of the stove repository and re-run its installer when it changed
Documentation=https://github.com/AlesZupanc/palazzetti-stove-hub
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/bash $repo/tools/auto-update.sh --repo $repo --branch $branch$extra -- $*
UNIT
    cat >"$timer.new" <<TIMER
[Unit]
Description=Check GitHub for stove updates every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=5s
RandomizedDelaySec=10s

[Install]
WantedBy=timers.target
TIMER
    mv -f "$unit.new" "$unit"
    mv -f "$timer.new" "$timer"
    if [ -n "$REQUEST" ]; then
        cat >"$path" <<PATHUNIT
[Unit]
Description=Start $name at once when the web page asks for an update

[Path]
PathExists=$REQUEST
Unit=$name.service

[Install]
WantedBy=paths.target
PATHUNIT
    elif [ -f "$path" ]; then
        systemctl disable --now "$name.path" >/dev/null 2>&1 || true
        rm -f "$path"
    fi
    systemctl daemon-reload
    systemctl enable "$name.timer" >/dev/null 2>&1
    systemctl restart "$name.timer"                      # a changed interval counts from now on
    if [ -n "$REQUEST" ]; then
        systemctl enable "$name.path" >/dev/null 2>&1
        systemctl restart "$name.path"
    fi
    echo "auto-update: $name.timer follows origin/$branch every minute (git as $OWNER, the command as root)"
    echo "             log: journalctl -u $name    run now: sudo systemctl start $name    off: --remove $name"
}

remove_timer() {
    local name=$1 unit=/etc/systemd/system/$1.service timer=/etc/systemd/system/$1.timer
    [ "$(id -u)" = 0 ] || die "--remove needs root"
    [ -f "$unit" ] || [ -f "$timer" ] || { echo "auto-update: $name is not installed"; return 0; }
    systemctl disable --now "$name.timer" "$name.path" >/dev/null 2>&1 || true
    rm -f "$unit" "$timer" "/etc/systemd/system/$name.path"
    systemctl daemon-reload
    echo "auto-update: $name removed - updates are manual again"
}

main "$@"
