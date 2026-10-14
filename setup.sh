#!/bin/bash
# HELP-START
# setup.sh -- configure and schedule the FireMon retrieval retry/alert tool.
#
# Usage:
#   bash setup.sh [VERB] [OPTIONS]
#
# Verbs:
#   menu               a pointer-driven menu of everything below. This is what
#                      runs when no verb is given -- the front door. Falls back
#                      to a numbered prompt on a terminal that cannot do ANSI.
#   install            configure, verify, then offer to schedule -- all in one pass.
#   configure          interactive wizard: ask for connection/mail settings,
#                       write config.ini and secrets.env, prove they work.
#   edit-config        open config.ini in $EDITOR for settings the wizard
#                       doesn't prompt for, then re-check it on save.
#   verify             re-run the read-only checks (auth, a --dry-run scan,
#                       the unit tests) against the live config. Changes nothing.
#   list-pending       show devices awaiting a retry confirmation and what's
#                       in the digest ledger. Read-only.
#   activity           tail cron.log/retry_alert.log and the mail queue.
#                       Read-only.
#   run-now            run one real cycle right now, off-schedule (asks first) --
#                       real retries; a digest is only sent if one is due.
#   send-digest        force-send whatever is in the ledger right now, then
#                       reset the digest timer, regardless of whether it's due.
#   schedule           install or replace the cron entry. Defaults to
#                       --dry-run (observe, trigger/alert nothing) until
#                       --live is passed.
#   unschedule         remove the cron entry. Leaves every file alone.
#   status             report what is configured and scheduled. Changes nothing.
#   restore-crontab    put back a crontab from backups/.
#   update             download the latest code from GitHub and install it over
#                       this copy, then run the unit tests. config.ini,
#                       secrets.env, state/, backups/ and the logs are never
#                       touched, and the cron entry is left as it is. The
#                       replaced code is kept under backups/code-<stamp>/
#                       (only the most recent one; older ones are pruned).
#                       --check only reports whether a newer version exists.
#                       --from FILE installs from a downloaded .tar.gz or .zip
#                       instead, for a box with no route to GitHub.
#   uninstall          remove the cron entry, then optionally the config files.
#   help               this text.
#
# Options:
#   --check            with update: compare versions and stop. Changes nothing.
#   --from FILE        with update: install from this archive instead of
#                       downloading. A GitHub "Download ZIP" or a release
#                       .tar.gz both work.
#   --dir DIR          install directory (default: the directory holding this
#                       script). Relative paths in config.ini resolve against it.
#   --at "M H D M W"   cron schedule (default: "*/10 * * * *", every 10 minutes).
#   --live             schedule real retries and real digest email. Without it
#                       the installed cron line carries --dry-run, which only
#                       logs what it would do.
#   --no-logrotate     do NOT install the monthly log-trimming cron lines.
#   --yes              accept the write confirmation without asking. Never
#                       skips a refusal.
#   --adopt            adopt an unmanaged retry-alert cron line found in the
#                       crontab instead of stopping to ask about it.
#
# Environment (each answers the matching prompt, for an unattended run):
#   FMRETRY_PASSWORD           FireMon service account password (also read by
#                               the tool itself; never written to config.ini)
#   FMRETRY_SMTP_PASSWORD      SMTP password, if the relay needs one
#   FMRETRY_SETUP_BASE_URL / _DOMAIN_ID / _DEVICE_FILTER / _VERIFY_TLS
#   FMRETRY_SETUP_EXCLUDE_NAME_CONTAINS
#   FMRETRY_SETUP_USERNAME / _SMTP_HOST / _SMTP_PORT / _SMTP_STARTTLS
#   FMRETRY_SETUP_SMTP_USERNAME / _FROM_ADDRESS / _RECIPIENTS
#   FMRETRY_SETUP_DIGEST_INTERVAL_HOURS
#
# What it writes, all inside the install directory:
#   config.ini      a copy of config.example.ini with your values patched in,
#                    comments and all. Never generated from scratch.
#   secrets.env      mode 0600, holds the passwords. Sourced by the cron line.
#   backups/         every crontab it replaces, and every config it overwrites.
#   setup.log        what this script did, and when.
#
# It does not need root, does not write outside the install directory apart
# from your own crontab.
#
# Notes:
#   * It never claims mail was delivered. The relay accepting a message is the
#     last thing any SMTP client can observe -- check mailq yourself.
#   * On FMOS /usr/bin/python3 may be too old for this tool (needs 3.7+ for
#     dataclasses); run_retry_alert.py re-executes itself under a newer
#     interpreter automatically, so the installed cron line names plain
#     /usr/bin/python3 and keeps working across upgrades.
# HELP-END

# Not `set -e`: the healthy path here includes commands EXPECTED to fail --
# grep finding no marker, crontab -l on a user who has none. Each is checked
# explicitly at its call site.
set -uo pipefail

umask 077

SCRIPT_PATH="$(readlink -f "$0" 2>/dev/null || echo "$0")"
INSTALL_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"

VERB=""
CRON_SPEC="*/10 * * * *"
OPT_LIVE=0
OPT_LOGROTATE=1
OPT_ADOPT=0
OPT_YES=0
RESTORE_FILE=""

UPDATE_FROM=""
OPT_CHECK=0

# Where `update` fetches from. The archive URL needs no authentication, so
# this works from any box that can reach github.com (curl honours
# https_proxy if one is needed). Override the repo for a fork.
GITHUB_REPO="${FMRETRY_GITHUB_REPO:-adamgunderson/Retrieval-Retry-Alert}"
GITHUB_BRANCH="${FMRETRY_GITHUB_BRANCH:-main}"

# The code this tool ships, i.e. what `update` replaces. Everything else in
# the install directory -- config.ini, secrets.env, state/, backups/, the
# logs -- is operator data and is never touched by an update.
CODE_FILES="VERSION DEPLOYMENT.md README.md config.example.ini run_retry_alert.py run_tests.py setup.sh"
CODE_DIRS="retry_alert templates tests"

MARK_OPEN="# >>> firemon-retry-alert managed block -- setup.sh >>>"
MARK_CLOSE="# <<< firemon-retry-alert managed block -- setup.sh <<<"
TRIM_OPEN="# >>> firemon-retry-alert log trimming -- setup.sh >>>"
TRIM_CLOSE="# <<< firemon-retry-alert log trimming -- setup.sh <<<"

EXIT_OK=0
EXIT_FAIL=1
EXIT_CRON=2

# ---------------------------------------------------------------- output ----

usage() {
    sed -n '/^# HELP-START/,/^# HELP-END/p' "$SCRIPT_PATH" \
        | sed -e '1d' -e '$d' -e 's/^#\{1,\} \{0,1\}//'
}

say() {
    local line="$1"
    printf '%s\n' "$line"
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$line" \
        >> "$INSTALL_DIR/setup.log" 2>/dev/null
}

ok()      { say "OK:      $1"; }
info()    { say "         $1"; }
action()  { say "ACTION:  $1"; }
warn()    { say "WARNING: $1"; }
skip()    { say "SKIP:    $1"; }
head2()   { say ""; say "=== $1 ==="; }

say_err() {
    printf '%s\n' "$1" >&2
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" \
        >> "$INSTALL_DIR/setup.log" 2>/dev/null
}

die() {
    say "ERROR:   $1"
    exit "${2:-$EXIT_FAIL}"
}

trap 'exit $EXIT_CRON' TERM
trap 'exit $EXIT_FAIL' USR1
fatal() {
    local sig="${2:-TERM}" code=$EXIT_CRON
    [ "$sig" = "USR1" ] && code=$EXIT_FAIL
    say_err "ERROR:   $1"
    if [ "${FMRETRY_SETUP_LIB-}" != "1" ]; then
        kill -"$sig" "$$" 2>/dev/null
    fi
    exit "$code"
}

# ---------------------------------------------------------------- prompts ---

require_tty() {
    local question="$1" envvar="${2:-}"
    if { true < /dev/tty; } 2>/dev/null; then
        return 0
    fi
    local hint="Run this from an interactive shell."
    [ -n "$envvar" ] && hint="Set $envvar to answer it without a prompt."
    fatal "need to ask '$question' but there is no terminal to ask on. $hint 'bash setup.sh help' lists every variable the wizard reads." USR1
}

ask() {
    local prompt="$1" default="$2" envvar="${3:-}" answer=""
    if [ -n "$envvar" ]; then
        local override="${!envvar-}"
        if [ -n "$override" ]; then
            printf '%s [%s]\n' "$prompt" "$override" >&2
            printf '%s' "$override"
            return 0
        fi
    fi
    require_tty "$prompt" "$envvar"
    while true; do
        if [ -n "$default" ]; then
            read -r -p "$prompt [$default]: " answer < /dev/tty
            answer="${answer:-$default}"
        else
            read -r -p "$prompt: " answer < /dev/tty
        fi
        [ -n "$answer" ] && break
        printf 'A value is required.\n' >&2
    done
    printf '%s' "$answer"
}

# $3, if given, is an existing secret (e.g. read back from secrets.env) that a
# blank answer keeps instead of re-asking -- so re-running `configure` on an
# already-configured install doesn't force retyping a password that isn't
# changing. Never echoed or logged; only ever printed back as this function's
# own stdout return value.
ask_secret() {
    local prompt="$1" envvar="$2" current="${3:-}" first="" second="" hint
    local override="${!envvar-}"
    if [ -n "$override" ]; then
        printf '%s [taken from %s]\n' "$prompt" "$envvar" >&2
        printf '%s' "$override"
        return 0
    fi
    require_tty "$prompt" "$envvar"
    hint="$prompt"
    [ -n "$current" ] && hint="$prompt (blank = keep the current one)"
    while true; do
        read -r -s -p "$hint: " first < /dev/tty; printf '\n' >&2
        if [ -z "$first" ] && [ -n "$current" ]; then
            printf '%s' "$current"
            return 0
        fi
        read -r -s -p "Confirm: " second < /dev/tty; printf '\n' >&2
        [ "$first" = "$second" ] && [ -n "$first" ] && break
        printf 'Did not match, or was empty. Again.\n' >&2
    done
    printf '%s' "$first"
}

# The value an existing config.ini has for section/key, or $3 if there is no
# config.ini yet, the key isn't set, or the file can't be read. Lets a
# re-run of `configure` pre-fill every prompt from what's already deployed,
# instead of always starting from the same hardcoded defaults.
_existing_or() {
    local section="$1" key="$2" fallback="$3" value
    if [ -f "$INSTALL_DIR/config.ini" ]; then
        value="$(config_value "$INSTALL_DIR/config.ini" "$section" "$key" 2>/dev/null)"
        if [ "$value" != "(no config)" ] && [ -n "$value" ]; then
            printf '%s' "$value"
            return 0
        fi
    fi
    printf '%s' "$fallback"
}

# true/false (config.ini's spelling) -> yes/no (the prompt's spelling), for
# pre-filling a yes/no prompt's default from an existing boolean setting.
_bool_to_yesno() {
    case "$1" in true) printf 'yes' ;; *) printf 'no' ;; esac
}

# The current value of an env-style secret name (FMRETRY_PASSWORD etc.) out of
# an existing secrets.env, or empty if there is none. Sourced in a subshell so
# it never leaks into this script's own environment.
_current_secret() {
    local varname="$1"
    [ -f "$INSTALL_DIR/secrets.env" ] || { printf ''; return 0; }
    ( . "$INSTALL_DIR/secrets.env" >/dev/null 2>&1; printf '%s' "${!varname-}" )
}

confirm() {
    local prompt="$1" answer=""
    if [ "$OPT_YES" -eq 1 ]; then
        info "$prompt -- yes (--yes)"
        return 0
    fi
    require_tty "$prompt" "(or pass --yes)"
    read -r -p "$prompt [y/N]: " answer < /dev/tty
    # Tolerate what terminals actually send: a stray CR from a client in
    # CRLF mode, surrounding whitespace, any capitalisation.
    local norm
    norm="$(printf '%s' "$answer" | tr -d '\r' | tr '[:upper:]' '[:lower:]' \
        | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    case "$norm" in
        y|yes) return 0 ;;
        *)
            # Say what was read, so "I typed y and it said no" is diagnosable
            # rather than a mystery: an empty answer prints as ''.
            info "read '$answer' -- not a yes, so: no"
            return 1 ;;
    esac
}

# ------------------------------------------------------------- validation ---

# configparser reads ' ;' and ' #' as an inline comment and would silently
# truncate the value, and a bare % is an interpolation token whose error
# escapes the tool's own error handling as a raw traceback.
reject_unsafe() {
    local what="$1" value="$2"
    case "$value" in
        *%*)    die "$what may not contain '%' -- config.ini treats it as an interpolation token." ;;
        *" ;"*) die "$what may not contain ' ;' -- config.ini would treat the rest as a comment." ;;
        *" #"*) die "$what may not contain ' #' -- config.ini would treat the rest as a comment." ;;
    esac
    case "$value" in
        *$'\n'*|*$'\r'*) die "$what may not contain a line break." ;;
    esac
}

need_int() {
    local what="$1" value="$2"
    case "$value" in
        ''|*[!0-9]*) die "$what must be a whole number (got '$value')." ;;
    esac
}

# Like need_int, but allows one decimal point -- digest interval_hours etc.
# are floats in config.py (4.5 hours is a reasonable thing to want).
need_number() {
    local what="$1" value="$2"
    case "$value" in
        ''|*[!0-9.]*) die "$what must be a number (got '$value')." ;;
    esac
    case "$value" in
        *.*.*|.) die "$what must be a number (got '$value')." ;;
    esac
}

yesno_to_bool() {
    case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
        y|yes|true|1|on)  printf 'true' ;;
        n|no|false|0|off) printf 'false' ;;
        *) die "expected yes or no, got '$1'." ;;
    esac
}

# CRON_SPEC is spliced verbatim into the live crontab line (retry_alert_cron_line)
# with no further quoting -- unlike every other free-form input, which goes
# through reject_unsafe/_parse_email_list. A line break here would inject an
# arbitrary extra crontab line; validate exactly like the rest of the script does.
validate_cron_spec() {
    local spec="$1"
    case "$spec" in
        *$'\n'*|*$'\r'*) die "--at may not contain a line break." ;;
        *%*) die "--at may not contain '%' -- cron treats it as a newline in a command." ;;
    esac
    local field_count
    field_count="$(printf '%s\n' "$spec" | awk '{print NF}')"
    [ "$field_count" = "5" ] || die "--at must have exactly 5 fields (minute hour day month weekday), got: '$spec'"
    case "$spec" in
        *[!0-9A-Za-z*/,\ -]*) die "--at contains a character that isn't valid in a cron schedule: '$spec'" ;;
    esac
}

_parse_email_list() {
    local raw="$1" label="$2" out='' item
    local IFS=','
    for item in $raw; do
        item="$(printf '%s' "$item" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        [ -z "$item" ] && continue
        case "$item" in
            *";"*|*"#"*|*"%"*)
                fatal "$label '$item' contains a character config.ini cannot store (one of ; # %)." USR1 ;;
        esac
        case "$item" in
            *$'\n'*|*$'\r'*) fatal "$label '$item' may not contain a line break." USR1 ;;
        esac
        case "$item" in
            *@*.*) ;;
            *) fatal "$label '$item' is not an email address." USR1 ;;
        esac
        out="${out:+$out,}$item"
    done
    printf '%s' "$out"
}

# ---------------------------------------------------------- config patching -

# Rewrite named keys inside a copy of config.example.ini, in place, keeping
# every comment. Every key must match EXACTLY ONCE, or this stops and says
# which key -- rather than silently writing a config missing a value.
patch_config() {
    local target="$1" table="$2" stderr_file
    stderr_file="$(mktemp)"

    awk -v TABLE="$table" '
        BEGIN {
            while ((getline entry < TABLE) > 0) {
                bar1 = index(entry, "|")
                rest = substr(entry, bar1 + 1)
                bar2 = index(rest, "|")
                if (bar1 == 0 || bar2 == 0) continue
                sec = substr(entry, 1, bar1 - 1)
                key = substr(rest, 1, bar2 - 1)
                want[sec "|" key] = substr(rest, bar2 + 1)
                hits[sec "|" key] = 0
            }
            close(TABLE)
            section = ""
        }
        /^[[:space:]]*\[/ {
            s = $0
            sub(/^[[:space:]]*\[/, "", s)
            sub(/\].*$/, "", s)
            section = s
            print
            next
        }
        {
            if (match($0, /^[[:space:]]*[A-Za-z0-9_]+[[:space:]]*=/)) {
                head = substr($0, RSTART, RLENGTH)
                key = head
                sub(/[[:space:]]*=$/, "", key)
                sub(/^[[:space:]]+/, "", key)
                id = section "|" key
                if (id in want) {
                    hits[id]++
                    print head " " want[id]
                    next
                }
            }
            print
        }
        END {
            failed = 0
            for (id in want) {
                if (hits[id] != 1) {
                    printf("%s matched %d times\n", id, hits[id]) > "/dev/stderr"
                    failed = 1
                }
            }
            if (failed) exit 3
        }
    ' "$target" > "$target.patched" 2>"$stderr_file"
    local rc=$?

    if [ "$rc" -ne 0 ]; then
        say "ERROR:   could not patch $(basename "$target") -- config.example.ini"
        say "         does not have the keys this script expects:"
        while IFS= read -r line; do say "           $line"; done < "$stderr_file"
        say "         Nothing was written. Edit config.ini by hand, or update setup.sh."
        rm -f "$stderr_file" "$target.patched"
        exit "$EXIT_FAIL"
    fi
    rm -f "$stderr_file"
    mv "$target.patched" "$target"
}

table_add() {
    printf '%s|%s|%s\n' "$1" "$2" "$3" >> "$TABLE_FILE"
}

config_value() {
    local file="$1" section="$2" key="$3"
    [ -f "$file" ] || { printf '(no config)'; return; }
    awk -v sec="$section" -v key="$key" '
        /^[[:space:]]*\[/ { s = $0; sub(/^[[:space:]]*\[/, "", s); sub(/\].*$/, "", s); cur = s; next }
        {
            if (cur != sec) next
            if (match($0, /^[[:space:]]*[A-Za-z0-9_]+[[:space:]]*=/)) {
                k = substr($0, RSTART, RLENGTH); sub(/[[:space:]]*=$/, "", k); sub(/^[[:space:]]+/, "", k)
                if (k == key) { v = substr($0, RSTART + RLENGTH); sub(/^[[:space:]]+/, "", v); print v; exit }
            }
        }
    ' "$file"
}

# --------------------------------------------------------------- crontab ----

have() { command -v "$1" >/dev/null 2>&1; }

mail_queue_count() {
    have mailq || { printf '?'; return; }
    local out
    out="$(mailq 2>/dev/null)"
    if printf '%s' "$out" | grep -qi 'mail queue is empty'; then
        printf '0'; return
    fi
    local n
    n="$(printf '%s\n' "$out" | sed -n 's/.*in \([0-9][0-9]*\) Request.*/\1/p' | tail -n 1)"
    if [ -n "$n" ]; then printf '%s' "$n"; return; fi
    printf '%s\n' "$out" | grep -c '^[A-Za-z0-9]\{5,\}[*!]\?[[:space:]]'
}

crontab_read() {
    local out err rc
    err="$(mktemp)"
    out="$(crontab -l 2>"$err")"
    rc=$?
    if [ "$rc" -eq 0 ]; then
        rm -f "$err"
        printf '%s\n' "$out"
        return 0
    fi
    if grep -qi 'no crontab for' "$err"; then
        rm -f "$err"
        return 0
    fi
    local detail
    detail="$(cat "$err")"
    rm -f "$err"
    fatal "could not read the crontab, and the reason is not 'no crontab for $(id -un)'. Refusing to write one -- doing so would replace whatever is actually there. crontab -l said: $detail"
}

LAST_BACKUP=""
crontab_backup() {
    local content="$1" stamp path expected actual
    stamp="$(date '+%Y%m%dT%H%M%S')"
    path="$INSTALL_DIR/backups/crontab-$stamp.bak"
    mkdir -p "$INSTALL_DIR/backups" || die "could not create $INSTALL_DIR/backups"
    printf '%s\n' "$content" > "$path" || die "could not write $path"

    expected="$(printf '%s\n' "$content" | wc -c)"
    actual="$(wc -c < "$path")"
    if [ "$expected" -ne "$actual" ]; then
        die "backup $path is $actual bytes but should be $expected -- refusing to touch the crontab."
    fi
    ok "crontab backed up: $path"
    info "restore it with:  crontab $path"
    ls -1t "$INSTALL_DIR/backups"/crontab-*.bak 2>/dev/null | tail -n +11 \
        | while IFS= read -r old; do rm -f "$old"; done
    LAST_BACKUP="$path"
}

strip_block() {
    local content="$1" open="$2" close="$3" opens closes
    opens="$(printf '%s\n' "$content" | grep -cxF "$open")"
    closes="$(printf '%s\n' "$content" | grep -cxF "$close")"
    if [ "$opens" != "$closes" ] || [ "$opens" -gt 1 ]; then
        fatal "the crontab has $opens opening and $closes closing firemon-retry-alert markers. That is not something this script can safely rewrite. Edit it by hand with: crontab -e"
    fi
    printf '%s\n' "$content" | awk -v mark_open="$open" -v mark_close="$close" '
        $0 == mark_open  { inside = 1; next }
        $0 == mark_close { inside = 0; next }
        !inside { print }
    '
}

trailing_mailto() {
    printf '%s\n' "$1" | grep -E '^[[:space:]]*MAILTO[[:space:]]*=' | tail -n 1
}

retry_alert_cron_line() {
    local flags="$1" dir="$INSTALL_DIR"
    if [ "$dir" = "$HOME/firemon-retry-alert" ]; then
        dir='$HOME/firemon-retry-alert'
    fi
    printf '%s . "%s/secrets.env"; /usr/bin/python3 "%s/run_retry_alert.py" --config "%s/config.ini"%s >> "%s/cron.log" 2>&1' \
        "$CRON_SPEC" "$dir" "$dir" "$dir" "$flags" "$dir"
}

build_managed_block() {
    local flags="$1" prior_mailto="$2" block

    block="$MARK_OPEN
# FireMon retrieval retry/alert. Rewritten by setup.sh; do not edit here.
# MAILTO is emptied so cron does not mail this output every run -- errors
# still land in cron.log, and the digest itself is emailed by the tool, not
# by cron. There is only one cron line: this same run checks whether a
# digest is due and sends it, no separate schedule needed.
MAILTO=\"\"
$(retry_alert_cron_line "$flags")"

    if [ "$OPT_LOGROTATE" -eq 1 ]; then
        block="$block
$TRIM_OPEN
# Keep cron.log and retry_alert.log from growing without bound. State files
# (state/pending.json, state/ledger.jsonl, state/digest_state.json) are left
# alone -- they are live data, not a log.
0 3 1 * * tail -n 2000 \"$INSTALL_DIR/cron.log\" > \"$INSTALL_DIR/cron.log.tmp\" && mv \"$INSTALL_DIR/cron.log.tmp\" \"$INSTALL_DIR/cron.log\"
0 3 1 * * [ -f \"$INSTALL_DIR/retry_alert.log\" ] && { tail -n 2000 \"$INSTALL_DIR/retry_alert.log\" > \"$INSTALL_DIR/retry_alert.log.tmp\" && mv \"$INSTALL_DIR/retry_alert.log.tmp\" \"$INSTALL_DIR/retry_alert.log\"; } || true
$TRIM_CLOSE"
    fi

    if [ -n "$prior_mailto" ]; then
        block="$block
# Restoring the MAILTO that was in force before this block, so anything added
# below it does not silently inherit MAILTO=\"\".
$prior_mailto"
    else
        block="$block
# NOTE: MAILTO=\"\" above applies to every line below it too. If you add a job
# beneath this block and want cron to mail its output, set MAILTO first."
    fi

    printf '%s\n%s' "$block" "$MARK_CLOSE"
}

rehearse_cron() {
    local line rc
    line="$(retry_alert_cron_line ' --dry-run')"
    line="${line#"$CRON_SPEC" }"
    info "rehearsing the installed command under a minimal environment..."
    env -i HOME="$HOME" PATH=/usr/bin:/bin LANG=C SHELL=/bin/sh \
        /bin/sh -c "$line" >/dev/null 2>"$INSTALL_DIR/.rehearsal.err"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        say "ERROR:   the command failed when run the way cron will run it"
        say "         (exit $rc). Nothing has been scheduled. It said:"
        if [ -s "$INSTALL_DIR/cron.log" ]; then
            while IFS= read -r l; do say "           $l"; done \
                < <(tail -n 15 "$INSTALL_DIR/cron.log")
        elif [ -s "$INSTALL_DIR/.rehearsal.err" ]; then
            while IFS= read -r l; do say "           $l"; done \
                < <(tail -n 15 "$INSTALL_DIR/.rehearsal.err")
        else
            say "           (no output anywhere -- run it yourself to see why:)"
            say "           $line"
        fi
        rm -f "$INSTALL_DIR/.rehearsal.err"
        exit "$EXIT_FAIL"
    fi
    rm -f "$INSTALL_DIR/.rehearsal.err"
    ok "the command runs correctly in a cron-like environment"
    info "this approximates cron's environment; it does not guarantee it"
}

crontab_write() {
    local content="$1" expect_marker="$2" tmp readback found
    tmp="$(mktemp)"
    printf '%s\n' "$content" | sed -e '/^$/{ $d }' > "$tmp"

    if ! crontab "$tmp" 2>/dev/null; then
        if ! crontab - < "$tmp"; then
            rm -f "$tmp"
            _crontab_restore "crontab refused the new content"
        fi
    fi
    rm -f "$tmp"

    readback="$(crontab_read)"
    if [ -n "$expect_marker" ]; then
        found="$(printf '%s\n' "$readback" | grep -cxF "$expect_marker")"
        [ "$found" -eq 1 ] || _crontab_restore "the managed block is not in the crontab after writing it"
    else
        found="$(printf '%s\n' "$readback" | grep -cxF "$MARK_OPEN")"
        [ "$found" -eq 0 ] || _crontab_restore "the managed block is still in the crontab after removing it"
    fi
}

_crontab_restore() {
    local why="$1"
    say_err "ERROR:   $why."
    if [ -n "$LAST_BACKUP" ] && [ -f "$LAST_BACKUP" ]; then
        if crontab "$LAST_BACKUP" 2>/dev/null; then
            say_err "         Restored the crontab from $LAST_BACKUP."
        else
            say_err "         COULD NOT auto-restore. Do it by hand: crontab $LAST_BACKUP"
        fi
    fi
    exit "$EXIT_CRON"
}

# ------------------------------------------------------------- preflight ----

preflight() {
    head2 "Environment"

    [ -w "$INSTALL_DIR" ] || die "$INSTALL_DIR is not writable."

    local missing=0 f
    for f in run_retry_alert.py run_tests.py config.example.ini \
             templates/digest.html templates/digest.txt \
             retry_alert/cli.py retry_alert/engine.py retry_alert/digest.py; do
        if [ ! -f "$INSTALL_DIR/$f" ]; then
            warn "missing: $f"
            missing=1
        fi
    done
    [ "$missing" -eq 0 ] || die "this does not look like a complete retry-alert install."

    case "$INSTALL_DIR" in
        *"%"*|*";"*|*"#"*|*'"'*|*'$'*|*'`'*|*'\'*)
            die "the install path contains a character this installer cannot put safely in a cron line or config.ini (one of  % ; # \" \$ \` \\ ): $INSTALL_DIR" ;;
    esac
    case "$INSTALL_DIR" in
        *$'\n'*|*$'\r'*) die "the install path contains a line break." ;;
    esac

    ok "install directory: $INSTALL_DIR"
    if [ "$INSTALL_DIR" != "$HOME/firemon-retry-alert" ]; then
        warn "not ~/firemon-retry-alert -- commands quoted in README.md and"
        warn "DEPLOYMENT.md name that path and will need adjusting."
    fi

    local py
    py="$(command -v python3 || true)"
    if [ -n "$py" ]; then
        ok "python3: $py ($("$py" -V 2>&1))"
        info "the tool re-executes itself under a newer interpreter if this"
        info "one predates dataclasses (3.7), so an old default python3 is fine"
    else
        die "no python3 on PATH."
    fi

    if [ -n "${FMRETRY_REEXEC-}" ]; then
        warn "FMRETRY_REEXEC is set in this shell. It is the tool's own"
        warn "exec-loop guard and will break a start under an old interpreter."
        warn "Run: unset FMRETRY_REEXEC"
    fi

    for tool in crontab mailq postconf; do
        if have "$tool"; then ok "$tool: present"; else info "$tool: not present"; fi
    done
}

# --------------------------------------------------------------- verbs ------

run_retry_alert() {
    ( set -a; . "$INSTALL_DIR/secrets.env"; set +a
      python3 "$INSTALL_DIR/run_retry_alert.py" --config "$INSTALL_DIR/config.ini" "$@" )
}

need_config() {
    [ -f "$INSTALL_DIR/config.ini" ] \
        || die "no config.ini in $INSTALL_DIR. Run: bash setup.sh configure"
    [ -f "$INSTALL_DIR/secrets.env" ] \
        || die "no secrets.env in $INSTALL_DIR. Run: bash setup.sh configure"
}

do_configure() {
    rm -f "$INSTALL_DIR/config.ini.new" "$INSTALL_DIR/secrets.env.new"
    preflight
    if [ -f "$INSTALL_DIR/config.ini" ]; then
        info "existing config.ini found -- every prompt below defaults to what"
        info "is already configured. Press Enter to keep a value unchanged."
    fi
    head2 "FireMon"

    local base_url domain_id device_filter exclude_name_contains username password verify_tls
    base_url="$(ask 'FireMon base URL' "$(_existing_or firemon base_url 'https://localhost')" \
        FMRETRY_SETUP_BASE_URL)"
    case "$base_url" in
        HTTP://*|HTTPS://*|Http://*|Https://*)
            base_url="$(printf '%s' "$base_url" | sed -e 's#^[Hh][Tt][Tt][Pp]\(s\{0,1\}\)://#http\1://#')" ;;
    esac
    case "$base_url" in
        http://*|https://*) ;;
        *) die "the base URL must start with http:// or https:// (got '$base_url')." ;;
    esac
    base_url="${base_url%/}"
    reject_unsafe "The base URL" "$base_url"

    domain_id="$(ask 'Domain id' "$(_existing_or firemon domain_id '1')" FMRETRY_SETUP_DOMAIN_ID)"
    need_int "Domain id" "$domain_id"

    # ask(), not a blank-means-something-special prompt: read -p always sets
    # answer="" when the user just presses Enter, so "empty means keep this
    # blank" and "empty means accept the shown default" are indistinguishable
    # at that point -- there is no way to tell them apart from empty input
    # alone. An explicit sentinel word sidesteps the ambiguity entirely,
    # mirroring how PolicyOptimizer-Email-Digest's setup.sh handles its own
    # optional/blank fields (e.g. the team-restriction prompt's "all").
    #
    # config_value's own blank-vs-absent cannot be told apart (both print an
    # empty string), so the existing value is read directly here rather than
    # through _existing_or: no config.ini yet needs the real filter text as
    # the default, but an existing blank (a prior "all") needs the sentinel
    # word shown back, not the filter text -- two different fallbacks for the
    # same "nothing there" reading, which _existing_or's single fallback
    # cannot express.
    local device_filter_in filter_default="DEVICEPACK.DEVICETYPE = 'MANAGEMENT_STATION'"
    if [ -f "$INSTALL_DIR/config.ini" ]; then
        local existing_filter
        existing_filter="$(config_value "$INSTALL_DIR/config.ini" firemon device_filter 2>/dev/null)"
        if [ "$existing_filter" != "(no config)" ]; then
            if [ -z "$existing_filter" ]; then filter_default='all'; else filter_default="$existing_filter"; fi
        fi
    fi
    device_filter_in="$(ask 'SIQL device filter, or "all" to monitor every device type' \
        "$filter_default" FMRETRY_SETUP_DEVICE_FILTER)"
    case "$(printf '%s' "$device_filter_in" | tr '[:upper:]' '[:lower:]')" in
        all|none) device_filter="" ;;
        *)        device_filter="$device_filter_in" ;;
    esac
    reject_unsafe "The device filter" "$device_filter"

    # Same "none" sentinel as the SMTP username prompt below -- ask() cannot
    # itself accept a blank answer (it loops asking again), and blank is the
    # common, legitimate choice here ("exclude nothing by name").
    local exclude_names_in exclude_names_default
    exclude_names_default="$(_existing_or retry exclude_name_contains '')"
    [ -z "$exclude_names_default" ] && exclude_names_default='none'
    exclude_names_in="$(ask 'Exclude devices whose name contains (comma-separated substrings, or "none")' \
        "$exclude_names_default" FMRETRY_SETUP_EXCLUDE_NAME_CONTAINS)"
    if [ "$(printf '%s' "$exclude_names_in" | tr '[:upper:]' '[:lower:]')" = "none" ]; then
        exclude_name_contains=""
    else
        exclude_name_contains="$exclude_names_in"
    fi
    reject_unsafe "The excluded name pattern" "$exclude_name_contains"

    username="$(ask 'FireMon service account username' "$(_existing_or firemon username '')" \
        FMRETRY_SETUP_USERNAME)"
    reject_unsafe "The username" "$username"
    password="$(ask_secret 'FireMon password' FMRETRY_PASSWORD "$(_current_secret FMRETRY_PASSWORD)")"

    local tls_default
    tls_default="$(_existing_or firemon verify_tls '')"
    if [ -n "$tls_default" ]; then
        tls_default="$(_bool_to_yesno "$tls_default")"
    else
        case "$base_url" in *localhost*|*127.0.0.1*) tls_default='no' ;; *) tls_default='yes' ;; esac
    fi
    verify_tls="$(ask 'Verify the TLS certificate? (appliances usually self-sign)' "$tls_default" FMRETRY_SETUP_VERIFY_TLS)"
    verify_tls="$(yesno_to_bool "$verify_tls")"

    head2 "Mail"
    local smtp_host smtp_port smtp_tls smtp_user smtp_pass from_address recipients
    smtp_host="$(ask 'SMTP relay host' "$(_existing_or smtp host 'localhost')" FMRETRY_SETUP_SMTP_HOST)"
    reject_unsafe "The SMTP host" "$smtp_host"
    smtp_port='25'; smtp_tls='false'; smtp_user=''; smtp_pass=''
    if [ "$smtp_host" != "localhost" ] && [ "$smtp_host" != "127.0.0.1" ]; then
        smtp_port="$(ask 'SMTP port' "$(_existing_or smtp port '25')" FMRETRY_SETUP_SMTP_PORT)"
        need_int "SMTP port" "$smtp_port"
        local smtp_tls_default
        smtp_tls_default="$(_existing_or smtp use_starttls '')"
        if [ -n "$smtp_tls_default" ]; then smtp_tls_default="$(_bool_to_yesno "$smtp_tls_default")"; else smtp_tls_default='yes'; fi
        smtp_tls="$(yesno_to_bool "$(ask 'Use STARTTLS?' "$smtp_tls_default" FMRETRY_SETUP_SMTP_STARTTLS)")"
        local smtp_user_default
        smtp_user_default="$(_existing_or smtp username '')"
        [ -z "$smtp_user_default" ] && smtp_user_default='none'
        smtp_user="$(ask 'SMTP username (blank if the relay does not authenticate)' \
            "$smtp_user_default" FMRETRY_SETUP_SMTP_USERNAME)"
        [ "$smtp_user" = "none" ] && smtp_user=""
        reject_unsafe "The SMTP username" "$smtp_user"
        if [ -n "$smtp_user" ]; then
            smtp_pass="$(ask_secret 'SMTP password' FMRETRY_SMTP_PASSWORD "$(_current_secret FMRETRY_SMTP_PASSWORD)")"
        fi
    else
        info "localhost relay: no port, STARTTLS or credentials to set"
    fi

    from_address="$(ask 'From address for digest emails' "$(_existing_or smtp from_address '')" \
        FMRETRY_SETUP_FROM_ADDRESS)"
    reject_unsafe "The from address" "$from_address"
    case "$from_address" in *@*.*) ;; *) die "'$from_address' is not a usable email address." ;; esac

    local recipients_in
    recipients_in="$(ask 'Digest recipients (comma-separated emails)' "$(_existing_or smtp recipients '')" \
        FMRETRY_SETUP_RECIPIENTS)"
    recipients="$(_parse_email_list "$recipients_in" "recipient address")"
    [ -n "$recipients" ] || die "at least one digest recipient is required."

    local digest_interval_hours
    digest_interval_hours="$(ask 'Digest interval in hours (one summary email covering everything since the last one)' \
        "$(_existing_or digest interval_hours '24')" FMRETRY_SETUP_DIGEST_INTERVAL_HOURS)"
    need_number "Digest interval hours" "$digest_interval_hours"

    if have postconf; then
        local canon
        canon="$(postconf -h sender_canonical_maps 2>/dev/null || true)"
        if [ -n "$canon" ]; then
            warn "postfix rewrites senders via $canon -- the recipient may not"
            warn "see $from_address. Check /etc/postfix/sender_canonical."
        fi
    fi

    local candidate="$INSTALL_DIR/config.ini.new"
    cp -f "$INSTALL_DIR/config.example.ini" "$candidate" \
        || die "could not copy config.example.ini"

    TABLE_FILE="$(mktemp)"
    : > "$TABLE_FILE"
    table_add firemon base_url      "$base_url"
    table_add firemon domain_id     "$domain_id"
    table_add firemon device_filter "$device_filter"
    table_add retry   exclude_name_contains "$exclude_name_contains"
    table_add firemon username      "$username"
    table_add firemon verify_tls    "$verify_tls"
    table_add smtp    host          "$smtp_host"
    table_add smtp    port          "$smtp_port"
    table_add smtp    use_starttls  "$smtp_tls"
    table_add smtp    username      "$smtp_user"
    table_add smtp    from_address  "$from_address"
    table_add smtp    recipients    "$recipients"
    table_add digest  interval_hours "$digest_interval_hours"
    table_add paths   template_dir  "$INSTALL_DIR/templates"
    table_add paths   log_file      "$INSTALL_DIR/retry_alert.log"
    table_add retry   state_dir     "$INSTALL_DIR/state"
    patch_config "$candidate" "$TABLE_FILE"
    rm -f "$TABLE_FILE"

    stage_secrets "$password" "$smtp_pass"
    local staged_secrets="$INSTALL_DIR/secrets.env.new"

    head2 "About to write"
    say ""
    local mask='s/^([+-]?[[:space:]]*(password)[[:space:]]*=[[:space:]]*).+/\1<hidden>/'
    if have diff && [ -f "$INSTALL_DIR/config.ini" ]; then
        diff -u "$INSTALL_DIR/config.ini" "$candidate" | sed -E "$mask" | sed -n '1,60p'
    else
        grep -nE '^[[:space:]]*(base_url|domain_id|device_filter|exclude_name_contains|username|verify_tls|host|port|use_starttls|from_address|recipients|interval_hours|template_dir|log_file|state_dir)[[:space:]]*=' "$candidate" \
            | sed 's/^/    /'
    fi
    say ""
    info "config.ini   (0600) -- no passwords: those go in secrets.env"
    info "secrets.env  (0600) -- sourced by the cron line"
    info "[retry] failure_statuses / stale_after_hours / grace_period_minutes /"
    info "ignore_orphaned / ignore_unlicensed, and [smtp] subject_prefix, keep"
    info "the config.example.ini defaults; edit config.ini by hand to change them."
    say ""
    if ! confirm "Write these files?"; then
        rm -f "$candidate" "$staged_secrets"
        skip "nothing written"
        exit "$EXIT_OK"
    fi

    head2 "Proving the configuration"
    info "running a --dry-run scan against the candidate config..."
    local rc
    ( set -a; . "$staged_secrets"; set +a
      python3 "$INSTALL_DIR/run_retry_alert.py" --config "$candidate" \
          --dry-run --log-level WARNING )
    rc=$?
    # 0 = clean scan, 2 = scan worked but found devices that would be
    # retried/alerted (expected on a box with pre-existing issues). Only a
    # fatal exit (1, auth/API failure) means the configuration itself is wrong.
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
        rm -f "$candidate" "$staged_secrets"
        say "ERROR:   the tool could not run with this configuration (exit $rc)."
        say "         config.ini and secrets.env are unchanged."
        exit "$EXIT_FAIL"
    fi
    ok "the configuration authenticates and can scan device health"

    if [ -f "$INSTALL_DIR/config.ini" ]; then
        mkdir -p "$INSTALL_DIR/backups"
        cp -p "$INSTALL_DIR/config.ini" \
            "$INSTALL_DIR/backups/config-$(date '+%Y%m%dT%H%M%S').ini.bak"
        info "previous config.ini backed up under backups/"
    fi
    mv "$candidate" "$INSTALL_DIR/config.ini"
    chmod 600 "$INSTALL_DIR/config.ini"
    promote_secrets
    mkdir -p "$INSTALL_DIR/state"
    chmod 700 "$INSTALL_DIR/state"
    ok "config.ini written and proven against the live server"
    action "next: bash setup.sh verify, then bash setup.sh schedule"
}

stage_secrets() {
    local fm_pass="$1" smtp_pass="$2" tmp="$INSTALL_DIR/secrets.env.new"
    local esc_fm esc_smtp
    esc_fm="${fm_pass//\'/\'\\\'\'}"
    esc_smtp="${smtp_pass//\'/\'\\\'\'}"

    : > "$tmp"
    chmod 600 "$tmp"
    printf '# FireMon retry-alert secrets. Sourced by the cron line; mode 0600.\n' >> "$tmp"
    printf '# Written by setup.sh -- edit by hand if a password changes.\n' >> "$tmp"
    printf "export FMRETRY_PASSWORD='%s'\n" "$esc_fm" >> "$tmp"
    if [ -n "$smtp_pass" ]; then
        printf "export FMRETRY_SMTP_PASSWORD='%s'\n" "$esc_smtp" >> "$tmp"
    fi

    local recovered
    recovered="$( unset FMRETRY_PASSWORD; . "$tmp" >/dev/null 2>&1; printf '%s' "${FMRETRY_PASSWORD-}" )"
    if [ "$recovered" != "$fm_pass" ]; then
        rm -f "$tmp"
        die "secrets.env did not read back as written -- the password contains something this script cannot quote. Set FMRETRY_PASSWORD in the environment instead."
    fi
}

promote_secrets() {
    local tmp="$INSTALL_DIR/secrets.env.new"
    [ -f "$tmp" ] || return 0
    if [ -f "$INSTALL_DIR/secrets.env" ]; then
        mkdir -p "$INSTALL_DIR/backups"
        cp -p "$INSTALL_DIR/secrets.env" \
            "$INSTALL_DIR/backups/secrets-$(date '+%Y%m%dT%H%M%S').env.bak"
        chmod 600 "$INSTALL_DIR/backups"/secrets-*.env.bak 2>/dev/null
    fi
    mv "$tmp" "$INSTALL_DIR/secrets.env"
    chmod 600 "$INSTALL_DIR/secrets.env"
    prune_secret_backups
    ok "secrets.env written (0600); no password is stored in config.ini"
}

prune_secret_backups() {
    ls -1t "$INSTALL_DIR/backups"/secrets-*.env.bak 2>/dev/null | tail -n +4 \
        | while IFS= read -r old; do rm -f "$old"; done
}

do_verify() {
    need_config
    head2 "Self-test"
    if python3 "$INSTALL_DIR/run_tests.py" >/dev/null 2>"$INSTALL_DIR/.tests.err"; then
        ok "the unit tests pass under this interpreter"
    else
        say "ERROR:   the unit tests failed. The install is not healthy:"
        while IFS= read -r l; do say "           $l"; done \
            < <(tail -n 20 "$INSTALL_DIR/.tests.err")
        rm -f "$INSTALL_DIR/.tests.err"
        exit "$EXIT_FAIL"
    fi
    rm -f "$INSTALL_DIR/.tests.err"

    head2 "Dry-run scan"
    run_retry_alert --dry-run --log-level INFO
    local rc=$?
    case "$rc" in
        0) ok "scan clean -- nothing needs retrying right now" ;;
        2) info "scan found device(s) that would be retried/alerted -- see above" ;;
        *) die "the dry-run scan failed (exit $rc)." "$rc" ;;
    esac

    head2 "Pending state"
    run_retry_alert --list-pending

    head2 "Did mail leave the box last time it tried?"
    if have mailq; then
        local queued
        queued="$(mail_queue_count)"
        info "mail queue holds $queued message(s)"
        [ "$queued" != "0" ] && [ "$queued" != "?" ] \
            && action "anything queued is NOT delivered. Run mailq and read the reason under each entry."
    else
        info "mailq not present -- check delivery from wherever your relay is"
    fi
    if have postconf; then
        info "relayhost:        $(postconf -h relayhost 2>/dev/null)"
        info "myhostname:       $(postconf -h myhostname 2>/dev/null)"
    fi
    action "'exit 2' from a dry-run means real devices are unhealthy right now,"
    action "not that anything is broken -- --dry-run never emails or retries."
}

# Run one real cycle right now, off-schedule -- the only setup.sh path (besides
# a live cron run) that triggers real manual retrievals. Everything else here
# renders or scans only. Mirrors do_send() in PolicyOptimizer-Email-Digest's
# setup.sh, adapted: a single run rarely emails anything by itself now --
# see do_send_digest for that.
do_run_now() {
    need_config
    head2 "Run now"
    warn "this triggers REAL manual retrievals on unhealthy devices -- not a"
    warn "dry run. Anything still broken afterward is recorded for the next"
    warn "digest, not emailed immediately, unless a digest also happens to be"
    warn "due right now. Cron will keep running on its own schedule regardless."
    confirm "Run a live cycle now?" || { skip "nothing run"; exit "$EXIT_OK"; }

    head2 "Running"
    run_retry_alert --log-level INFO
    local rc=$?
    case "$rc" in
        0) ok "clean cycle -- nothing needed retrying, or everything resolved" ;;
        2) warn "device(s) still need attention -- see above. A digest email"
           warn "may have gone out if one was due; otherwise it's queued." ;;
        *) die "the run failed (exit $rc)." "$rc" ;;
    esac
    if have mailq; then
        local queued; queued="$(mail_queue_count)"
        info "mail queue holds $queued message(s)"
        [ "$queued" != "0" ] && [ "$queued" != "?" ] \
            && action "anything queued is NOT delivered. Run mailq and read the reason under each entry."
    fi
    return "$rc"
}

# Force-sends whatever is currently in the digest ledger, regardless of
# whether the interval timer says it's due, and resets that timer. The only
# way to see a digest render and deliver without waiting for the interval.
do_send_digest() {
    need_config
    head2 "Send digest now"
    info "this sends whatever is currently in state/ledger.jsonl, if anything,"
    info "and resets the digest timer -- regardless of whether a digest is"
    info "actually due yet."
    confirm "Send the digest now?" || { skip "nothing sent"; exit "$EXIT_OK"; }

    head2 "Sending"
    run_retry_alert --send-digest --log-level INFO
    local rc=$?
    case "$rc" in
        0) ok "digest sent (or there was nothing to report)" ;;
        2) warn "digest sent -- some devices are still broken. Check mailq below." ;;
        *) die "the digest send failed (exit $rc)." "$rc" ;;
    esac
    if have mailq; then
        local queued; queued="$(mail_queue_count)"
        info "mail queue holds $queued message(s)"
        [ "$queued" != "0" ] && [ "$queued" != "?" ] \
            && action "anything queued is NOT delivered. Run mailq and read the reason under each entry."
    fi
    return "$rc"
}

# Read-only: what's currently awaiting a retry confirmation, and what's
# sitting in the digest ledger waiting for the next send.
do_list_pending() {
    need_config
    run_retry_alert --list-pending
}

# Read-only: the tail of what cron has actually been doing, plus whether mail
# is backing up -- the fastest way to see "is this actually working" without
# leaving the menu.
do_activity() {
    head2 "Recent cron.log"
    if [ -s "$INSTALL_DIR/cron.log" ]; then
        tail -n 40 "$INSTALL_DIR/cron.log"
    else
        info "cron.log is empty or doesn't exist yet -- nothing has run through cron."
    fi
    if [ -s "$INSTALL_DIR/retry_alert.log" ]; then
        head2 "Recent retry_alert.log"
        tail -n 20 "$INSTALL_DIR/retry_alert.log"
    fi
    head2 "Mail"
    if have mailq; then
        local queued; queued="$(mail_queue_count)"
        info "mail queue holds $queued message(s)"
        [ "$queued" != "0" ] && [ "$queued" != "?" ] \
            && action "anything queued is NOT delivered. Run mailq and read the reason under each entry."
    else
        info "mailq not present -- check delivery from wherever your relay is"
    fi
}

# Opens config.ini in $EDITOR for the settings the wizard doesn't prompt for
# (failure_statuses, stale_after_hours, grace_period_minutes, ...), then
# re-runs a --dry-run scan so a typo is caught immediately, not on the next
# cron tick.
do_edit_config() {
    need_config
    local editor="${EDITOR:-vi}"
    if ! have "$editor"; then
        warn "\$EDITOR ('$editor') not found on PATH -- falling back to vi."
        editor="vi"
    fi
    require_tty "edit config.ini" "EDITOR"
    info "opening config.ini in $editor -- save and quit to return."
    "$editor" "$INSTALL_DIR/config.ini" < /dev/tty > /dev/tty 2>&1
    head2 "Re-checking after edit"
    run_retry_alert --dry-run --log-level WARNING
    local rc=$?
    case "$rc" in
        0|2) ok "config.ini still parses and the tool still runs (exit $rc)." ;;
        *)   warn "the edited config.ini failed to run (exit $rc) -- check your edits." ;;
    esac
}

do_schedule() {
    need_config
    have crontab || die "no crontab command on this host. Scheduling needs one; configuration does not."

    head2 "Re-checking the configuration"
    run_retry_alert --dry-run --log-level ERROR >/dev/null
    local rc=$?
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
        die "a dry-run scan exited $rc -- fix that before scheduling." "$rc"
    fi
    ok "the tool runs and authenticates"

    local flags=' --dry-run'
    if [ "$OPT_LIVE" -eq 1 ]; then
        flags=''
    else
        info "scheduling with --dry-run. It will log what it would do and change nothing."
        info "Promote it with: bash setup.sh schedule --live"
    fi

    rehearse_cron

    head2 "Crontab"
    local existing stripped unmanaged
    existing="$(crontab_read)"
    crontab_backup "$existing"

    stripped="$(strip_block "$existing" "$MARK_OPEN" "$MARK_CLOSE")"
    stripped="$(strip_block "$stripped" "$TRIM_OPEN" "$TRIM_CLOSE")"

    unmanaged="$(printf '%s\n' "$stripped" | grep 'run_retry_alert\.py' || true)"
    if [ -n "$unmanaged" ]; then
        warn "the crontab runs the tool from outside the managed block:"
        while IFS= read -r l; do say "           $l"; done <<< "$unmanaged"
        if [ "$OPT_ADOPT" -eq 1 ]; then
            warn "--adopt given: that line will be replaced by the managed block."
        elif confirm "Replace it with a managed block?"; then
            :
        else
            die "stopping. Two retry-alert jobs would trigger retries and alerts twice."
        fi
        stripped="$(printf '%s\n' "$stripped" | grep -v 'run_retry_alert\.py')"
    fi

    local block
    block="$(build_managed_block "$flags" "$(trailing_mailto "$stripped")")"

    local updated
    if [ -z "$(printf '%s' "$stripped" | tr -d '[:space:]')" ]; then
        updated="$block"
    else
        updated="$stripped
$block"
    fi

    crontab_write "$updated" "$MARK_OPEN"
    ok "cron entry installed: $CRON_SPEC"
    info "$(retry_alert_cron_line "$flags")"
    info "previous crontab: $LAST_BACKUP"
    if [ "$OPT_LOGROTATE" -eq 1 ]; then
        info "log rotation on (monthly): cron.log and retry_alert.log are trimmed"
        action "to their last 2000 lines. state/ is never touched by rotation."
    else
        info "log rotation OFF (--no-logrotate). Logs grow unbounded; trim yourself."
    fi
    if [ "$OPT_LIVE" -eq 1 ]; then
        action "it will trigger real manual retrievals and send a real digest"
        action "email once the interval elapses."
    else
        action "it is scheduled with --dry-run. Read cron.log after the first"
        action "few runs, then: bash setup.sh schedule --live"
    fi
}

do_unschedule() {
    have crontab || die "no crontab command on this host."
    local existing stripped
    existing="$(crontab_read)"
    if ! printf '%s\n' "$existing" | grep -qxF "$MARK_OPEN"; then
        skip "no managed retry-alert block in the crontab"
        return 0
    fi
    crontab_backup "$existing"
    stripped="$(strip_block "$existing" "$MARK_OPEN" "$MARK_CLOSE")"
    stripped="$(strip_block "$stripped" "$TRIM_OPEN" "$TRIM_CLOSE")"
    crontab_write "$stripped" ""
    ok "cron entry removed. Files and logs are untouched."
}

do_restore_crontab() {
    have crontab || die "no crontab command on this host."
    local file="$RESTORE_FILE"
    if [ -z "$file" ]; then
        file="$(ls -1t "$INSTALL_DIR/backups"/crontab-*.bak 2>/dev/null | head -n 1)"
        [ -n "$file" ] || die "no crontab backups in $INSTALL_DIR/backups"
    fi
    [ -f "$file" ] || die "no such backup: $file"
    head2 "Restoring $file"
    say ""
    sed 's/^/    /' "$file"
    say ""
    confirm "Install this as the crontab?" || { skip "left alone"; exit "$EXIT_OK"; }
    crontab_backup "$(crontab_read)"

    # Not crontab_write(): that function's read-back check asserts our OWN
    # managed-block marker is present or absent, which is the right check for
    # do_schedule/do_unschedule (which know which state they're aiming for) but
    # wrong here -- $file is an arbitrary prior crontab that may legitimately
    # contain the marker (e.g. a backup taken by do_unschedule, right before it
    # removed the block, or one taken moments ago by the crontab_backup call
    # above). Asserting "marker absent" against that content would fail and
    # trigger a bogus revert even though the restore worked correctly. Instead,
    # just confirm the write took effect at all (not silently a no-op).
    if ! crontab "$file" 2>/dev/null && ! crontab - < "$file"; then
        die "crontab refused $file -- nothing was changed (your prior crontab is backed up: $LAST_BACKUP)." "$EXIT_CRON"
    fi
    local readback
    readback="$(crontab_read)"
    if [ -z "$(printf '%s' "$readback" | tr -d '[:space:]')" ] && [ -s "$file" ]; then
        say_err "ERROR:   the crontab reads back empty after restoring a non-empty file."
        if crontab "$LAST_BACKUP" 2>/dev/null; then
            say_err "         Restored the crontab from $LAST_BACKUP."
        else
            say_err "         COULD NOT auto-restore. Do it by hand: crontab $LAST_BACKUP"
        fi
        exit "$EXIT_CRON"
    fi
    ok "crontab restored from $file"
    info "verify with: crontab -l"
}

# --------------------------------------------------------------- update -----

local_version() {
    if [ -s "$INSTALL_DIR/VERSION" ]; then
        tr -d '[:space:]' < "$INSTALL_DIR/VERSION"
    else
        printf 'unknown (pre-1.1, no VERSION file)'
    fi
}

_fetch() {
    local url="$1" dest="$2"
    have curl || die "curl is not installed. Download the archive on another machine from https://github.com/$GITHUB_REPO and run: bash setup.sh update --from FILE"
    curl -fsSL --connect-timeout 15 --max-time 300 -o "$dest" "$url"
}

# Unpacks a .tar.gz or .zip into $2 and prints the directory inside it that
# holds setup.sh (GitHub archives wrap everything in a REPO-BRANCH/ folder;
# a hand-made zip of the project may not). Dies if it is not this tool.
_unpack_archive() {
    local archive="$1" work="$2" found
    case "$archive" in
        *.zip|*.ZIP)
            if have unzip; then
                unzip -q "$archive" -d "$work" || die "could not unzip $archive"
            else
                python3 -m zipfile -e "$archive" "$work" || die "could not unzip $archive (no unzip, and python3 -m zipfile failed)"
            fi ;;
        *)
            tar -xzf "$archive" -C "$work" || die "could not extract $archive (expected a .tar.gz or .zip)" ;;
    esac
    found="$(find "$work" -maxdepth 2 -name run_retry_alert.py -print 2>/dev/null | head -n 1)"
    [ -n "$found" ] || die "that archive does not contain run_retry_alert.py -- not a Retrieval-Retry-Alert archive."
    found="$(dirname "$found")"
    [ -d "$found/retry_alert" ] && [ -f "$found/setup.sh" ] \
        || die "that archive is missing retry_alert/ or setup.sh -- not a complete Retrieval-Retry-Alert archive."
    printf '%s' "$found"
}

do_update() {
    head2 "Update"
    local current new_dir src work remote
    current="$(local_version)"
    info "installed:  $current  ($INSTALL_DIR)"

    if [ -n "$UPDATE_FROM" ]; then
        [ "$OPT_CHECK" -eq 1 ] && die "--check compares against GitHub; it does not take --from."
        [ -f "$UPDATE_FROM" ] || die "no such file: $UPDATE_FROM"
        src="$UPDATE_FROM"
        info "source:     $src"
    else
        # Only the archive URL is used (github.com, which redirects to
        # codeload.github.com) -- never raw.githubusercontent.com or
        # api.github.com. A firewall that allows the archive often blocks
        # those (seen in the field: the archive downloaded fine, the VERSION
        # lookup on raw.githubusercontent.com timed out). The archive is
        # ~70 KB, so even --check just downloads it and reads VERSION from it.
        local url="https://github.com/$GITHUB_REPO/archive/refs/heads/$GITHUB_BRANCH.tar.gz"
        info "source:     https://github.com/$GITHUB_REPO ($GITHUB_BRANCH)"
        work="$(mktemp -d)"
        src="$work/archive.tar.gz"
        info "downloading..."
        if ! _fetch "$url" "$src"; then
            rm -rf "$work"
            say "ERROR:   could not download $url"
            say "         from this box. If it needs a proxy, export https_proxy first."
            say "         Otherwise download it elsewhere, copy it here, and run:"
            say "         bash setup.sh update --from $GITHUB_BRANCH.tar.gz"
            exit "$EXIT_FAIL"
        fi
    fi

    [ -n "${work:-}" ] || work="$(mktemp -d)"
    mkdir -p "$work/unpacked"
    new_dir="$(_unpack_archive "$src" "$work/unpacked")"
    local new_version='unknown (no VERSION file)'
    [ -s "$new_dir/VERSION" ] && new_version="$(tr -d '[:space:]' < "$new_dir/VERSION")"

    if [ -z "$UPDATE_FROM" ]; then
        info "on GitHub:  $new_version"
        if [ "$OPT_CHECK" -eq 1 ]; then
            rm -rf "$work"
            if [ "$new_version" = "$current" ]; then
                ok "up to date."
            else
                action "a different version is available. Install it with: bash setup.sh update"
            fi
            return 0
        fi
        if [ "$new_version" = "$current" ]; then
            ok "already up to date."
            if ! confirm "Reinstall $new_version over itself anyway?"; then
                rm -rf "$work"
                skip "left alone"
                return 0
            fi
        fi
    fi

    say ""
    info "about to replace:  $CODE_FILES"
    info "                   $(printf '%s/ ' $CODE_DIRS)"
    info "never touched:     config.ini secrets.env state/ backups/ *.log, and the cron entry"
    say ""
    if ! confirm "Install version $new_version over $current?"; then
        rm -rf "$work"
        skip "nothing changed"
        return 0
    fi

    # Keep what is there now. Restoring is a plain copy back, so an update
    # that turns out to be bad is a one-line undo, printed at the end.
    local stamp keep f d
    stamp="$(date '+%Y%m%dT%H%M%S')"
    keep="$INSTALL_DIR/backups/code-$stamp"
    mkdir -p "$keep" || die "could not create $keep"
    for f in $CODE_FILES; do
        [ -f "$INSTALL_DIR/$f" ] && cp -p "$INSTALL_DIR/$f" "$keep/$f"
    done
    for d in $CODE_DIRS; do
        [ -d "$INSTALL_DIR/$d" ] && cp -Rp "$INSTALL_DIR/$d" "$keep/$d"
    done
    ok "current code kept in $keep"

    # Files go in as NAME.new then mv -- a rename swaps the directory entry
    # atomically and, for setup.sh in particular, leaves the copy bash is
    # executing right now untouched (bash reads a script incrementally from
    # the open file; overwriting it in place would corrupt this very run).
    for f in $CODE_FILES; do
        [ -f "$new_dir/$f" ] || continue
        cp -p "$new_dir/$f" "$INSTALL_DIR/$f.new" && mv -f "$INSTALL_DIR/$f.new" "$INSTALL_DIR/$f" \
            || die "could not install $f -- restore with: cp -Rp $keep/* $INSTALL_DIR/"
    done
    for d in $CODE_DIRS; do
        [ -d "$new_dir/$d" ] || continue
        rm -rf "$INSTALL_DIR/$d.new"
        cp -Rp "$new_dir/$d" "$INSTALL_DIR/$d.new" || die "could not stage $d -- restore with: cp -Rp $keep/* $INSTALL_DIR/"
        rm -rf "$INSTALL_DIR/$d"
        mv "$INSTALL_DIR/$d.new" "$INSTALL_DIR/$d" || die "could not install $d -- restore with: cp -Rp $keep/* $INSTALL_DIR/"
    done
    chmod 755 "$INSTALL_DIR/setup.sh" "$INSTALL_DIR/run_retry_alert.py" "$INSTALL_DIR/run_tests.py" 2>/dev/null
    find "$INSTALL_DIR/retry_alert" "$INSTALL_DIR/tests" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null
    rm -rf "$work"
    ok "code updated: $current -> $new_version"
    # Keep only the code this update just replaced; each backup is a full
    # copy of the tool, and anything older than the one you can roll back to
    # is clutter. Prunes any pile left by earlier versions too.
    ls -1dt "$INSTALL_DIR/backups"/code-* 2>/dev/null | tail -n +2 \
        | while IFS= read -r old; do rm -rf "$old"; done

    head2 "Self-test"
    if python3 "$INSTALL_DIR/run_tests.py" >/dev/null 2>"$INSTALL_DIR/.tests.err"; then
        ok "the unit tests pass"
    else
        say "ERROR:   the unit tests FAILED on the new code:"
        while IFS= read -r l; do say "           $l"; done \
            < <(tail -n 20 "$INSTALL_DIR/.tests.err")
        rm -f "$INSTALL_DIR/.tests.err"
        action "roll back with:  cp -Rp \"$keep\"/* \"$INSTALL_DIR\"/"
        exit "$EXIT_FAIL"
    fi
    rm -f "$INSTALL_DIR/.tests.err"

    if [ -f "$INSTALL_DIR/config.ini" ] && [ -f "$INSTALL_DIR/secrets.env" ]; then
        run_retry_alert --dry-run --log-level ERROR >/dev/null
        local rc=$?
        case "$rc" in
            0|2) ok "config.ini still loads and the tool still authenticates (exit $rc)" ;;
            *)   warn "the tool exited $rc against config.ini -- run: bash setup.sh verify" ;;
        esac
    fi
    info "the cron entry was not changed; it runs the new code from its next tick."
    info "undo:  cp -Rp \"$keep\"/* \"$INSTALL_DIR\"/"
}

do_status() {
    head2 "Install"
    say "  directory:     $INSTALL_DIR"
    say "  version:       $(local_version)   (bash setup.sh update --check compares with GitHub)"
    say "  runs as:       $(id -un)"
    say "  python3:       $(python3 -V 2>&1) at $(command -v python3 || echo 'not found')"
    local f
    for f in config.ini secrets.env; do
        if [ -f "$INSTALL_DIR/$f" ]; then
            say "  $f:$(printf '%*s' $((14 - ${#f})) '')mode $(stat -c '%a' "$INSTALL_DIR/$f" 2>/dev/null || echo '?')"
        else
            say "  $f:$(printf '%*s' $((14 - ${#f})) '')not present"
        fi
    done

    if [ -f "$INSTALL_DIR/config.ini" ]; then
        head2 "Configuration"
        local c="$INSTALL_DIR/config.ini"
        say "  base_url:       $(config_value "$c" firemon base_url)"
        say "  domain_id:      $(config_value "$c" firemon domain_id)"
        say "  device_filter:  $(config_value "$c" firemon device_filter | sed 's/^$/(all devices)/')"
        say "  excl. names:    $(config_value "$c" retry exclude_name_contains | sed 's/^$/(none)/')"
        say "  username:       $(config_value "$c" firemon username)"
        say "  verify_tls:     $(config_value "$c" firemon verify_tls)"
        say "  smtp host:      $(config_value "$c" smtp host):$(config_value "$c" smtp port)"
        say "  from_address:   $(config_value "$c" smtp from_address)"
        say "  recipients:     $(config_value "$c" smtp recipients)"
        say "  digest_interval: $(config_value "$c" digest interval_hours)h"
        say "  state_dir:      $(config_value "$c" retry state_dir)"
    fi

    if [ -f "$INSTALL_DIR/secrets.env" ]; then
        head2 "Secrets"
        grep -o '^export [A-Z_]*' "$INSTALL_DIR/secrets.env" 2>/dev/null \
            | sed 's/^export /  defined: /' || say "  (none)"
    fi

    head2 "Schedule"
    if ! have crontab; then
        say "  crontab:       not present on this host"
    else
        local existing
        existing="$(crontab -l 2>/dev/null || true)"
        if printf '%s\n' "$existing" | grep -qxF "$MARK_OPEN"; then
            local line
            line="$(printf '%s\n' "$existing" | grep 'run_retry_alert\.py' | head -n 1)"
            say "  managed block: present"
            say "  line:          $line"
            case "$line" in
                "$CRON_SPEC"*) ;;
                *) say "  note:          the schedule differs from the default $CRON_SPEC" ;;
            esac
            case "$line" in
                *--dry-run*) say "  mode:          DRY RUN -- logs only, retries/digests nothing" ;;
                *)           say "  mode:          LIVE -- retries devices and sends the digest when due" ;;
            esac
        elif printf '%s\n' "$existing" | grep -q 'run_retry_alert\.py'; then
            say "  managed block: absent, but an unmanaged line exists:"
            printf '%s\n' "$existing" | grep 'run_retry_alert\.py' | sed 's/^/      /'
        else
            say "  managed block: not installed"
        fi
    fi

    head2 "Last run"
    if [ -s "$INSTALL_DIR/cron.log" ]; then
        say "  cron.log:      $(wc -c < "$INSTALL_DIR/cron.log") bytes"
        say "  last line:     $(tail -n 1 "$INSTALL_DIR/cron.log")"
    else
        say "  cron.log:      never written"
    fi
    if [ -f "$INSTALL_DIR/state/pending.json" ]; then
        say "  pending.json:  $(wc -c < "$INSTALL_DIR/state/pending.json") bytes"
    fi

    head2 "Mail"
    if have mailq; then
        say "  queued:        $(mail_queue_count) message(s) -- anything queued is NOT delivered"
    else
        say "  mailq:         not present"
    fi

    local backups
    backups="$(ls -1 "$INSTALL_DIR/backups" 2>/dev/null | wc -l)"
    head2 "Backups"
    say "  $INSTALL_DIR/backups: ${backups:-0} file(s)"
}

do_uninstall() {
    head2 "Uninstall"
    if have crontab; then
        do_unschedule
    fi
    if [ -f "$INSTALL_DIR/secrets.env" ] && confirm "Delete secrets.env (it holds the password)?"; then
        rm -f "$INSTALL_DIR/secrets.env"
        ok "secrets.env deleted"
    fi
    if [ -f "$INSTALL_DIR/config.ini" ] && confirm "Archive config.ini to backups/?"; then
        mkdir -p "$INSTALL_DIR/backups"
        mv "$INSTALL_DIR/config.ini" \
            "$INSTALL_DIR/backups/config-removed-$(date '+%Y%m%dT%H%M%S').ini.bak"
        ok "config.ini moved to backups/"
    fi
    prune_secret_backups
    info "state/, cron.log and retry_alert.log were left alone -- they are the"
    info "only record of what was actually retried and alerted on."
}

# ============================================================ menu / TUI ====
#
# A pointer-driven menu -- the "front door" for an operator who does not know
# the verbs. It is a thin dispatcher: each choice re-invokes THIS script with
# the matching verb as a fresh CHILD process, so an action's exit, its traps,
# and the crontab-abort signal all stay inside that child and can never disturb
# the menu. Every tested verb runs unchanged. Mirrors the equivalent section in
# PolicyOptimizer-Email-Digest's setup.sh.
#
# Rendering goes through tput, so it uses the terminal's real terminfo and is
# correct under tmux. dialog and whiptail are absent on FMOS, so this is pure
# bash. With no interactive terminal, or a dumb TERM, it drops to a plain
# numbered prompt.

TUI_ANSI=0
tui_caps() {
    TUI_ANSI=0
    [ -t 0 ] && [ -t 1 ] || return 0
    case "${TERM:-dumb}" in dumb|'') return 0 ;; esac
    command -v tput >/dev/null 2>&1 || return 0
    tput sgr0 >/dev/null 2>&1 && tput bold >/dev/null 2>&1 && TUI_ANSI=1
    return 0
}
_t() { [ "$TUI_ANSI" = 1 ] && tput "$@" 2>/dev/null; return 0; }

MENU_LABELS=(); MENU_HINTS=(); MENU_VERBS=(); MENU_BREAKS=()
MENU_CONFIGURED=0
MENU_SCHED_MODE=""
MENU_PENDING_COUNT="?"
MENU_LEDGER_COUNT="?"
MENU_NEXT_DIGEST="?"

# Reads live state into the MENU_* globals above -- called once when the menu
# is entered and once after each dispatched action, NEVER on every keystroke
# (it shells out to python3 via run_retry_alert --list-pending, which costs a
# real interpreter startup; doing that on every arrow-key redraw would make
# the menu feel sluggish for no benefit, since nothing changes between key
# presses that aren't a dispatch).
_menu_refresh_status() {
    MENU_CONFIGURED=0
    MENU_SCHED_MODE=""
    MENU_PENDING_COUNT="?"
    MENU_LEDGER_COUNT="?"
    MENU_NEXT_DIGEST="?"

    if [ -f "$INSTALL_DIR/config.ini" ] && [ -f "$INSTALL_DIR/secrets.env" ]; then
        MENU_CONFIGURED=1
    fi

    if have crontab; then
        local ct dline
        ct="$(crontab -l 2>/dev/null || true)"
        if printf '%s\n' "$ct" | grep -qxF "$MARK_OPEN"; then
            # Read --dry-run off our own command line only, not the whole
            # crontab -- an unrelated job carrying --dry-run must not make a
            # LIVE schedule read as dry-run (the dangerous direction of a
            # mislabel).
            dline="$(printf '%s\n' "$ct" | grep 'run_retry_alert\.py' | head -n 1)"
            case "$dline" in
                *--dry-run*) MENU_SCHED_MODE="dry-run" ;;
                *)           MENU_SCHED_MODE="live" ;;
            esac
        fi
    fi

    if [ "$MENU_CONFIGURED" -eq 1 ]; then
        local out
        out="$(run_retry_alert --list-pending 2>/dev/null)"
        MENU_PENDING_COUNT="$(printf '%s\n' "$out" | sed -n 's/^Pending retries: //p')"
        MENU_LEDGER_COUNT="$(printf '%s\n' "$out" | sed -n 's/^Digest ledger: \([0-9]*\).*/\1/p')"
        MENU_NEXT_DIGEST="$(printf '%s\n' "$out" | sed -n 's/^Next digest: //p')"
        [ -n "$MENU_PENDING_COUNT" ] || MENU_PENDING_COUNT="?"
        [ -n "$MENU_LEDGER_COUNT" ] || MENU_LEDGER_COUNT="?"
        [ -n "$MENU_NEXT_DIGEST" ] || MENU_NEXT_DIGEST="?"
    fi
}

# The menu adapts to how far along the install is, rather than always showing
# every verb -- a freshly-unpacked install sees four items, not thirteen.
# MENU_BREAKS records the index of each item that wants a blank line drawn
# before it, purely cosmetic grouping with no effect on numbering/selection.
_menu_add() { MENU_LABELS+=("$1"); MENU_HINTS+=("$2"); MENU_VERBS+=("$3"); }
_menu_break() { MENU_BREAKS+=("${#MENU_LABELS[@]}"); }

_menu_build() {
    MENU_LABELS=(); MENU_HINTS=(); MENU_VERBS=(); MENU_BREAKS=()

    if [ "$MENU_CONFIGURED" -eq 0 ]; then
        _menu_add "Configure"  "connection, device filter, and mail settings -- start here" configure
        _menu_add "Full setup" "configure, verify, then schedule in one pass"                install
        _menu_break
        _menu_add "Status"     "full configuration and schedule detail"                      status
        _menu_add "Update"     "fetch and install the latest code from GitHub"               update
        _menu_add "Uninstall"  "remove the schedule and, optionally, the files"               uninstall
        return 0
    fi

    _menu_add "Configure"        "connection, device filter, and mail settings"  configure
    _menu_add "Edit config"      "open config.ini in \$EDITOR, re-check on save" edit-config
    _menu_break
    _menu_add "Verify"           "read-only checks; dry-run scan, sends nothing" verify
    _menu_add "Pending/ledger"   "what's awaiting retry or the next digest"      list-pending
    _menu_add "Activity"         "recent cron.log, retry_alert.log, mail queue" activity
    _menu_break
    _menu_add "Run now"          "one real cycle off-schedule (asks first)"      run-now
    _menu_add "Send digest"      "force-send the ledger now, reset the timer"    send-digest
    _menu_break
    if [ -z "$MENU_SCHED_MODE" ]; then
        _menu_add "Schedule"     "install the cron entry (starts in --dry-run)"  schedule
    else
        _menu_add "Schedule"     "currently $MENU_SCHED_MODE -- reinstall or promote" schedule
        _menu_add "Unschedule"   "remove the cron entry, leaving files alone"    unschedule
    fi
    _menu_break
    _menu_add "Status"      "full configuration and schedule detail"         status
    _menu_add "Update"      "fetch and install the latest code from GitHub"  update
    _menu_add "Full setup"  "configure, verify, then schedule in one pass"   install
    _menu_add "Uninstall"   "remove the schedule and, optionally, the files" uninstall
}

_menu_is_break() {
    local i="$1" b
    for b in "${MENU_BREAKS[@]:-}"; do [ "$b" = "$i" ] && return 0; done
    return 1
}

# The index of VERB in the current MENU_VERBS, or 0 if it isn't there (e.g.
# the item it names vanished from a rebuild, such as Unschedule after
# unscheduling). Read-only -- safe to call via command substitution.
_menu_default_index() {
    local want="$1" i n="${#MENU_VERBS[@]}"
    for ((i = 0; i < n; i++)); do
        [ "${MENU_VERBS[$i]}" = "$want" ] && { printf '%d' "$i"; return 0; }
    done
    printf '0'
}

# Where the cursor should land on a fresh menu, or whenever the item the user
# was just on no longer exists: the single most useful next step given where
# the install actually stands right now, not just "always item 1".
_menu_recommended_verb() {
    if [ "$MENU_CONFIGURED" -eq 0 ]; then
        printf 'configure'
    elif [ -z "$MENU_SCHED_MODE" ]; then
        printf 'schedule'
    elif [ "$MENU_SCHED_MODE" = "dry-run" ]; then
        printf 'schedule'
    else
        printf 'status'
    fi
}

# Call ONLY after _menu_refresh_status and _menu_build have already run in
# the CALLER's own shell -- this itself runs via command substitution, a
# subshell, so anything it tried to write to MENU_* would vanish when that
# subshell exits. It only ever reads them, which is safe.
_menu_reselect() {
    local last_verb="$1" idx
    idx="$(_menu_default_index "$last_verb")"
    if [ "${MENU_VERBS[$idx]:-}" != "$last_verb" ]; then
        idx="$(_menu_default_index "$(_menu_recommended_verb)")"
    fi
    printf '%d' "$idx"
}

_menu_dashboard() {
    if [ "$MENU_CONFIGURED" -eq 0 ]; then
        printf '  not configured yet -- Configure or Full setup to begin\n'
        return 0
    fi
    local sched
    if [ -z "$MENU_SCHED_MODE" ]; then sched="not scheduled"; else sched="scheduled ($MENU_SCHED_MODE)"; fi
    printf '  configured  ·  %s\n' "$sched"
    printf '  pending: %s   ledger: %s event(s)   next digest: %s\n' \
        "$MENU_PENDING_COUNT" "$MENU_LEDGER_COUNT" "$MENU_NEXT_DIGEST"
}

_menu_draw() {
    local sel="$1" n="${#MENU_LABELS[@]}" i label hint
    _t clear
    _t civis
    _t bold; _t setaf 6
    printf '  FireMon Retrieval Retry Alert\n'
    _t sgr0
    printf '  %s\n\n' "$INSTALL_DIR"
    _t setaf 4
    _menu_dashboard
    _t sgr0
    printf '\n'
    for ((i = 0; i < n; i++)); do
        _menu_is_break "$i" && printf '\n'
        label="${MENU_LABELS[$i]}"; hint="${MENU_HINTS[$i]}"
        if [ "$i" = "$sel" ]; then
            # A visible caret AND reverse video: the caret shows the selection
            # even where colour does not (a very plain terminal, a screenshot).
            _t bold; _t rev
            printf '> %2d) %-15s %-48s' "$((i + 1))" "$label" "$hint"
            _t sgr0; printf '\n'
        else
            printf '  %2d) %-15s %s\n' "$((i + 1))" "$label" "$hint"
        fi
    done
    printf '\n'
    _t setaf 4
    printf '  Up/Down or a number to choose  ·  Enter to run  ·  q to quit\n'
    _t sgr0
}

# Read one keystroke, echo a normalised token: UP DOWN ENTER QUIT, a digit, or
# '' (ignore). Arrow keys arrive as ESC [ A etc.; a tiny timeout after ESC
# tells a real arrow from a lone Escape.
_menu_read() {
    local k seq
    IFS= read -rsn1 k </dev/tty 2>/dev/null || { printf 'QUIT'; return; }
    case "$k" in
        $'\e')
            # Read the whole burst of escape bytes in ONE read -- byte-by-byte
            # reading flips the tty in and out of raw mode between calls and
            # loses bytes, which breaks arrows. Read up to 32 bytes, act on the
            # first sequence, discard the rest.
            seq=''
            read -rsn32 -t 0.06 seq </dev/tty 2>/dev/null || true
            case "$seq" in
                '[A'*|'OA'*)           printf 'UP' ;;
                '[B'*|'OB'*)           printf 'DOWN' ;;
                '['*';'*A*|'O'*';'*A*) printf 'UP' ;;    # modified up, e.g. [1;5A
                '['*';'*B*|'O'*';'*B*) printf 'DOWN' ;;  # modified down
                *)                     printf '' ;;      # function key, lone ESC, etc.
            esac ;;
        ''|$'\n'|$'\r') printf 'ENTER' ;;
        k|K) printf 'UP' ;;
        j|J) printf 'DOWN' ;;
        q|Q) printf 'QUIT' ;;
        [0-9]) printf '%s' "$k" ;;
        *) printf '' ;;
    esac
}

_menu_cleanup() { _t cnorm; _t sgr0; stty echo </dev/tty 2>/dev/null || true; }

# Run one chosen verb as a child process, then wait for Enter. A child fully
# isolates the action -- its exits, traps and the fatal() abort signal target
# the child's own PID, never the menu's. Does NOT refresh MENU_* itself --
# the caller does that, since this runs in the same shell either way and
# there is no subshell hazard here, but keeping the refresh at the call site
# keeps this function's one job to just "run it."
_menu_dispatch() {
    local i="$1" verb extra_args=()
    verb="${MENU_VERBS[$i]}"
    _menu_cleanup
    _t clear
    _t bold; printf '=== %s ===\n\n' "${MENU_LABELS[$i]}"; _t sgr0

    # The single most common post-dry-run action, offered right where you'd
    # look for it instead of requiring you to already know the --live flag.
    if [ "$verb" = "schedule" ] && [ "$MENU_SCHED_MODE" = "dry-run" ]; then
        if confirm "Already scheduled in --dry-run. Promote to --live now?"; then
            extra_args=(--live)
        fi
    fi

    bash "$SCRIPT_PATH" --dir "$INSTALL_DIR" "$verb" "${extra_args[@]}"
    printf '\n'
    _t setaf 4; printf 'Press Enter to return to the menu.'; _t sgr0
    IFS= read -r _ </dev/tty 2>/dev/null || true
}

# Plain numbered fallback for a dumb TERM. Still needs a tty to prompt; with no
# tty at all there is nobody to answer, so print the help instead.
_menu_plain() {
    if ! { [ -t 0 ] && [ -t 1 ]; }; then
        usage
        return 0
    fi
    _menu_refresh_status
    _menu_build
    local n choice i chosen_verb idx
    while true; do
        n="${#MENU_LABELS[@]}"
        printf '\nFireMon Retrieval Retry Alert\n'
        printf '  %s\n' "$INSTALL_DIR"
        _menu_dashboard
        printf '\n'
        for ((i = 0; i < n; i++)); do
            _menu_is_break "$i" && printf '\n'
            printf '  %2d) %-15s %s\n' "$((i + 1))" "${MENU_LABELS[$i]}" "${MENU_HINTS[$i]}"
        done
        printf '  q) Quit\n'
        read -r -p 'Choose: ' choice </dev/tty || break
        case "$choice" in
            q|Q|'') break ;;
            *[!0-9]*) printf 'Enter a number, or q.\n' ;;
            *)
                idx=$((10#$choice - 1))
                if [ "$idx" -ge 0 ] && [ "$idx" -lt "$n" ]; then
                    chosen_verb="${MENU_VERBS[$idx]}"
                    bash "$SCRIPT_PATH" --dir "$INSTALL_DIR" "$chosen_verb"
                    printf '\nPress Enter to continue.'
                    IFS= read -r _ </dev/tty 2>/dev/null || break
                    _menu_refresh_status
                    _menu_build
                else
                    printf 'Choose 1-%d, or q.\n' "$n"
                fi ;;
        esac
    done
}

do_menu() {
    tui_caps
    if [ "$TUI_ANSI" != 1 ]; then
        _menu_plain
        return 0
    fi
    _menu_refresh_status
    _menu_build
    local sel key idx n chosen_verb
    sel="$(_menu_default_index "$(_menu_recommended_verb)")"
    # Restore the cursor and echo on every way out. INT/HUP/QUIT each exit --
    # without the explicit exit, a bash INT handler that only cleans up would
    # RESUME the loop, so Ctrl-C would redraw instead of quitting. EXIT is the
    # backstop for a normal return. These do NOT touch the TERM/USR1 traps
    # fatal() relies on: the menu never calls fatal() (its actions do, each in
    # its own child process).
    trap '_menu_cleanup' EXIT
    trap '_menu_cleanup; exit 130' INT
    trap '_menu_cleanup; exit 129' HUP
    trap '_menu_cleanup; exit 131' QUIT
    while true; do
        n="${#MENU_LABELS[@]}"
        _menu_draw "$sel"
        key="$(_menu_read)"
        case "$key" in
            UP)    sel=$(( (sel - 1 + n) % n )) ;;
            DOWN)  sel=$(( (sel + 1) % n )) ;;
            ENTER)
                chosen_verb="${MENU_VERBS[$sel]}"
                _menu_dispatch "$sel"
                # Refresh directly in THIS shell (not via command
                # substitution -- see _menu_reselect's own note) so a change
                # the action just made (configured, scheduled, ...) is what
                # the very next redraw shows, not what was true before it ran.
                _menu_refresh_status
                _menu_build
                sel="$(_menu_reselect "$chosen_verb")"
                ;;
            QUIT)  break ;;
            [0-9])
                idx=$((key - 1))
                if [ "$idx" -ge 0 ] && [ "$idx" -lt "$n" ]; then
                    sel="$idx"
                    chosen_verb="${MENU_VERBS[$sel]}"
                    _menu_dispatch "$sel"
                    _menu_refresh_status
                    _menu_build
                    sel="$(_menu_reselect "$chosen_verb")"
                fi ;;
        esac
    done
    _menu_cleanup
    trap - EXIT INT HUP QUIT
    _t clear
}

# --------------------------------------------------------------- main -------

main() {
    # Verb names are matched as exact tokens anywhere in the argument list
    # (not just first), so `bash setup.sh --dir X configure` and
    # `bash setup.sh configure --dir X` both work -- the menu's own dispatch
    # puts --dir before the verb.
    while [ "$#" -gt 0 ]; do
        case "$1" in
            configure|edit-config|verify|list-pending|activity|run-now|send-digest|schedule|unschedule|status|restore-crontab|update|uninstall|install|menu|help)
                if [ -z "$VERB" ]; then VERB="$1"; else RESTORE_FILE="$1"; fi ;;
            --dir) INSTALL_DIR="$(cd "$2" 2>/dev/null && pwd)" || die "no such directory: $2"; shift ;;
            --from) UPDATE_FROM="$2"; shift ;;
            --check) OPT_CHECK=1 ;;
            --at) CRON_SPEC="$2"; shift ;;
            --live) OPT_LIVE=1 ;;
            --no-logrotate) OPT_LOGROTATE=0 ;;
            --adopt) OPT_ADOPT=1 ;;
            --yes) OPT_YES=1 ;;
            --restore-file) RESTORE_FILE="$2"; shift ;;
            -h|--help) VERB="help" ;;
            -*) die "unknown option: $1. Try: bash setup.sh help" ;;
            *) RESTORE_FILE="$1" ;;
        esac
        shift
    done

    # No verb -> the menu, the front door. An explicit verb still runs
    # directly, so every scripted `bash setup.sh <verb> [flags]` invocation
    # works exactly as before.
    VERB="${VERB:-menu}"

    validate_cron_spec "$CRON_SPEC"

    case "$VERB" in
        help)            usage ;;
        menu)            do_menu ;;
        configure)       do_configure ;;
        edit-config)     do_edit_config ;;
        verify)          do_verify ;;
        list-pending)    do_list_pending ;;
        activity)        do_activity ;;
        run-now)         do_run_now ;;
        send-digest)     do_send_digest ;;
        schedule)        do_schedule ;;
        unschedule)      do_unschedule ;;
        status)          do_status ;;
        restore-crontab) do_restore_crontab ;;
        update)          do_update ;;
        uninstall)       do_uninstall ;;
        install)
            do_configure
            do_verify
            head2 "Schedule it?"
            if confirm "Install the cron entry now ($CRON_SPEC)?"; then
                do_schedule
            else
                skip "not scheduled. When you are ready: bash setup.sh schedule"
            fi
            head2 "Done"
            info "status:   bash setup.sh status"
            info "re-check: bash setup.sh verify"
            ;;
        *) usage; die "unknown verb: $VERB" ;;
    esac
}

# Lets a test harness `source` this file to exercise its internal functions
# (patch_config, _existing_or, crontab surgery, ...) without triggering a real
# run. Nothing below this line executes when sourced with the marker set.
if [ "${FMRETRY_SETUP_LIB-}" = "1" ]; then
    return 0
fi

main "$@"
