"""Работа с libvirt: сети, диски, cloud-init seed, домены."""
import ipaddress
import os
import shutil
import subprocess
import time

import libvirt
import yaml

DATA_DIR = os.environ.get("DATA_DIR", "/var/lib/cloudmaker")
URI = os.environ.get("LIBVIRT_URI", "qemu:///system")

_conn = None


def conn():
    global _conn
    if _conn is None or not _conn.isAlive():
        _conn = libvirt.open(URI)
    return _conn


# ---------------------------------------------------------------- сети
def lv_net_name(name):
    return "default" if name == "default" else "cm-" + name


def net_xml(n):
    name = lv_net_name(n["name"])
    if n["mode"] == "nat":
        net = ipaddress.ip_network(n["cidr"])
        return (
            f"<network><name>{name}</name><forward mode='nat'/>"
            f"<bridge name='{n['bridge']}' stp='on' delay='0'/>"
            f"<ip address='{net[1]}' netmask='{net.netmask}'>"
            f"<dhcp><range start='{net[10]}' end='{net[-2]}'/></dhcp></ip></network>"
        )
    # режим bridge: ВМ подключаются в существующий bridge хоста (br0)
    return (f"<network><name>{name}</name><forward mode='bridge'/>"
            f"<bridge name='{n['bridge']}'/></network>")


def ensure_network(n):
    c = conn()
    try:
        net = c.networkLookupByName(lv_net_name(n["name"]))
    except libvirt.libvirtError:
        net = c.networkDefineXML(net_xml(n))
    if not net.isActive():
        net.create()
    net.setAutostart(True)


def ensure_default_network():
    net = conn().networkLookupByName("default")
    if not net.isActive():
        net.create()


def delete_network(name):
    try:
        net = conn().networkLookupByName(lv_net_name(name))
    except libvirt.libvirtError:
        return
    if net.isActive():
        net.destroy()
    net.undefine()


# ---------------------------------------------------------------- диски / cloud-init
def vm_dir(name):
    return f"{DATA_DIR}/vms/{name}"


def _sh(cmd, cwd=None):
    subprocess.run(cmd, check=True, cwd=cwd, capture_output=True, text=True)


def build_user_data(hostname, ssh_key, user_data):
    # не cloud-config (например shell-скрипт) — передаём как есть
    if user_data and not user_data.lstrip().startswith("#cloud-config"):
        return user_data
    cfg = (yaml.safe_load(user_data) if user_data else None) or {}
    cfg.setdefault("hostname", hostname)
    cfg.setdefault("manage_etc_hosts", True)
    if ssh_key:
        cfg.setdefault("ssh_authorized_keys", []).append(ssh_key)
    return "#cloud-config\n" + yaml.safe_dump(cfg)


def make_seed(vm):
    d = vm_dir(vm["name"])
    with open(f"{d}/user-data", "w") as f:
        f.write(build_user_data(vm["hostname"], vm.get("ssh_key"), vm.get("user_data")))
    with open(f"{d}/meta-data", "w") as f:
        f.write(f"instance-id: {vm['name']}\nlocal-hostname: {vm['hostname']}\n")
    _sh(["genisoimage", "-output", "seed.iso", "-volid", "cidata", "-joliet",
         "-rock", "user-data", "meta-data"], cwd=d)


def make_disk(vm, image):
    disk = f"{vm_dir(vm['name'])}/disk.qcow2"
    _sh(["qemu-img", "create", "-f", "qcow2", "-F", image["format"],
         "-b", image["path"], disk, f"{vm['disk_gb']}G"])


# ---------------------------------------------------------------- домены
def domain_xml(vm):
    d = vm_dir(vm["name"])
    return f"""
<domain type='kvm'>
  <name>{vm['name']}</name>
  <memory unit='MiB'>{vm['ram_mb']}</memory>
  <vcpu>{vm['cpus']}</vcpu>
  <os><type arch='x86_64' machine='q35'>hvm</type><boot dev='hd'/></os>
  <cpu mode='host-passthrough'/>
  <features><acpi/><apic/></features>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{d}/disk.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{d}/seed.iso'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>
    <interface type='network'>
      <source network='{lv_net_name(vm['network'])}'/>
      <model type='virtio'/>
    </interface>
    <serial type='pty'><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
    <rng model='virtio'><backend model='random'>/dev/urandom</backend></rng>
  </devices>
</domain>"""


_STATES = {
    libvirt.VIR_DOMAIN_RUNNING: "running",
    libvirt.VIR_DOMAIN_SHUTOFF: "stopped",
    libvirt.VIR_DOMAIN_PAUSED: "paused",
    libvirt.VIR_DOMAIN_SHUTDOWN: "stopping",
    libvirt.VIR_DOMAIN_CRASHED: "crashed",
}


def _lookup(name):
    try:
        return conn().lookupByName(name)
    except libvirt.libvirtError:
        return None


def state(name):
    dom = _lookup(name)
    return "undefined" if dom is None else _STATES.get(dom.state()[0], "unknown")


def ips(name):
    dom = _lookup(name)
    if dom is None or not dom.isActive():
        return []
    out = set()
    for src in (libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE,
                libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_ARP):
        try:
            for iface in dom.interfaceAddresses(src).values():
                for a in iface["addrs"] or []:
                    if a["type"] == libvirt.VIR_IP_ADDR_TYPE_IPV4:
                        out.add(a["addr"])
        except libvirt.libvirtError:
            pass
    return sorted(out)


def ensure_vm(vm, image):
    """Приводит ВМ к состоянию из БД. Идемпотентна — используется и при
    создании, и при восстановлении после перезапуска."""
    d = vm_dir(vm["name"])
    os.makedirs(d, mode=0o755, exist_ok=True)
    os.chmod(d, 0o755)
    if vm["network"] == "default":
        ensure_default_network()
    if not os.path.exists(f"{d}/disk.qcow2"):
        make_disk(vm, image)
    if not os.path.exists(f"{d}/seed.iso"):
        make_seed(vm)
    dom = _lookup(vm["name"])
    if dom is None:
        dom = conn().defineXML(domain_xml(vm))
    want_run = vm["desired"] == "running"
    dom.setAutostart(1 if want_run else 0)
    if want_run and not dom.isActive():
        dom.create()
    elif not want_run and dom.isActive():
        stop(vm["name"])


def start(name):
    dom = conn().lookupByName(name)
    dom.setAutostart(1)
    if not dom.isActive():
        dom.create()


def stop(name, force=False):
    dom = conn().lookupByName(name)
    dom.setAutostart(0)
    if not dom.isActive():
        return
    if force:
        dom.destroy()
        return
    dom.shutdown()  # ACPI
    for _ in range(30):
        if not dom.isActive():
            return
        time.sleep(1)
    dom.destroy()


def delete_vm(name):
    dom = _lookup(name)
    if dom is not None:
        if dom.isActive():
            dom.destroy()
        dom.undefine()
    shutil.rmtree(vm_dir(name), ignore_errors=True)
