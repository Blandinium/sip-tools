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

SIM1_NAME=""
SIM1_STATUS=""
SIM2_NAME=""
SIM2_STATUS=""

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
    local -a sim_records sim_names sim_statuses
    local i record old_status log_parts
    local -a fields

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
        sim_statuses[$i]=${fields[2]:-}
    done

    # Log all SIMs
    log_parts=""
    for i in "${!sim_names[@]}"; do
        [[ -n "$log_parts" ]] && log_parts="${log_parts}; "
        log_parts+="${sim_names[$i]}: ${sim_statuses[$i]}"
    done
    log "$log_parts"

    # Compare/store/notify
    for i in "${!sim_names[@]}"; do
        touch "${CONFIG_DIR}/${ID}.sim$((i+1))" # it might not yet exist
        old_status=$(cat "${CONFIG_DIR}/${ID}.sim$((i+1))")
        printf '%s\n' "${sim_statuses[$i]}" > "${CONFIG_DIR}/${ID}.sim$((i+1))"

        if [[ "$old_status" != "${sim_statuses[$i]}" ]]; then
            notify "${ID} SIM ${sim_names[$i]} changed from ${old_status} to ${sim_statuses[$i]}"
        fi
    done
}

restart() {
    curl -sS "${BASE_URL}/cgi/WebCGI?1262" \
         -X POST \
         -o /dev/null
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

main() {
    login
    fetch_sims
    # TODO:
    # check the SIM status, if stuck in initialzing, and other sims are not in a call, reboot
    # Idealy, we could just reboot the module of the initializing sim, but then we get stuck in
    # "no sim", so rebooting the whole device seems the only way out...
    # restart
}

main "$@"
