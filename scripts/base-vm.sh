#!/usr/bin/env bash
# БАЗОВОЕ ЗАДАНИЕ: ВМ без GUI и без нашего приложения (virt-install + cloud-init)
# Использование: ./scripts/base-vm.sh   (переменные: NAME, PUBKEY)
set -euo pipefail
cd "$(dirname "$0")/.."
NAME=${NAME:-cm-base}
PUBKEY=${PUBKEY:-$HOME/.ssh/id_ed25519.pub}
BASE=/var/lib/cloudmaker/images/base.img
DIR=/var/lib/cloudmaker/base-$NAME
[ -f "$PUBKEY" ] || { echo "Нет ключа $PUBKEY (ssh-keygen -t ed25519)"; exit 1; }
[ -f "$BASE" ] || ./scripts/fetch-image.sh
sudo mkdir -p "$DIR"; sudo chmod 755 "$DIR"

# 1. диск ВМ поверх базового образа (copy-on-write)
sudo qemu-img create -f qcow2 -F qcow2 -b "$BASE" "$DIR/disk.qcow2" 10G

# 2. cloud-init seed
TMP=$(mktemp -d)
cat > "$TMP/user-data" <<UD
#cloud-config
hostname: $NAME
ssh_authorized_keys:
  - $(cat "$PUBKEY")
UD
printf 'instance-id: %s\nlocal-hostname: %s\n' "$NAME" "$NAME" > "$TMP/meta-data"
(cd "$TMP" && sudo genisoimage -output "$DIR/seed.iso" -volid cidata -joliet -rock user-data meta-data)

# 3. запуск ВМ через libvirt (virt-install), без графики
sudo virt-install --connect qemu:///system --name "$NAME" --memory 1024 --vcpus 1 \
  --import --os-variant ubuntu24.04 \
  --disk path="$DIR/disk.qcow2",format=qcow2,bus=virtio \
  --disk path="$DIR/seed.iso",device=cdrom \
  --network network=default --graphics none --noautoconsole

echo "Ждём IP..."
for _ in $(seq 60); do
  IP=$(sudo virsh -c qemu:///system domifaddr "$NAME" --source lease | awk '/ipv4/{split($4,a,"/");print a[1]}')
  [ -n "${IP:-}" ] && break; sleep 2
done
echo "ВМ запущена: $(sudo virsh -c qemu:///system domstate "$NAME"), IP: ${IP:-?}"
echo "Подключение: ssh ubuntu@${IP:-<ip>}"
echo "Удалить: sudo virsh destroy $NAME; sudo virsh undefine $NAME; sudo rm -rf $DIR"
