#!/usr/bin/env bash
# Keep a Yeastar TG gateway's clock close to this host's (NTP-synced) clock.
# Reads the gateway's time from its web UI's HTTP Date header and, when it is off
# by more than MAX_DRIFT_MINUTES, sets it through the web UI's Date and Time page.
# Setting the time needs manual mode; NTP is then switched back on if it was on.
# See README.md for configuration.
#
# Usage: tg200_clock_watchdog.sh [--dry-run]
#   --dry-run  log in and show what would be posted, even if the clock is fine;
#              change nothing.
set -euo pipefail
umask 077
export LC_ALL=C

log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$*" >&2
}

die() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

DRY_RUN=0
case "${1:-}" in
    "") ;;
    --dry-run) DRY_RUN=1 ;;
    *) die "usage: $0 [--dry-run]" ;;
esac

read_required_file() {
    local path="$1"
    [[ -r "$path" ]] || die "required config file not readable: $path"
    <"$path" tr -d '\r'
}

# Read KEY=value lines from an optional config file. A setting already given in
# the environment as YEASTAR_<KEY> takes precedence over the file. The file may
# be shared with tg200_sim_status.sh, so its settings are skipped silently.
load_optional_config() {
    local path="$1" key value env
    [[ -r "$path" ]] || return 0
    while IFS='=' read -r key value; do
        key="${key//[[:space:]]/}"
        [[ -z "$key" || "$key" == \#* ]] && continue
        case "$key" in
            NOTIFY_HOOK|REPEAT_HOURS|MAX_DRIFT_MINUTES|LAN_SUBNET)
                env="YEASTAR_${key}"
                [[ -n "${!env+x}" ]] || printf -v "$env" '%s' "$value"
                ;;
            SIP_FAIL_RUNS) ;;
            *) log "Ignoring unknown setting in ${path}: ${key}" ;;
        esac
    done < <(tr -d '\r' < "$path")
}

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/yeastar"
ID="${YEASTAR_ID:-tg200}"
load_optional_config "${CONFIG_DIR}/${ID}.conf"

TG_USER="$(read_required_file "${CONFIG_DIR}/${ID}.user")"
TG_PASS="$(read_required_file "${CONFIG_DIR}/${ID}.pass")"

# Command called with the message as its argument; empty disables notifications.
NOTIFY_HOOK="${YEASTAR_NOTIFY_HOOK-/usr/local/bin/pushover.sh}"
# Repeat a fault notification this often while the fault lasts; 0 disables repeats.
REPEAT_HOURS="${YEASTAR_REPEAT_HOURS:-6}"
# Set the clock when it is off by more than this.
MAX_DRIFT_MINUTES="${YEASTAR_MAX_DRIFT_MINUTES:-10}"
# Optional first three octets of a /24, e.g. 192.168.1: search it for the gateway
# when the configured host doesn't answer as one (its DHCP address can change).
LAN_SUBNET="${YEASTAR_LAN_SUBNET:-}"

[[ "$REPEAT_HOURS" =~ ^[0-9]+$ ]] || die "REPEAT_HOURS must be a whole number: $REPEAT_HOURS"
[[ "$MAX_DRIFT_MINUTES" =~ ^[1-9][0-9]*$ ]] || die "MAX_DRIFT_MINUTES must be at least 1: $MAX_DRIFT_MINUTES"
[[ -z "$LAN_SUBNET" || "$LAN_SUBNET" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] ||
    die "LAN_SUBNET must be the first three octets of a /24, e.g. 192.168.1: $LAN_SUBNET"
# The host file is optional when LAN_SUBNET lets the script find the gateway.
if [[ -n "$LAN_SUBNET" && ! -e "${CONFIG_DIR}/${ID}.host" ]]; then
    TG_HOST=""
else
    TG_HOST="$(read_required_file "${CONFIG_DIR}/${ID}.host")"
fi
REPEAT_SECS=$((REPEAT_HOURS * 3600))
MAX_DRIFT_SECS=$((MAX_DRIFT_MINUTES * 60))

FOUND_HOST_FILE="${CONFIG_DIR}/${ID}.clock.foundhost"
UNREACHABLE_MARKER="${CONFIG_DIR}/${ID}.clock.unreachable"
FAILED_MARKER="${CONFIG_DIR}/${ID}.clock.failed"
UA='Mozilla/5.0 (X11; Linux x86_64; rv:148.0) Gecko/20100101 Firefox/148.0'
TMP_FILES=()
BASE_URL=""
COOKIE=""

notify() {
    if [ -n "$NOTIFY_HOOK" ]
    then
        "$NOTIFY_HOOK" "$*" >/dev/null
    fi
}

# A fault is tracked by a marker file that exists while the fault lasts. Its
# first line holds the start time; each repeat notification appends a line,
# which also resets the file's mtime, the basis for the next repeat.
fault_raise() {
    local marker="$1" message="$2" repeat_message="$3"
    if [[ ! -f "$marker" ]]; then
        date >> "$marker"
        notify "$message"
    elif (( REPEAT_SECS > 0 )) &&
         (( $(date +%s) - $(stat -c %Y "$marker") >= REPEAT_SECS )); then
        date >> "$marker"
        notify "${repeat_message} (since $(head -n 1 "$marker"))"
    fi
}

fault_clear() {
    local marker="$1" message="$2"
    if [[ -f "$marker" ]]; then
        rm -f "$marker"
        notify "$message"
    fi
}

cleanup() {
    if ((${#TMP_FILES[@]})); then
        rm -f "${TMP_FILES[@]}"
    fi
}
trap cleanup EXIT

mktemp_tracked() {
    local f
    f="$(mktemp)"
    TMP_FILES+=("$f")
    printf '%s\n' "$f"
}

urlencode() {
    local s="$1"
    local i c out=""
    for ((i=0; i<${#s}; i++)); do
        c="${s:i:1}"
        case "$c" in
            [a-zA-Z0-9.~_-]) out+="$c" ;;
            *) printf -v out '%s%%%02X' "$out" "'$c" ;;
        esac
    done
    printf '%s' "$out"
}

# Form encoding as a browser does it: spaces become '+'.
formencode() {
    local s
    s="$(urlencode "$1")"
    printf '%s' "${s//%20/+}"
}

build_js_password() {
    # The yeastar javascript obfuscates the password...
    local md5hex b64 out="" i ch ord
    md5hex="$(printf '%s' "$TG_PASS" | md5sum | awk '{print $1}')"
    b64="$(printf '%s' "$md5hex" | base64 | tr -d '\n')"

    for ((i=0; i<${#b64}; i++)); do
        ch="${b64:i:1}"
        printf -v ord '%d' "'$ch"
        printf -v ch '\\%03o' "$((ord + 2))"
        out+="$ch"
    done

    printf '%b' "$out"
}
ENCODED_PASS="$(urlencode "$(build_js_password)")"

# "2h 3m 4s", "-9s"
human_secs() {
    local s="$1" sign="" out=""
    (( s < 0 )) && { sign="-"; s=$(( -s )); }
    (( s >= 86400 )) && out+="$((s / 86400))d "
    (( s >= 3600 )) && out+="$((s % 86400 / 3600))h "
    (( s >= 60 )) && out+="$((s % 3600 / 60))m "
    out+="$((s % 60))s"
    printf '%s%s' "$sign" "$out"
}

# Print the gateway's clock as epoch seconds, read from the Date header of its
# login page, which needs no login. Fails if the host isn't a Yeastar web UI.
device_epoch() {
    local host="$1" headers body date_hdr
    headers="$(mktemp_tracked)"
    body="$(mktemp_tracked)"
    curl -sS -m 5 -A "$UA" -D "$headers" -o "$body" "http://${host}/" 2>/dev/null || return 1
    grep -qi '^Server: Boa' "$headers" || return 1
    grep -q -e 'yeastar' -e 'mypbx.js' "$body" || return 1
    date_hdr="$(tr -d '\r' < "$headers" | sed -n 's/^[Dd]ate: //p' | head -n 1)"
    [[ -n "$date_hdr" ]] || return 1
    date -u -d "$date_hdr" +%s
}

# Probe every address in LAN_SUBNET; print the first one that answers as a
# Yeastar web UI.
discover_host() {
    seq 1 254 | sed "s/^/${LAN_SUBNET}./" |
        xargs -P 64 -I{} sh -c '
            h=$(curl -s -m 2 -D - -o /dev/null "http://$1/" 2>/dev/null) || exit 0
            printf "%s" "$h" | grep -qi "^Server: Boa" || exit 0
            curl -s -m 3 "http://$1/" 2>/dev/null | grep -q -e yeastar -e mypbx.js && echo "$1"
        ' _ {} | head -n 1
}

remember_host() {
    local host="$1"
    BASE_URL="http://${host}"
    if [[ "$(cat "$FOUND_HOST_FILE" 2>/dev/null)" != "$host" ]]; then
        printf '%s\n' "$host" > "$FOUND_HOST_FILE"
    fi
}

# Find a host that answers as the gateway: the one that answered last time, the
# configured one, then (if LAN_SUBNET is set) a sweep. The host that answers is
# remembered for the next run. Sets BASE_URL.
find_gateway() {
    local candidates=() host found last=""
    [[ -r "$FOUND_HOST_FILE" ]] && last="$(tr -d '\r\n' < "$FOUND_HOST_FILE")"
    candidates=("$last")
    [[ "$TG_HOST" != "$last" ]] && candidates+=("$TG_HOST")
    for host in "${candidates[@]}"; do
        [[ -n "$host" ]] || continue
        if device_epoch "$host" >/dev/null; then
            remember_host "$host"
            return 0
        fi
    done
    if [[ -n "$LAN_SUBNET" ]]; then
        log "No Yeastar web UI at ${last:-${TG_HOST:-(no host yet)}}; searching ${LAN_SUBNET}.0/24"
        found="$(discover_host || true)"
        if [[ -n "$found" ]]; then
            log "Found the web UI at ${found}"
            remember_host "$found"
            return 0
        fi
    fi
    return 1
}

login() {
    local headers_file http_code
    headers_file="$(mktemp_tracked)"
    http_code="$(
        curl -sS -m 10 "${BASE_URL}/cgi/WebCGI?1000" \
             -X POST \
             -D "$headers_file" \
             -o /dev/null \
             -w '%{http_code}' \
             -A "$UA" \
             -H 'Content-Type: application/x-www-form-urlencoded' \
             -H "Origin: ${BASE_URL}" \
             -H "Referer: ${BASE_URL}/" \
             -H "Cookie: language=en; loginname=${TG_USER}; password=${ENCODED_PASS}; defaultpwd=bbb; OsVer=xxxx" \
             --data-raw "username=$(urlencode "$TG_USER")&secret=${ENCODED_PASS}" || echo 500
    )"
    COOKIE="language=en; loginname=${TG_USER}; password=${ENCODED_PASS}; OsVer=91.3.0.23; current=system; curUrl=1251; defaultpwd=; Series=; Product=TG100; TabIndex=0; TabIndexwithback=0"
    [[ "$http_code" == "200" ]] && grep -qi '^Set-Cookie:' "$headers_file"
}

# Read the Date and Time page. Sets TZ_NAME, DST, NTP_SERVER, NTP_ON and
# TZ_OFFSET_SECS: the UTC offset the gateway applies right now, DST included,
# measured as its local time minus its own HTTP Date (GMT) in the same response.
# This uses the gateway's own timezone rules, which can differ from this host's.
read_settings() {
    local headers body comm fields local_str date_hdr local_epoch gmt_epoch diff
    headers="$(mktemp_tracked)"
    body="$(mktemp_tracked)"
    curl -sS -m 10 "${BASE_URL}/cgi/WebCGI?1251" \
         -D "$headers" -o "$body" \
         -A "$UA" \
         -H "Referer: ${BASE_URL}/cgi/WebCGI?1000" \
         -H "Cookie: ${COOKIE}" || return 1

    # e.g. &DateAndTime:Sun Nov 22 23:35:25 2026,UTC-5 America/New_York,yes,yes,pool.ntp.org;
    comm="$(sed -n 's/.*id="MyPBX_COMM" value="\([^"]*\)".*/\1/p' "$body")"
    fields="$(sed -n 's/.*DateAndTime:\([^;&]*\).*/\1/p' <<< "$comm")"
    [[ -n "$fields" ]] || { log "No DateAndTime settings on the Date and Time page"; return 1; }
    IFS=',' read -r local_str TZ_NAME DST NTP_ON NTP_SERVER <<< "$fields"
    [[ -n "$local_str" && -n "$TZ_NAME" && -n "$DST" ]] ||
        { log "Unexpected DateAndTime settings: ${fields}"; return 1; }

    date_hdr="$(tr -d '\r' < "$headers" | sed -n 's/^[Dd]ate: //p' | head -n 1)"
    local_epoch="$(date -u -d "$local_str" +%s)" || return 1
    gmt_epoch="$(date -u -d "$date_hdr" +%s)" || return 1
    diff=$(( local_epoch - gmt_epoch ))
    # Round to 15 minutes; the shift keeps the division on positive numbers.
    TZ_OFFSET_SECS=$(( (diff + 450 + 86400) / 900 * 900 - 86400 ))
    log "Settings: local time ${local_str}, timezone ${TZ_NAME}, DST ${DST}, NTP ${NTP_ON} (${NTP_SERVER}); offset $(human_secs "$TZ_OFFSET_SECS")"
}

post_form() {
    local code="$1" data="$2"
    if (( DRY_RUN )); then
        log "Dry run, would POST ?${code}: ${data}"
        return 0
    fi
    log "POST ?${code}: ${data}"
    curl -sS -m 30 "${BASE_URL}/cgi/WebCGI?${code}" \
         -X POST -o /dev/null \
         -A "$UA" \
         -H 'Content-Type: application/x-www-form-urlencoded' \
         -H "Origin: ${BASE_URL}" \
         -H "Referer: ${BASE_URL}/cgi/WebCGI?1251" \
         -H "Cookie: ${COOKIE}" \
         --data-raw "$data"
}

# Set the gateway's clock to now, as its local time. The form only takes a time
# in manual mode, so post that first; then, if NTP was on, switch it back on,
# as the web UI does (empty time fields). The time is set to the nearest
# minute; the form has no seconds. Finally "Apply Changes" saves the settings.
set_clock() {
    local t common data
    t=$(( $(date +%s) + TZ_OFFSET_SECS + 30 ))
    common="&NTPserver=$(formencode "$NTP_SERVER")&timezone=$(formencode "$TZ_NAME")"

    # The gateway converts naively: hour = hod + (PM ? 12 : 0). So "12 AM" is
    # noon (seen on firmware 91.3.0.23), and midnight must be sent as "00 AM".
    local h hod apm
    h=$(( 10#$(date -u -d "@$t" +%H) ))
    if (( h < 12 )); then apm=AM; else apm=PM; h=$(( h - 12 )); fi
    if [[ $apm == PM && $h -eq 0 ]]; then apm=AM; h=12; fi
    printf -v hod '%02d' "$h"

    data="checkdst=$(formencode "$DST")&enablentp=no"
    data+="&timedate=$(formencode "$(date -u -d "@$t" '+%-d %b %Y')")"
    data+="&hod=${hod}&minute=$(date -u -d "@$t" +%M)"
    data+="&timeapm=${apm}${common}"
    post_form 1252 "$data" || return 1

    if [[ "$NTP_ON" == "yes" ]]; then
        post_form 1252 "checkdst=$(formencode "$DST")&enablentp=yes&timedate=&hod=&minute=&timeapm=AM${common}" ||
            return 1
    fi
    post_form 1606 "username=$(urlencode "$TG_USER")"
}

drift_now() {
    local e
    e="$(device_epoch "${BASE_URL#http://}")" || return 1
    printf '%s\n' $(( e - $(date +%s) ))
}

main() {
    local drift first_drift attempt

    if ! find_gateway; then
        log "No Yeastar web UI found${TG_HOST:+ at ${TG_HOST}}${LAN_SUBNET:+ or anywhere in ${LAN_SUBNET}.0/24}"
        fault_raise "$UNREACHABLE_MARKER" \
            "${ID} clock watchdog can't reach the web UI" \
            "${ID} clock watchdog still can't reach the web UI"
        exit 1
    fi
    fault_clear "$UNREACHABLE_MARKER" "${ID} clock watchdog reaches the web UI again"

    drift="$(drift_now)" || die "Lost the web UI at ${BASE_URL}"
    first_drift="$drift"
    log "${BASE_URL#http://}: clock offset $(human_secs "$drift")"
    if (( drift >= -MAX_DRIFT_SECS && drift <= MAX_DRIFT_SECS )) && (( ! DRY_RUN )); then
        fault_clear "$FAILED_MARKER" "${ID} clock is right again (offset $(human_secs "$drift"))"
        return 0
    fi

    login || {
        log "Login failed"
        fault_raise "$FAILED_MARKER" \
            "${ID} clock is off by $(human_secs "$drift"), and login failed" \
            "${ID} clock still off, login fails"
        exit 1
    }
    log "Login successful"

    # A second try covers a DST change between the wrong date and the right one:
    # the offset measured at the wrong date can be an hour off.
    for attempt in 1 2; do
        read_settings || { log "Could not read the Date and Time settings"; break; }
        set_clock || { log "Could not post the new time"; break; }
        (( DRY_RUN )) && return 0
        drift="$(drift_now)" || break
        log "After setting (try ${attempt}): clock offset $(human_secs "$drift")"
        if (( drift >= -MAX_DRIFT_SECS && drift <= MAX_DRIFT_SECS )); then
            rm -f "$FAILED_MARKER"
            notify "${ID} clock was off by $(human_secs "$first_drift"); set it (now $(human_secs "$drift"), timezone ${TZ_NAME})"
            return 0
        fi
    done

    fault_raise "$FAILED_MARKER" \
        "${ID} clock is off by $(human_secs "$first_drift") and setting it failed" \
        "${ID} clock still off, setting it fails"
    exit 1
}

main
