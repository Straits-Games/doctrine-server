"""Custom (Scenario Builder) maps: content-addressed storage + an id -> newest-sha index.

Files live at `scenarios/<sha256>.hexscn`. Re-uploading an id points it at the new
sha; old files are kept because games may reference them by sha.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from pathlib import Path

from . import auth, db
from .errors import ApiError

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def valid_sha(value) -> bool:
    return isinstance(value, str) and bool(SHA_RE.match(value))


def valid_id(value) -> bool:
    return isinstance(value, str) and bool(ID_RE.match(value))


def file_path(data_dir: Path, sha: str) -> Path:
    if not valid_sha(sha):                       # path safety: only 64 hex chars
        raise ValueError("bad sha256")
    return Path(data_dir) / "scenarios" / f"{sha}.hexscn"


def file_exists(conn: sqlite3.Connection, sha: str) -> bool:
    if not valid_sha(sha):
        return False
    return conn.execute("SELECT 1 FROM scenario_files WHERE sha256 = ?",
                        (sha,)).fetchone() is not None


def get_meta(conn: sqlite3.Connection, scenario_id: str) -> dict | None:
    if not valid_id(scenario_id):
        return None
    row = conn.execute("SELECT * FROM scenarios WHERE scenario_id = ?", (scenario_id,)).fetchone()
    if row is None:
        return None
    return {"scenario_id": row["scenario_id"], "name": row["name"], "sha256": row["sha256"],
            "exists": True}


def store(conn: sqlite3.Connection, data_dir: Path, player_id: str, meta: dict,
          data: bytes) -> None:
    scenario_id = meta.get("scenario_id")
    if not valid_id(scenario_id):
        raise ApiError(400, "bad_scenario")
    claimed = meta.get("sha256")
    actual = hashlib.sha256(data).hexdigest()
    if not isinstance(claimed, str) or claimed.strip().lower() != actual:
        raise ApiError(400, "sha_mismatch")
    name = str(meta.get("name") or "").strip()[:64] or scenario_id
    path = file_path(data_dir, actual)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    t = auth.now()
    with db.tx(conn):
        conn.execute(
            "INSERT OR IGNORE INTO scenario_files (sha256, size, uploaded_by, uploaded_at)"
            " VALUES (?, ?, ?, ?)", (actual, len(data), player_id, t))
        conn.execute(
            "INSERT INTO scenarios (scenario_id, name, sha256, uploaded_by, updated_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(scenario_id) DO UPDATE SET"
            " name = excluded.name, sha256 = excluded.sha256,"
            " uploaded_by = excluded.uploaded_by, updated_at = excluded.updated_at",
            (scenario_id, name, actual, player_id, t))


def bundle(conn: sqlite3.Connection, data_dir: Path, scenario_id: str) -> bytes | None:
    meta = get_meta(conn, scenario_id)
    if meta is None:
        return None
    try:
        return file_path(data_dir, meta["sha256"]).read_bytes()
    except (FileNotFoundError, ValueError):
        return None
