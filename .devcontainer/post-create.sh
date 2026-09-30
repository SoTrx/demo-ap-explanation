#! /bin/bash

# Workaround for newer linux kernel 
# https://github.com/devcontainers/features/issues/1235#event-21749942947
set -ex
if ! docker info > /dev/null 2>&1; then
    sudo update-alternatives --set iptables /usr/sbin/iptables-nft
fi

uv sync --all-groups

# Install kiota
TMP_DIR=$(mktemp -d)

ARCH=$(uname -m)
if [ "$ARCH" = "x86_64" ]; then
  FILE="linux-x64.zip"
elif [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
  FILE="linux-arm64.zip"
else
  echo "Unsupported architecture: $ARCH"
  exit 1
fi

curl -L https://aka.ms/get/kiota/latest/linux-x64.zip -o "$TMP_DIR/kiota.zip" \
    && unzip "$TMP_DIR/kiota.zip" -d "$TMP_DIR" \
    && sudo mv "$TMP_DIR/kiota" /usr/local/bin/kiota \
    && sudo chmod +x /usr/local/bin/kiota \
    && rm -rf "$TMP_DIR"