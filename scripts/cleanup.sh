#!/usr/bin/env bash
# Удаляет следы тестирования: ВМ, сети, образы, диски (всё с префиксом cmtest-), базовую ВМ.
API=${API:-http://localhost:8080}
VIRSH="sudo virsh -c qemu:///system"

# 1. через API (если сервис жив)
for vm in $(curl -fs "$API/vms" 2>/dev/null | jq -r '.[].name|select(startswith("cmtest-"))'); do
  curl -s -X DELETE "$API/vms/$vm" >/dev/null; done
for n in $(curl -fs "$API/networks" 2>/dev/null | jq -r '.[].name|select(startswith("cmtest-"))'); do
  curl -s -X DELETE "$API/networks/$n" >/dev/null; done
for i in $(curl -fs "$API/images" 2>/dev/null | jq -r '.[].name|select(startswith("cmtest-"))'); do
  curl -s -X DELETE "$API/images/$i" >/dev/null; done

# 2. напрямую через libvirt (на случай, если API недоступен)
for d in $($VIRSH list --all --name | grep -E '^(cmtest-|cm-base)'); do
  $VIRSH destroy "$d" 2>/dev/null; $VIRSH undefine "$d" 2>/dev/null; done
for n in $($VIRSH net-list --all --name | grep '^cm-cmtest-'); do
  $VIRSH net-destroy "$n" 2>/dev/null; $VIRSH net-undefine "$n" 2>/dev/null; done

# 3. файлы
sudo rm -rf /var/lib/cloudmaker/vms/cmtest-* /var/lib/cloudmaker/base-cm-base
rm -f /tmp/cmtest_key /tmp/cmtest_key.pub
echo "Очистка завершена."
