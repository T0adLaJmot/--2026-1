import hashlib
import ipaddress
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Annotated, Literal, Optional

import libvirt
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints

from . import db, vmm

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cloudmaker")

QUOTA_CPUS = int(os.environ.get("QUOTA_CPUS", "8"))
QUOTA_RAM_MB = int(os.environ.get("QUOTA_RAM_MB", "8192"))
LOCK = threading.RLock()

Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$")]


class ImageIn(BaseModel):
    name: Name
    path: str
    description: str = ""
    format: Literal["qcow2", "raw"] = "qcow2"


class NetIn(BaseModel):
    name: Name
    mode: Literal["nat", "bridge"] = "nat"
    cidr: Optional[str] = None      # для nat, например 10.77.0.0/24
    bridge: Optional[str] = None    # для bridge, например br0


class VmIn(BaseModel):
    name: Name
    image: str
    cpus: int = Field(1, ge=1, le=64)
    ram_mb: int = Field(1024, ge=256)
    disk_gb: int = Field(10, ge=1)
    network: str = "default"
    hostname: Optional[str] = None
    ssh_key: Optional[str] = None
    user_data: Optional[str] = None


def reconcile():
    """Восстановление состояния после рестарта: БД — источник истины."""
    for n in db.rows("SELECT * FROM networks"):
        try:
            vmm.ensure_network(n)
        except Exception:
            log.exception("network %s", n["name"])
    for v in db.rows("SELECT * FROM vms"):
        try:
            img = db.one("SELECT * FROM images WHERE name=?", (v["image"],))
            vmm.ensure_vm(v, img)
            log.info("vm %s restored (desired=%s)", v["name"], v["desired"])
        except Exception:
            log.exception("vm %s", v["name"])


@asynccontextmanager
async def lifespan(app):
    db.init()
    try:
        reconcile()
    except Exception:
        log.exception("reconcile failed")
    yield


app = FastAPI(title="Cloud Maker", lifespan=lifespan)


@app.exception_handler(libvirt.libvirtError)
def _lv_err(request, exc):
    return JSONResponse(status_code=500, content={"detail": f"libvirt: {exc}"})


@app.get("/health")
def health():
    return {"status": "ok"}


# ------------------------------------------------------------ images (п.7)
@app.post("/images", status_code=201)
def add_image(b: ImageIn):
    if not os.path.isabs(b.path) or not os.path.isfile(b.path):
        raise HTTPException(400, "path must be an absolute path to an existing file")
    if db.one("SELECT 1 FROM images WHERE name=?", (b.name,)):
        raise HTTPException(409, "image already exists")
    db.run("INSERT INTO images VALUES(?,?,?,?)", (b.name, b.path, b.description, b.format))
    return b


@app.get("/images")
def list_images():
    return db.rows("SELECT * FROM images")


@app.delete("/images/{name}")
def del_image(name: str):
    if db.one("SELECT 1 FROM vms WHERE image=?", (name,)):
        raise HTTPException(409, "image is used by a VM (backing file)")
    db.run("DELETE FROM images WHERE name=?", (name,))
    return {"deleted": name}


# ------------------------------------------------------------ networks (п.3)
@app.post("/networks", status_code=201)
def add_network(b: NetIn):
    if b.name == "default":
        raise HTTPException(400, "'default' is reserved (libvirt default network)")
    if db.one("SELECT 1 FROM networks WHERE name=?", (b.name,)):
        raise HTTPException(409, "network already exists")
    n = b.model_dump()
    if b.mode == "nat":
        try:
            net = ipaddress.ip_network(b.cidr, strict=True)
        except Exception:
            raise HTTPException(400, "nat mode requires valid cidr, e.g. 10.77.0.0/24")
        if net.version != 4 or net.prefixlen > 28:
            raise HTTPException(400, "need IPv4 network with prefix <= /28")
        n["bridge"] = "cm" + hashlib.md5(b.name.encode()).hexdigest()[:8]
    elif not b.bridge:
        raise HTTPException(400, "bridge mode requires 'bridge' (host bridge name, e.g. br0)")
    db.run("INSERT INTO networks VALUES(?,?,?,?)", (n["name"], n["mode"], n["cidr"], n["bridge"]))
    try:
        vmm.ensure_network(n)
    except Exception as e:
        db.run("DELETE FROM networks WHERE name=?", (b.name,))
        vmm.delete_network(b.name)
        raise HTTPException(500, f"failed to create network: {e}")
    return n


@app.get("/networks")
def list_networks():
    return db.rows("SELECT * FROM networks")


@app.delete("/networks/{name}")
def del_network(name: str):
    if db.one("SELECT 1 FROM vms WHERE network=?", (name,)):
        raise HTTPException(409, "network is used by a VM")
    vmm.delete_network(name)
    db.run("DELETE FROM networks WHERE name=?", (name,))
    return {"deleted": name}


# ------------------------------------------------------------ квоты (п.9)
def used():
    r = db.rows("SELECT cpus, ram_mb FROM vms WHERE desired='running'")
    return sum(x["cpus"] for x in r), sum(x["ram_mb"] for x in r)


def check_quota(cpus, ram_mb):
    u_cpu, u_ram = used()
    if u_cpu + cpus > QUOTA_CPUS or u_ram + ram_mb > QUOTA_RAM_MB:
        raise HTTPException(
            409, f"quota exceeded: used {u_cpu}/{QUOTA_CPUS} CPU, {u_ram}/{QUOTA_RAM_MB} MB RAM; "
                 f"requested {cpus} CPU, {ram_mb} MB")


@app.get("/quota")
def quota():
    u_cpu, u_ram = used()
    return {"cpus": {"used": u_cpu, "limit": QUOTA_CPUS},
            "ram_mb": {"used": u_ram, "limit": QUOTA_RAM_MB}}


# ------------------------------------------------------------ VMs (п.1)
def view(v):
    v = dict(v)
    v["state"] = vmm.state(v["name"])
    v["ips"] = vmm.ips(v["name"])
    return v


def get_vm_or_404(name):
    v = db.one("SELECT * FROM vms WHERE name=?", (name,))
    if not v:
        raise HTTPException(404, "VM not found")
    return v


@app.post("/vms", status_code=201)
def create_vm(b: VmIn):
    with LOCK:
        if db.one("SELECT 1 FROM vms WHERE name=?", (b.name,)) or vmm.state(b.name) != "undefined":
            raise HTTPException(409, "VM with this name already exists")
        img = db.one("SELECT * FROM images WHERE name=?", (b.image,))
        if not img:
            raise HTTPException(404, f"image '{b.image}' not registered")
        if b.network != "default" and not db.one("SELECT 1 FROM networks WHERE name=?", (b.network,)):
            raise HTTPException(404, f"network '{b.network}' not found")
        check_quota(b.cpus, b.ram_mb)
        vm = b.model_dump()
        vm["hostname"] = vm["hostname"] or b.name
        vm["desired"] = "running"
        db.run("INSERT INTO vms(name,image,cpus,ram_mb,disk_gb,network,hostname,ssh_key,user_data,desired)"
               " VALUES(?,?,?,?,?,?,?,?,?,?)",
               (vm["name"], vm["image"], vm["cpus"], vm["ram_mb"], vm["disk_gb"], vm["network"],
                vm["hostname"], vm["ssh_key"], vm["user_data"], vm["desired"]))
        try:
            vmm.ensure_vm(vm, img)
        except Exception as e:
            db.run("DELETE FROM vms WHERE name=?", (b.name,))
            vmm.delete_vm(b.name)
            raise HTTPException(500, f"failed to create VM: {e}")
    return view(vm)


@app.get("/vms")
def list_vms():
    return [view(v) for v in db.rows("SELECT * FROM vms")]


@app.get("/vms/{name}")
def get_vm(name: str):
    return view(get_vm_or_404(name))


@app.post("/vms/{name}/start")
def start_vm(name: str):
    with LOCK:
        v = get_vm_or_404(name)
        if v["desired"] != "running":
            check_quota(v["cpus"], v["ram_mb"])
        db.run("UPDATE vms SET desired='running' WHERE name=?", (name,))
        v["desired"] = "running"
        vmm.ensure_vm(v, db.one("SELECT * FROM images WHERE name=?", (v["image"],)))
    return view(v)


@app.post("/vms/{name}/stop")
def stop_vm(name: str, force: bool = False):
    with LOCK:
        v = get_vm_or_404(name)
        db.run("UPDATE vms SET desired='stopped' WHERE name=?", (name,))
        if vmm.state(name) != "undefined":
            vmm.stop(name, force)
        v["desired"] = "stopped"
    return view(v)


@app.delete("/vms/{name}")
def delete_vm(name: str):
    with LOCK:
        get_vm_or_404(name)
        vmm.delete_vm(name)
        db.run("DELETE FROM vms WHERE name=?", (name,))
    return {"deleted": name}
