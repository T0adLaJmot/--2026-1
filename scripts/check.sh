#!/usr/bin/env bash
# Единый скрипт проверки всех реализованных пунктов (пункт 11).
# Требуется: docker compose up -d, curl, jq, ssh, virsh. Запуск: ./scripts/check.sh
# Необязательно: BRIDGE=br0 ./scripts/check.sh  — проверить сеть в режиме bridge
set -uo pipefail
cd "$(dirname "$0")/.."
API=${API:-http://localhost:8080}
VIRSH="virsh -c qemu:///system"
IMG_PATH=${IMG_PATH:-/var/lib/cloudmaker/images/base.img}
KEY=/tmp/cmtest_key
PASS=0; FAILN=0

step(){ echo; echo "== $*"; }
ok(){ echo "  [OK]   $*"; PASS=$((PASS+1)); }
bad(){ echo "  [FAIL] $*"; FAILN=$((FAILN+1)); }
check(){ local d=$1; shift; if "$@" >/dev/null 2>&1; then ok "$d"; else bad "$d"; fi; }
api(){ curl -sS -H 'Content-Type: application/json' "$@"; }
code(){ curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' "$@"; }
expect_code(){ local d=$1 e=$2; shift 2; local c; c=$(code "$@")
  if [ "$c" = "$e" ]; then ok "$d (HTTP $c)"; else bad "$d: ожидали $e, получили $c"; fi; }
wait_for(){ local t=$1; shift; for _ in $(seq "$t"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }

api_up(){ curl -fs "$API/health"; }
vm_state(){ api "$API/vms/cmtest-vm1" | jq -r .state; }
vm_running(){ [ "$(vm_state)" = running ]; }
vm_stopped(){ [ "$(vm_state)" = stopped ]; }
get_ip(){ api "$API/vms/cmtest-vm1" | jq -r '.ips[0] // empty'; }
have_ip(){ [ -n "$(get_ip)" ]; }
ssh_vm(){ ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o ConnectTimeout=3 -o BatchMode=yes -o LogLevel=ERROR "ubuntu@$IP" "$@"; }
hostname_ok(){ [ "$(ssh_vm hostname)" = cmtest-host ]; }
marker_ok(){ [ "$(ssh_vm cat /etc/cm-marker)" = hello-from-cloud-init ]; }
virsh_running(){ [ "$($VIRSH domstate cmtest-vm1)" = running ]; }

step "0. Подготовка"
[ -f "$IMG_PATH" ] || ./scripts/fetch-image.sh
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N '' -f "$KEY"
if wait_for 30 api_up; then ok "API доступен"; else bad "API недоступен на $API"; exit 1; fi

step "1. Реестр образов (п.7)"
expect_code "регистрация образа" 201 -X POST "$API/images" \
  -d "{\"name\":\"cmtest-img\",\"path\":\"$IMG_PATH\",\"description\":\"test image\"}"
check "образ виден в GET /images" bash -c "curl -s $API/images | jq -e '.[]|select(.name==\"cmtest-img\")'"
expect_code "несуществующий путь отклоняется" 400 -X POST "$API/images" \
  -d '{"name":"cmtest-bad","path":"/nonexistent.img"}'

step "2. Виртуальная сеть (п.3)"
expect_code "создание NAT-сети" 201 -X POST "$API/networks" \
  -d '{"name":"cmtest-net","mode":"nat","cidr":"10.77.0.0/24"}'
check "сеть активна в libvirt" $VIRSH net-info cm-cmtest-net
if [ -n "${BRIDGE:-}" ]; then
  expect_code "сеть в режиме bridge ($BRIDGE)" 201 -X POST "$API/networks" \
    -d "{\"name\":\"cmtest-br\",\"mode\":\"bridge\",\"bridge\":\"$BRIDGE\"}"
  check "bridge-сеть активна в libvirt" $VIRSH net-info cm-cmtest-br
fi

step "3. Создание ВМ + cloud-init (п.1, п.2)"
PUB=$(cat "$KEY.pub")
UD=$'#cloud-config\nwrite_files:\n  - path: /etc/cm-marker\n    content: hello-from-cloud-init\n'
BODY=$(jq -n --arg k "$PUB" --arg ud "$UD" '{name:"cmtest-vm1",image:"cmtest-img",cpus:1,
  ram_mb:1024,disk_gb:10,network:"cmtest-net",hostname:"cmtest-host",ssh_key:$k,user_data:$ud}')
expect_code "POST /vms" 201 -X POST "$API/vms" -d "$BODY"
if wait_for 120 have_ip; then IP=$(get_ip); ok "ВМ получила IP $IP"; else bad "ВМ не получила IP"; IP=; fi
if [ -n "$IP" ] && wait_for 180 ssh_vm true; then
  ok "SSH по ключу из cloud-init работает"
  check "hostname из cloud-init = cmtest-host" hostname_ok
  check "user_data выполнен (файл /etc/cm-marker)" marker_ok
else
  bad "SSH недоступен"
fi

step "4. ВМ видна на хосте через virsh (п.5)"
check "virsh domstate = running" virsh_running

step "5. Квоты (п.9)"
expect_code "ВМ сверх лимита CPU отклонена" 409 -X POST "$API/vms" \
  -d '{"name":"cmtest-big","image":"cmtest-img","cpus":999,"ram_mb":1024,"disk_gb":1,"network":"cmtest-net"}'
check "GET /quota отвечает" bash -c "curl -s $API/quota | jq -e .cpus.limit"

step "6. Остановка / запуск (п.1)"
expect_code "stop" 200 -X POST "$API/vms/cmtest-vm1/stop"
if wait_for 60 vm_stopped; then ok "состояние stopped"; else bad "ВМ не остановилась"; fi
expect_code "start" 200 -X POST "$API/vms/cmtest-vm1/start"
if wait_for 30 vm_running; then ok "состояние running"; else bad "ВМ не запустилась"; fi

step "7. Восстановление состояния (п.6)"
docker compose restart cloudmaker >/dev/null 2>&1
if wait_for 60 api_up && wait_for 30 vm_running; then ok "после рестарта контейнера ВМ в состоянии running"
else bad "состояние не восстановилось после рестарта"; fi
echo "  ... теряем домен libvirt (destroy+undefine) пока контейнер остановлен"
docker compose stop cloudmaker >/dev/null 2>&1
$VIRSH destroy cmtest-vm1 >/dev/null 2>&1; $VIRSH undefine cmtest-vm1 >/dev/null 2>&1
docker compose start cloudmaker >/dev/null 2>&1
if wait_for 60 api_up && wait_for 60 vm_running; then ok "домен пересоздан из БД и запущен"
else bad "домен не восстановлен"; fi

step "8. Защита от удаления используемых ресурсов"
expect_code "образ нельзя удалить пока он используется" 409 -X DELETE "$API/images/cmtest-img"
expect_code "сеть нельзя удалить пока она используется" 409 -X DELETE "$API/networks/cmtest-net"

step "9. Удаление"
expect_code "DELETE ВМ" 200 -X DELETE "$API/vms/cmtest-vm1"
expect_code "ВМ больше не найдена" 404 "$API/vms/cmtest-vm1"
[ -n "${BRIDGE:-}" ] && expect_code "DELETE bridge-сети" 200 -X DELETE "$API/networks/cmtest-br"
expect_code "DELETE сети" 200 -X DELETE "$API/networks/cmtest-net"
expect_code "DELETE образа" 200 -X DELETE "$API/images/cmtest-img"

echo; echo "ИТОГО: OK=$PASS  FAIL=$FAILN"
echo "Если остались хвосты: ./scripts/cleanup.sh"
[ "$FAILN" -eq 0 ]
