import os
import sqlite3
from contextlib import closing

DATA_DIR = os.environ.get("DATA_DIR", "/var/lib/cloudmaker")
DB_PATH = os.path.join(DATA_DIR, "state.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS images(
  name TEXT PRIMARY KEY, path TEXT NOT NULL,
  description TEXT DEFAULT '', format TEXT DEFAULT 'qcow2');
CREATE TABLE IF NOT EXISTS networks(
  name TEXT PRIMARY KEY, mode TEXT NOT NULL, cidr TEXT, bridge TEXT);
CREATE TABLE IF NOT EXISTS vms(
  name TEXT PRIMARY KEY, image TEXT NOT NULL, cpus INTEGER, ram_mb INTEGER,
  disk_gb INTEGER, network TEXT, hostname TEXT, ssh_key TEXT, user_data TEXT,
  desired TEXT DEFAULT 'running');
"""


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init():
    os.makedirs(DATA_DIR, mode=0o755, exist_ok=True)
    os.chmod(DATA_DIR, 0o755)
    with closing(_conn()) as c:
        c.executescript(SCHEMA)


def run(sql, args=()):
    with closing(_conn()) as c:
        with c:
            c.execute(sql, args)


def rows(sql, args=()):
    with closing(_conn()) as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None
