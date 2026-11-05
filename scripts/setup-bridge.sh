#!/usr/bin/env bash
# Создаёт bridge br0 и включает в него физический интерфейс (NetworkManager).
# ВНИМАНИЕ: по SSH соединение может оборваться; Wi-Fi в bridge добавить нельзя.
# Использование: sudo ./scripts/setup-bridge.sh <iface> [bridge=br0]
set -euo pipefail
IFACE=${1:?укажите интерфейс (ip -br link)}
BR=${2:-br0}
nmcli con add type bridge ifname "$BR" con-name "$BR" stp no ipv4.method auto
nmcli con add type bridge-slave ifname "$IFACE" master "$BR" con-name "$BR-port-$IFACE"
nmcli con up "$BR"
ip -br addr show "$BR"
