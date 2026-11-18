#!/usr/bin/env bash
# Monitor a Yeastar TG gateway through its web UI: SIM states, SIP trunk status,
# and reachability. Sends notifications on changes and reboots the gateway when
# its SIMs are stuck. See README.md for configuration.
set -euo pipefail
umask 077

log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$*" >&2
}

die() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

read_required_file() {
    local path="$1"
    [[ -r "$path" ]] || die "required config file not readable: $path"
    <"$path" tr -d '\r'
}

# Read KEY=value lines from an optional config file. A setting already given in
# the environment as YEASTAR_<KEY> takes precedence over the file.
load_optional_config() {
    local path="$1" key value env
    [[ -r "$path" ]] || return 0
    while IFS='=' read -r key value; do
        key="${key//[[:space:]]/}"
        [[ -z "$key" || "$key" == \#* ]] && continue
        case "$key" in
            NOTIFY_HOOK|REPEAT_HOURS|SIP_FAIL_RUNS)
                env="YEASTAR_${key}"
                [[ -n "${!env+x}" ]] || printf -v "$env" '%s' "$value"
                ;;
            *) log "Ignoring unknown setting in ${path}: ${key}" ;;
        esac
    done < <(tr -d '\r' < "$path")
}

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/yeastar"
ID="${YEASTAR_ID:-tg200}"
load_optional_config "${CONFIG_DIR}/${ID}.conf"

TG_HOST="$(read_required_file "${CONFIG_DIR}/${ID}.host")"
TG_USER="$(read_required_file "${CONFIG_DIR}/${ID}.user")"
TG_PASS="$(read_required_file "${CONFIG_DIR}/${ID}.pass")"

# Command called with the message as its argument; empty disables notifications.
NOTIFY_HOOK="${YEASTAR_NOTIFY_HOOK-/usr/local/bin/pushover.sh}"
# Repeat a fault notification this often while the fault lasts; 0 disables repeats.
REPEAT_HOURS="${YEASTAR_REPEAT_HOURS:-6}"
# Consecutive runs a SIP trunk must be down before notifying.
SIP_FAIL_RUNS="${YEASTAR_SIP_FAIL_RUNS:-2}"

[[ "$REPEAT_HOURS" =~ ^[0-9]+$ ]] || die "REPEAT_HOURS must be a whole number: $REPEAT_HOURS"
[[ "$SIP_FAIL_RUNS" =~ ^[1-9][0-9]*$ ]] || die "SIP_FAIL_RUNS must be at least 1: $SIP_FAIL_RUNS"
REPEAT_SECS=$((REPEAT_HOURS * 3600))

BASE_URL="http://${TG_HOST}"
LOGIN_URL="${BASE_URL}/cgi/WebCGI?1000"
TMP_FILES=()
STATUS_COMM=""
declare -a SIM_STATUSES=()
declare -a ERROR_STATUSES=(
    "Registering"
    "NO SIM"
)

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

is_error_status() {
    local status="$1"
    local err
    for err in "${ERROR_STATUSES[@]}"; do
        if [[ "$status" == "$err" ]]; then
            return 0
        fi
    done
    return 1
}

all_sims_idle_or_error() {
    local status
    local i

    # If there are no SIMs parsed, do not reboot.
    ((${#SIM_STATUSES[@]})) || return 1

    for i in "${!SIM_STATUSES[@]}"; do
        status="${SIM_STATUSES[$i]}"
        if [[ "$status" != "IDLE" ]] && ! is_error_status "$status"; then
            return 1
        fi
    done

    return 0
}

login() {
    local headers_file body_file http_code

    headers_file="$(mktemp_tracked)"
    body_file="$(mktemp_tracked)"
    http_code="$(
        curl -sS "$LOGIN_URL" \
             -X POST \
             -D "$headers_file" \
             -o "$body_file" \
             -w '%{http_code}' \
             -H 'User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:148.0) Gecko/20100101 Firefox/148.0' \
             -H 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8' \
             -H 'Accept-Language: en,en-US;q=0.9' \
             -H 'Accept-Encoding: gzip, deflate' \
             -H 'Content-Type: application/x-www-form-urlencoded' \
             -H "Origin: ${BASE_URL}" \
             -H 'Connection: keep-alive' \
             -H "Referer: ${BASE_URL}/" \
             -H "Cookie: language=en; loginname=${TG_USER}; password=${ENCODED_PASS}; defaultpwd=bbb; OsVer=xxxx" \
             -H 'Upgrade-Insecure-Requests: 1' \
             -H 'Priority: u=0, i' \
             -H 'Pragma: no-cache' \
             -H 'Cache-Control: no-cache' \
             --data-raw "username=$(urlencode "$TG_USER")&secret=${ENCODED_PASS}" || echo 500
    )"

    if [[ "$http_code" == "200" ]] && grep -qi '^Set-Cookie:' "$headers_file"; then
        log "Login successful"
        fault_clear "${CONFIG_DIR}/${ID}.down" "${ID} is available again"
        return 0
    else
        log "Login failed"
        fault_raise "${CONFIG_DIR}/${ID}.down" \
            "Login into ${ID} failed" "${ID} still unreachable"
        return 1
    fi
}

fetch_sims() {
    local body_file comm gsm
    local -a sim_records sim_names
    local i record old_status log_parts
    local -a fields

    # Clear the global var
    SIM_STATUSES=()

    body_file="$(mktemp_tracked)"
    curl -sS "${BASE_URL}/cgi/WebCGI?10102" \
         -o "$body_file" \
         -H 'User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:148.0) Gecko/20100101 Firefox/148.0' \
         -H 'Accept: */*' \
         -H 'Accept-Language: en,en-US;q=0.9' \
         -H 'Accept-Encoding: gzip, deflate' \
         -H 'Content-Type: application/x-www-form-urlencoded' \
         -H 'Connection: keep-alive' \
         -H "Referer: ${BASE_URL}/cgi/WebCGI?1000" \
         -H 'Pragma: no-cache' \
         -H 'Cache-Control: no-cache' \
         -H "Cookie: language=en; loginname=${TG_USER}; password=${ENCODED_PASS}; OsVer=91.3.0.23; current=; TabIndex=0; TabIndexwithback=0; curUrl=; defaultpwd=; Series=; Product=TG100"

    # Extract MyPBX_COMM value; check_sip_trunks parses it too
    comm=$(sed -n 's/.*id="MyPBX_COMM" value="\([^"]*\)".*/\1/p' "$body_file")
    STATUS_COMM="$comm"

    # Extract GsmTrunks section
    gsm=$(sed -n 's/.*GsmTrunks:\([^&]*\).*/\1/p' <<< "$comm")

    # Split on ';' into SIM records
    IFS=';' read -r -a sim_records <<< "$gsm"

    # Parse each SIM record
    for i in "${!sim_records[@]}"; do
        record=${sim_records[$i]}

        # Skip empty trailing records if present
        [[ -z "$record" ]] && continue

        IFS=',' read -r -a fields <<< "$record"

        # fields[1] = name, fields[2] = status
        sim_names[$i]=${fields[1]:-}
        SIM_STATUSES[$i]=${fields[2]:-}
    done

    # Log all SIMs
    log_parts=""
    for i in "${!sim_names[@]}"; do
        [[ -n "$log_parts" ]] && log_parts="${log_parts}; "
        log_parts+="${sim_names[$i]}: ${SIM_STATUSES[$i]}"
    done
    log "$log_parts"

    # Compare/store/notify
    for i in "${!sim_names[@]}"; do
        touch "${CONFIG_DIR}/${ID}.sim$((i+1))" # it might not yet exist
        old_status=$(cat "${CONFIG_DIR}/${ID}.sim$((i+1))")
        printf '%s\n' "${SIM_STATUSES[$i]}" > "${CONFIG_DIR}/${ID}.sim$((i+1))"

        if [[ "$old_status" != "${SIM_STATUSES[$i]}" ]]; then
            notify "${ID} SIM ${sim_names[$i]} changed from ${old_status} to ${SIM_STATUSES[$i]}"
        fi
    done
}

# The status page lists the gateway's SIP trunks with their qualify result, e.g.
#   SpTrunks:<name>,SPS,,<server>, OK (13 ms);
# "OK" means the gateway reaches the SIP server; anything else is a fault.
check_sip_trunks() {
    local sp record name status i count count_file marker
    local -a records fields

    sp=$(sed -n 's/.*SpTrunks:\([^&]*\).*/\1/p' <<< "$STATUS_COMM")
    IFS=';' read -r -a records <<< "$sp"

    for i in "${!records[@]}"; do
        record=${records[$i]}
        [[ -z "$record" ]] && continue

        IFS=',' read -r -a fields <<< "$record"
        name=${fields[0]:-trunk$((i+1))}
        status=${fields[4]:-}
        status="${status#"${status%%[![:space:]]*}"}"   # trim leading space
        log "SIP trunk ${name}: ${status:-no status}"

        count_file="${CONFIG_DIR}/${ID}.sptrunk$((i+1)).failcount"
        marker="${CONFIG_DIR}/${ID}.sptrunk$((i+1)).down"

        if [[ "$status" == OK* ]]; then
            printf '0\n' > "$count_file"
            fault_clear "$marker" "${ID} SIP trunk ${name} is OK again: ${status}"
            continue
        fi

        count=0
        if [[ -r "$count_file" ]]; then
            count="$(tr -dc '0-9' < "$count_file")"
            [[ -n "$count" ]] || count=0
        fi
        count=$((count + 1))
        printf '%s\n' "$count" > "$count_file"

        if (( count >= SIP_FAIL_RUNS )); then
            fault_raise "$marker" \
                "${ID} SIP trunk ${name} is down: ${status:-no status}" \
                "${ID} SIP trunk ${name} still down: ${status:-no status}"
        fi
    done
}

restart() {
    curl -sS "${BASE_URL}/cgi/WebCGI?1262" \
         -X POST \
         -o /dev/null \
         -H 'User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:148.0) Gecko/20100101 Firefox/148.0' \
         -H 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8' \
         -H 'Accept-Language: en,en-US;q=0.9' \
         -H 'Accept-Encoding: gzip, deflate' \
         -H 'Content-Type: application/x-www-form-urlencoded' \
         -H "Origin: ${BASE_URL}" \
         -H 'Connection: keep-alive' \
         -H "Referer: ${BASE_URL}/cgi/WebCGI?1263" \
         -H "Cookie: language=en; loginname=${TG_USER}; password=${ENCODED_PASS}; OsVer=91.3.0.23; current=system; curUrl=122111; defaultpwd=; Series=; Product=TG100; TabIndex=0; TabIndexwithback=0" \
         -H 'Upgrade-Insecure-Requests: 1' \
         -H 'Priority: u=4' \
         -H 'Pragma: no-cache' \
         -H 'Cache-Control: no-cache' \
         --data-raw 'rebootmode=0&login=yes'
    notify "Rebooting ${ID}"
}

error_check() {
    local error_file="${CONFIG_DIR}/${ID}.errorcount"
    local count=0
    local has_error=0
    local status
    local i

    if [[ -r "$error_file" ]]; then
        count="$(tr -dc '0-9' < "$error_file")"
        [[ -n "$count" ]] || count=0
    fi

    for i in "${!SIM_STATUSES[@]}"; do
        status="${SIM_STATUSES[$i]}"
        if is_error_status "$status"; then
            has_error=1
            break
        fi
    done

    if (( has_error )); then
        count=$((count + 1))
        printf '%s\n' "$count" > "$error_file"
        log "At least one SIM is in an error state; error counter is now $count"
    else
        printf '0\n' > "$error_file"
        return 0
    fi

    if (( count >= 2 )); then
        if all_sims_idle_or_error; then
            log "Error counter is $count and all SIMs are either IDLE or in error; rebooting ${ID}"
            printf '0\n' > "$error_file" #
            restart
        else
            log "Error counter is $count, but not all SIMs are IDLE or in error; not rebooting"
        fi
    fi
}

main() {
    login
    fetch_sims
    check_sip_trunks
    # check the SIM status, if stuck in Initialising, and other sims are not in a call, reboot
    # Ideally, we could just reboot the module of the initializing sim, but then we get stuck in
    # "no sim", so rebooting the whole device seems the only way out in case of errors
    error_check
}

main "$@"
