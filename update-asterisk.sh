#!/bin/bash
set -euo pipefail

AST_MAJOR=22
BASE_DIR=/usr/local/src/asterisk
TARBALL_DIR="$BASE_DIR/tarballs"
BUILD_DIR="$BASE_DIR/build"

usage() {
  echo "Usage: $0 [--check]" >&2
  echo "  --check  only notify if a newer release than the installed one exists" >&2
  exit 2
}

CHECK=0
case "${1-}" in
  "") ;;
  --check) CHECK=1 ;;
  *) usage ;;
esac

if (( CHECK )); then
  # Settings for --check, overridable from the environment.
  NOTIFY_HOOK="${ASTERISK_NOTIFY_HOOK-/usr/local/bin/pushover.sh}"
  ASTERISK_BIN="${ASTERISK_BIN:-$(command -v asterisk || echo /usr/sbin/asterisk)}"
  STATE_FILE="${ASTERISK_CHECK_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/update-asterisk/notified}"
else
  echo "Checking latest Asterisk $AST_MAJOR LTS version..."
fi

LATEST=$(
  curl -fsSL https://downloads.asterisk.org/pub/telephony/asterisk/ \
  | grep -Eo "asterisk-${AST_MAJOR}\.[0-9]+(\.[0-9]+)?\.tar\.gz" \
  | sed -E 's/^asterisk-(.*)\.tar\.gz$/\1/' \
  | sort -V | tail -n1
)

if [[ -z "$LATEST" ]]; then
  echo "Could not determine latest version"
  exit 1
fi

if (( CHECK )); then
  INSTALLED=$("$ASTERISK_BIN" -V | sed -nE 's/^Asterisk ([0-9][0-9.]*).*/\1/p')
  if [[ -z "$INSTALLED" ]]; then
    echo "Could not determine installed version from $ASTERISK_BIN -V" >&2
    exit 1
  fi

  newest=$(printf '%s\n%s\n' "$INSTALLED" "$LATEST" | sort -V | tail -n1)
  if [[ "$newest" == "$INSTALLED" ]]; then
    echo "$(date '+%F %T') Asterisk $INSTALLED is up to date"
    exit 0
  fi

  notified=$(cat "$STATE_FILE" 2>/dev/null || true)
  if [[ "$notified" == "$LATEST" ]]; then
    echo "$(date '+%F %T') Asterisk $LATEST available (installed $INSTALLED), already notified"
    exit 0
  fi

  message="Asterisk $LATEST is available on $(hostname -s) (installed: $INSTALLED)"
  echo "$(date '+%F %T') $message"
  if [[ -n "$NOTIFY_HOOK" ]]; then
    "$NOTIFY_HOOK" "$message" >/dev/null
  fi
  mkdir -p "$(dirname "$STATE_FILE")"
  echo "$LATEST" > "$STATE_FILE"
  exit 0
fi

echo "Latest version: $LATEST"

TARBALL="$TARBALL_DIR/asterisk-$LATEST.tar.gz"
SRC_DIR="$BUILD_DIR/asterisk-$LATEST"

if [[ -d "$SRC_DIR" ]]; then
  echo "Version $LATEST already extracted. Exiting."
  exit 0
fi

echo "Downloading tarball..."
curl -fL "https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-$LATEST.tar.gz" \
  -o "$TARBALL"

echo "Extracting..."
tar -xzf "$TARBALL" -C "$BUILD_DIR"

cd "$SRC_DIR"

echo "Configuring..."
./configure

echo "Building..."
make -j"$(nproc)"

if [ -d /etc/asterisk ]; then
  echo "Backing up config"
  ts=$(date +%F-%H%M%S)
  for f in /etc/asterisk/*.conf
  do
    sudo cp -a "$f" "$f.bak.$ts"
  done
fi

echo "Installing..."
sudo systemctl stop asterisk || true
sudo make install
if [ ! -f /etc/asterisk/asterisk.conf ]; then
  sudo make samples || true
fi
sudo make config
sudo ldconfig
sudo systemctl start asterisk

echo "Asterisk upgraded to $LATEST"

