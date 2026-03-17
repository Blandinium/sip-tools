#!/usr/bin/env bash
set -euo pipefail
umask 077

read_required_file() {
    local path="$1"
    if [[ ! -r "$path" ]]; then
        printf 'Error: required config file not readable: %s\n' "$path" >&2
        exit 1
    fi
    <"$path" tr -d '\r'
}

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/yeastar"
ID="tg200"
TG_HOST="$(read_required_file "${CONFIG_DIR}/${ID}.host")"
TG_USER="$(read_required_file "${CONFIG_DIR}/${ID}.user")"
TG_PASS="$(read_required_file "${CONFIG_DIR}/${ID}.pass")"
NOTIFY_HOOK="/usr/local/bin/pushover.sh"

BASE_URL="http://${TG_HOST}"
LOGIN_URL="${BASE_URL}/cgi/WebCGI?1000"
TMP_FILES=()
declare -a SIM_STATUSES=()
declare -a ERROR_STATUSES=(
    "Registering"
    "NO SIM"
)

log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$*" >&2
}

notify() {
    if [ -n "$NOTIFY_HOOK" ]
    then
        "$NOTIFY_HOOK" "$*" >/dev/null
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
        if [[ -f "${CONFIG_DIR}/${ID}.down" ]]
        then
            rm -f "${CONFIG_DIR}/${ID}.down"
            notify "$ID is available again"
        fi
        return 0
    else
        log "Login failed"
        if [[ ! -f "${CONFIG_DIR}/${ID}.down" ]]
        then
            date>> "${CONFIG_DIR}/${ID}.down"
            notify "Login into ${ID} failed"
        fi
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

    # Extract MyPBX_COMM value
    comm=$(sed -n 's/.*id="MyPBX_COMM" value="\([^"]*\)".*/\1/p' "$body_file")

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
    # check the SIM status, if stuck in Initialising, and other sims are not in a call, reboot
    # Ideally, we could just reboot the module of the initializing sim, but then we get stuck in
    # "no sim", so rebooting the whole device seems the only way out in case of errors
    error_check
}

main "$@"
