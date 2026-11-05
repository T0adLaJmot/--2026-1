# Cloud Maker

Весь проект выполнялся через virtual box на ubuntu 22.04.5
Для того, чтобы работало kvm необходимо было в windows отключить core protection и что-то связанное с компонентой hyper-V
Мини-облако поверх QEMU/KVM + libvirt: HTTP API создаёт, останавливает и удаляет ВМ.
Приложение работает **в контейнере** и управляет ВМ через **хостовый libvirt** (сокет).

## Реализованные пункты

| № | Пункт | Баллы | Где |
|---|-------|-------|-----|
| — | Базовое: ВМ без GUI (virt-install/libvirt) + описание | 4 | `scripts/base-vm.sh`, раздел «Базовое задание» |
| 1 | Приложение с HTTP API (create / stop / start / delete) | 2 | `app/main.py` |
| 2 | cloud-init: user_data, ssh-ключ, hostname | 2 | `app/vmm.py` (`make_seed`), `examples/` |
| 3 | Виртуальные сети (NAT) и подключение к хостовой сети через bridge | 3 | `POST /networks`, `scripts/setup-bridge.sh` |
| 5 | Приложение в контейнере управляет ВМ на хосте через libvirt socket | 3 | `docker-compose.yml` |
| 6 | Восстановление состояния после перезапуска | 2 | `reconcile()` в `app/main.py` |
| 7 | Регистрация образов (название, путь, описание) | 2 | `POST /images` |
| 9 | Квоты CPU/RAM | 2 | `check_quota()`, `QUOTA_*` в compose |
| 11 | Один скрипт проверки всех фич | 2 | `scripts/check.sh` |
| 12 | Скрипт очистки результатов тестирования | 1 | `scripts/cleanup.sh` |

## Требования к хосту (Ubuntu 24.04)

```bash
sudo apt install -y qemu-kvm libvirt-daemon-system libvirt-clients virtinst \
     genisoimage qemu-utils docker.io docker-compose-v2 curl jq
sudo usermod -aG libvirt,docker $USER   # перелогиниться
ls -l /dev/kvm /var/run/libvirt/libvirt-sock   # оба должны существовать, если нет, то если запускаете с виртуалки нужно выключить защиту ядра, а иначе хз
```

## Базовое задание: ВМ без приложения

```bash
./scripts/base-vm.sh
```
Что происходит: 
(1) скачивается облачный образ Ubuntu;  
(2) `qemu-img` создаёт copy-on-write диск поверх него;
(3) `genisoimage` собирает cloud-init seed-ISO (hostname + SSH-ключ);
(4) `virt-install --import --graphics none` определяет и запускает ВМ в libvirt (QEMU/KVM);
(5) скрипт ждёт DHCP-аренду и печатает IP.
Результат: `virsh list` показывает `cm-base` в состоянии running, `ssh ubuntu@<ip>` работает.

## Запуск приложения

```bash
./scripts/fetch-image.sh            # образ -> /var/lib/cloudmaker/images/base.img
docker compose up -d --build
curl localhost:8080/health          # {"status":"ok"}
```
Каталог `/var/lib/cloudmaker` смонтирован в контейнер по **тому же пути**, что и на хосте:
qemu запускается на хосте и должен видеть те же пути к дискам. В нём же лежит `state.db`.

## API

```bash
# образ (п.7)
curl -X POST localhost:8080/images -H 'Content-Type: application/json' \
  -d '{"name":"ubuntu24","path":"/var/lib/cloudmaker/images/base.img","description":"Ubuntu 24.04"}'
# NAT-сеть (п.3)
curl -X POST localhost:8080/networks -H 'Content-Type: application/json' \
  -d '{"name":"lab","mode":"nat","cidr":"10.77.0.0/24"}'
# сеть в хостовый bridge (п.3), bridge br0 должен существовать на хосте
curl -X POST localhost:8080/networks -H 'Content-Type: application/json' \
  -d '{"name":"lan","mode":"bridge","bridge":"br0"}'
# ВМ с cloud-init (п.1, п.2)
curl -X POST localhost:8080/vms -H 'Content-Type: application/json' -d "$(jq -n \
  --arg k "$(cat ~/.ssh/id_ed25519.pub)" --rawfile ud examples/user-data.yaml \
  '{name:"vm1",image:"ubuntu24",cpus:1,ram_mb:1024,disk_gb:10,network:"lab",
    hostname:"vm1",ssh_key:$k,user_data:$ud}')"
curl localhost:8080/vms                     # состояние и IP
curl -X POST localhost:8080/vms/vm1/stop    # ?force=true — жёсткое выключение
curl -X POST localhost:8080/vms/vm1/start
curl -X DELETE localhost:8080/vms/vm1
curl localhost:8080/quota
virsh -c qemu:///system list --all          # ВМ видны на хосте (п.5)
```

### Как это работает
* **cloud-init**: для каждой ВМ строится seed-ISO (`cidata`) с `user-data`/`meta-data`.
  Если `user_data` — это `#cloud-config`, он объединяется с `hostname` и `ssh_key`;
  иначе (shell-скрипт) передаётся как есть. Пользователь по умолчанию в образе — `ubuntu`.
* **Сети**: каждая сеть — отдельная постоянная libvirt-сеть `cm-<имя>` с автозапуском.
  `nat` — свой bridge + DHCP + NAT; `bridge` — `forward mode='bridge'`, ВМ попадает
  в L2 хостовой сети. Сеть `default` — стандартная сеть libvirt.
* **Создание bridge на хосте** (для режима `bridge`): `sudo ./scripts/setup-bridge.sh <iface>`
  (NetworkManager) или netplan:
  ```yaml
  network: {version: 2, ethernets: {enp3s0: {}},
            bridges: {br0: {interfaces: [enp3s0], dhcp4: true}}}
  ```
  Wi-Fi-интерфейс в bridge добавить нельзя. По SSH соединение может оборваться.
* **Квоты**: суммарные CPU/RAM *работающих* ВМ (желаемое состояние `running`) плюс новая
  ВМ не должны превышать `QUOTA_CPUS` / `QUOTA_RAM_MB`, иначе HTTP 409.
  Запуск остановленной ВМ тоже проверяется.
* **Восстановление**: БД SQLite — источник истины. При старте `reconcile()` пересоздаёт
  недостающие сети и домены libvirt, дособирает диск/seed и приводит ВМ к желаемому
  состоянию (running/stopped). Домены также помечены autostart на уровне libvirt.

## Проверка

```bash
./scripts/check.sh                 # все пункты; BRIDGE=br0 ./scripts/check.sh — ещё и bridge
```
Ожидаемый результат: все строки `[OK]`, в конце `ИТОГО: OK=<N>  FAIL=0`, код возврата 0.
Почему-то не работают пару пунктов в 3 разделе, а именно:
== 3. Создание ВМ + cloud-init (п.1, п.2)
  [OK]   POST /vms (HTTP 201)
  [FAIL] ВМ не получила IP
  [FAIL] SSH недоступен
хз, как исправлять.
Скрипт: регистрирует образ, создаёт сеть и ВМ с cloud-init, проверяет SSH, hostname и файл
из `user_data`, видимость ВМ в `virsh`, отказ по квоте (409), stop/start, рестарт контейнера
и пересоздание потерянного домена из БД, защиту от удаления занятых ресурсов, удаление.

## Очистка

```bash
./scripts/cleanup.sh      # удаляет ВМ, сети, образы, диски с префиксом cmtest-/cm-base
docker compose down       # остановить приложение
```
