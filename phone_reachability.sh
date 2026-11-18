#!/usr/bin/env bash
# Check that SIP phones are reachable through Asterisk (PJSIP) and, optionally,
# by ping. Notify when a phone stops being reachable, repeat while it stays
# down, and notify once when it recovers. See README.md for configuration.
#
# Usage: phone_reachability.sh <config-file>...
#   One config file per phone. Each is checked independently, with its own
#   state and lock, so one broken config does not stop the others.
set -euo pipefail
umask 077

log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$*" >&2
}

die() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

set_defaults() {
    NAME="$(basename "$CONFIG_FILE" .conf)"
    AOR=""
    CONTACT_HOST=""
    PING_HOST=""
    PING_COUNT=3
    PING_TIMEOUT=5
    FAIL_RUNS=2
    REPEAT_HOURS=6
    NOTIFY_HOOK="/usr/local/bin/pushover.sh"
    ASTERISK="/usr/sbin/asterisk"
    if (( EUID == 0 )); then
        STATE_DIR="/var/lib/phone-reachability"
    else
        STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/phone-reachability"
    fi
}

# Read KEY=value lines; the file is parsed, not sourced.
load_config() {
    local key value
    while IFS='=' read -r key value; do
        key="${key//[[:space:]]/}"
        [[ -z "$key" || "$key" == \#* ]] && continue
        # Trim surrounding whitespace and one pair of quotes.
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"
        if [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]]; then
            value="${BASH_REMATCH[1]}"
        fi
        case "$key" in
            NAME|AOR|CONTACT_HOST|PING_HOST|PING_COUNT|PING_TIMEOUT|FAIL_RUNS|REPEAT_HOURS|NOTIFY_HOOK|ASTERISK|STATE_DIR)
                printf -v "$key" '%s' "$value"
                ;;
            *) log "Ignoring unknown setting in ${CONFIG_FILE}: ${key}" ;;
        esac
    done < <(tr -d '\r' < "$CONFIG_FILE")
}

validate_config() {
    local setting
    [[ -n "$AOR" ]] || die "AOR is not set in $CONFIG_FILE"
    [[ "$NAME" =~ ^[A-Za-z0-9._-]+$ ]] ||
        die "NAME may only contain letters, digits, '.', '_' and '-': $NAME"
    for setting in PING_COUNT PING_TIMEOUT FAIL_RUNS; do
        [[ "${!setting}" =~ ^[1-9][0-9]*$ ]] ||
            die "$setting must be a positive whole number: ${!setting}"
    done
    [[ "$REPEAT_HOURS" =~ ^[0-9]+$ ]] ||
        die "REPEAT_HOURS must be a whole number: $REPEAT_HOURS"
}

notify() {
    if [[ -n "$NOTIFY_HOOK" ]]; then
        "$NOTIFY_HOOK" "$*" >/dev/null || log "${NAME}: notify hook failed"
    fi
}

# Print the host part of a SIP URI such as sip:1001@192.0.2.1:5060;transport=udp
uri_host() {
    local rest="${1#*:}"          # drop the scheme
    rest="${rest#*@}"             # drop the user part, if any
    if [[ "$rest" == \[* ]]; then
        rest="${rest#[}"          # IPv6 literal
        printf '%s\n' "${rest%%]*}"
    else
        printf '%s\n' "${rest%%[:;>]*}"
    fi
}

# Print the reason when the AOR has no usable contact; print nothing when it does.
check_contact() {
    local output word uri status found="" seen=""
    local -a fields

    if ! output="$("$ASTERISK" -rx "pjsip show contacts" 2>&1)"; then
        printf 'cannot query Asterisk\n'
        return
    fi

    # Lines look like:
    #   Contact:  <aor>/<uri>   <hash>   <status>   <rtt>
    # Asterisk cuts <aor>/<uri> to a fixed column width (about 46 characters),
    # so CONTACT_HOST only works if the host falls within that width.
    while read -r -a fields; do
        [[ "${fields[0]:-}" == "Contact:" && "${fields[1]:-}" == "${AOR}/"* ]] || continue
        uri="${fields[1]#"${AOR}/"}"
        if [[ -n "$CONTACT_HOST" && "$(uri_host "$uri")" != "$CONTACT_HOST" ]]; then
            seen+=" ${uri}"
            continue
        fi
        status=""
        for word in "${fields[@]:2}"; do
            case "$word" in
                Avail|Unavail|NonQual|Unknown|Created|Removed|Reachable|Unreachable)
                    status="$word"; break ;;
            esac
        done
        case "$status" in
            Avail|Reachable|NonQual)
                [[ "$status" == NonQual ]] &&
                    log "${NAME}: contact ${uri} is not qualified (no qualify_frequency when it registered); only its registration is checked"
                found=1
                break
                ;;
            *) seen+=" ${uri} (${status:-no status})" ;;
        esac
    done <<< "$output"

    [[ -n "$found" ]] && return
    if [[ -n "$seen" ]]; then
        printf 'no reachable contact for %s:%s\n' "$AOR" "$seen"
    elif [[ -n "$CONTACT_HOST" ]]; then
        printf 'not registered from %s\n' "$CONTACT_HOST"
    else
        printf 'not registered\n'
    fi
}

check_ping() {
    [[ -n "$PING_HOST" ]] || return 0
    if ! ping -c "$PING_COUNT" -W "$PING_TIMEOUT" -q "$PING_HOST" >/dev/null 2>&1; then
        printf 'no ping reply from %s\n' "$PING_HOST"
    fi
}

read_count() {
    local count=0
    if [[ -r "$COUNT_FILE" ]]; then
        count="$(tr -dc '0-9' < "$COUNT_FILE")"
        [[ -n "$count" ]] || count=0
    fi
    printf '%s\n' "$count"
}

# Check one phone. Runs in a subshell, so settings and failures stay local.
check_phone() {
    CONFIG_FILE="$1"
    [[ -r "$CONFIG_FILE" ]] || die "config file not readable: $CONFIG_FILE"
    set_defaults
    load_config
    validate_config

    local repeat_secs=$((REPEAT_HOURS * 3600))
    local reason summary count
    local -a reasons=()

    mkdir -p "$STATE_DIR"
    COUNT_FILE="${STATE_DIR}/${NAME}.failcount"
    DOWN_FILE="${STATE_DIR}/${NAME}.down"

    # One run at a time per phone; also catches two configs sharing a NAME.
    exec 9> "${STATE_DIR}/${NAME}.lock"
    if ! flock -n 9; then
        log "${NAME}: another check is still running; skipped"
        return 0
    fi

    reason="$(check_contact)"; [[ -n "$reason" ]] && reasons+=("$reason")
    reason="$(check_ping)";    [[ -n "$reason" ]] && reasons+=("$reason")

    if (( ${#reasons[@]} == 0 )); then
        log "${NAME}: reachable"
        printf '0\n' > "$COUNT_FILE"
        if [[ -f "$DOWN_FILE" ]]; then
            rm -f "$DOWN_FILE"
            notify "Phone ${NAME} is reachable again"
        fi
        return 0
    fi

    summary="${reasons[0]}"
    for reason in "${reasons[@]:1}"; do
        summary+="; ${reason}"
    done
    count=$(( $(read_count) + 1 ))
    printf '%s\n' "$count" > "$COUNT_FILE"
    log "${NAME}: check failed (${count}/${FAIL_RUNS}): ${summary}"

    (( count >= FAIL_RUNS )) || return 0

    # The down file's first line is the start time; each repeat appends a line,
    # which also resets its mtime, the basis for the next repeat.
    if [[ ! -f "$DOWN_FILE" ]]; then
        date >> "$DOWN_FILE"
        notify "Phone ${NAME} is unreachable: ${summary}"
    elif (( repeat_secs > 0 )) &&
         (( $(date +%s) - $(stat -c %Y "$DOWN_FILE") >= repeat_secs )); then
        date >> "$DOWN_FILE"
        notify "Phone ${NAME} still unreachable (since $(head -n 1 "$DOWN_FILE")): ${summary}"
    fi
}

main() {
    local config rc status=0
    (( $# >= 1 )) || die "usage: $(basename "$0") <config-file>..."
    for config in "$@"; do
        # Not "( ... ) || status=1": bash ignores set -e inside a subshell
        # whose status is tested, so errors would not stop the check.
        set +e
        ( set -e; check_phone "$config" )
        rc=$?
        set -e
        (( rc == 0 )) || status=1
    done
    return "$status"
}

main "$@"
