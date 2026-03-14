#!/bin/bash
set -euo pipefail

AST_MAJOR=22
BASE_DIR=/usr/local/src/asterisk
TARBALL_DIR="$BASE_DIR/tarballs"
BUILD_DIR="$BASE_DIR/build"

echo "Checking latest Asterisk $AST_MAJOR LTS version..."

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
    cp -a "$f" "$f.bak.$ts"
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

