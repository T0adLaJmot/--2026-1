#!/usr/bin/env bash
# Скачивает облачный образ Ubuntu 24.04 в /var/lib/cloudmaker/images/base.img
set -euo pipefail
DIR=/var/lib/cloudmaker/images
sudo mkdir -p "$DIR"
sudo chmod 755 /var/lib/cloudmaker "$DIR"
sudo curl -fL -o "$DIR/base.img" \
  https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
echo "OK: $DIR/base.img"
