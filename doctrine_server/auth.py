"""Identity: players, password hashing, API keys, the device-code flow, rate limits.

Pure functions over a sqlite connection (plus a small in-memory rate limiter), so the
HTTP layer stays thin and the CLI can reuse the same code.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass

from . import db
from .errors import ApiError

DEVICE_TTL = 600            # seconds a device code stays valid
DEVICE_INTERVAL = 3         # seconds between client polls
USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no I/O/0/1
NAME_MIN, NAME_MAX = 2, 24
PASSWORD_MIN = 8
PASSWORD_MAX = 256

# Letters/digits (any script), space and a few punctuation marks.
_NAME_RE = re.compile(r"^[\w .'-]+$", re.UNICODE)


def now() -> int:
    return int(time.time())


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- passwords

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                        p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, n, r, p, salt_hex, dk_hex = (stored or "").split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p), dklen=len(dk_hex) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), dk_hex)


# A hash nobody knows the password of; verified against when the name doesn't exist so
# a failed sign-in takes the same time either way.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


# ---------------------------------------------------------------- players

@dataclass
class Player:
    id: str
    kind: str
    username: str
    display_name: str
    disabled: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Player":
        return cls(id=row["id"], kind=row["kind"], username=row["username"],
                   display_name=row["display_name"], disabled=bool(row["disabled"]))


def name_key(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold().strip()


def clean_display_name(name: str) -> str:
    """Validate a local display name; returns the normalized name or raises ApiError."""
    name = " ".join(unicodedata.normalize("NFKC", name or "").split())
    if not (NAME_MIN <= len(name) <= NAME_MAX):
        raise ApiError(400, "bad_name")
    if not _NAME_RE.match(name):
        raise ApiError(400, "bad_name")
    return name


def get_player(conn: sqlite3.Connection, player_id: str) -> Player | None:
    row = conn.execute("SELECT * FROM players WHERE id = ?", (str(player_id),)).fetchone()
    return Player.from_row(row) if row else None


def name_taken(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM players WHERE name_key = ?",
                        (name_key(name),)).fetchone() is not None


def create_local_player(conn: sqlite3.Connection, display_name: str, password: str) -> Player:
    name = clean_display_name(display_name)
    if not (PASSWORD_MIN <= len(password or "") <= PASSWORD_MAX):
        raise ApiError(400, "bad_password")
    pw_hash = hash_password(password)
    pid = "l" + secrets.token_hex(8)
    with db.tx(conn):
        if name_taken(conn, name):
            raise ApiError(409, "name_taken")
        conn.execute(
            "INSERT INTO players (id, kind, username, display_name, name_key, pw_hash, created_at)"
            " VALUES (?, 'local', ?, ?, ?, ?, ?)",
            (pid, name, name, name_key(name), pw_hash, now()))
    return get_player(conn, pid)


def authenticate_local(conn: sqlite3.Connection, display_name: str, password: str) -> Player | None:
    """Name + password check for local accounts. Constant-ish time on unknown names."""
    row = conn.execute("SELECT * FROM players WHERE kind = 'local' AND name_key = ?",
                       (name_key(display_name or ""),)).fetchone()
    if row is None:
        verify_password(password or "", _DUMMY_HASH)
        return None
    if not verify_password(password or "", row["pw_hash"]):
        return None
    return Player.from_row(row)


def upsert_discord_player(conn: sqlite3.Connection, user_id: str, username: str,
                          display_name: str) -> Player:
    user_id = str(user_id)
    display_name = (display_name or username or user_id)[:64]
    username = (username or user_id)[:64]
    with db.tx(conn):
        conn.execute(
            "INSERT INTO players (id, kind, username, display_name, name_key, created_at)"
            " VALUES (?, 'discord', ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET username = excluded.username,"
            " display_name = excluded.display_name, name_key = excluded.name_key",
            (user_id, username, display_name, name_key(display_name), now()))
    return get_player(conn, user_id)


def roster(conn: sqlite3.Connection) -> list[Player]:
    rows = conn.execute("SELECT * FROM players WHERE disabled = 0 ORDER BY name_key").fetchall()
    return [Player.from_row(r) for r in rows]


def find_players(conn: sqlite3.Connection, name_or_id: str) -> list[Player]:
    """Exact id match first, else case-insensitive display-name match (admin CLI)."""
    p = get_player(conn, name_or_id)
    if p:
        return [p]
    rows = conn.execute("SELECT * FROM players WHERE name_key = ?",
                        (name_key(name_or_id),)).fetchall()
    return [Player.from_row(r) for r in rows]


def set_disabled(conn: sqlite3.Connection, player_id: str, disabled: bool) -> None:
    conn.execute("UPDATE players SET disabled = ? WHERE id = ?", (int(disabled), player_id))


def set_password(conn: sqlite3.Connection, player_id: str, password: str) -> None:
    conn.execute("UPDATE players SET pw_hash = ? WHERE id = ? AND kind = 'local'",
                 (hash_password(password), player_id))


# ---------------------------------------------------------------- API keys

def issue_api_key(conn: sqlite3.Connection, player_id: str) -> str:
    """Create a key for one device. Only its sha256 is stored; the token is returned once."""
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO api_keys (key_hash, player_id, created_at) VALUES (?, ?, ?)",
                 (sha256_hex(token), player_id, now()))
    return token


def player_for_key(conn: sqlite3.Connection, token: str) -> Player | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT p.* FROM api_keys k JOIN players p ON p.id = k.player_id WHERE k.key_hash = ?",
        (sha256_hex(token),)).fetchone()
    if row is None or row["disabled"]:
        return None
    return Player.from_row(row)


def touch_key(conn: sqlite3.Connection, token: str) -> None:
    """Record key use, at most once a minute (avoids a write on every request)."""
    t = now()
    conn.execute("UPDATE api_keys SET last_used_at = ? WHERE key_hash = ?"
                 " AND (last_used_at IS NULL OR last_used_at < ?)",
                 (t, sha256_hex(token), t - 60))


def revoke_key(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM api_keys WHERE key_hash = ?", (sha256_hex(token),))


def revoke_all_keys(conn: sqlite3.Connection, player_id: str) -> int:
    return conn.execute("DELETE FROM api_keys WHERE player_id = ?", (player_id,)).rowcount


def key_count(conn: sqlite3.Connection, player_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM api_keys WHERE player_id = ?",
                        (player_id,)).fetchone()[0]


# ---------------------------------------------------------------- device flow

def format_user_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def normalize_user_code(raw: str) -> str:
    """'abcd-efgh ' -> 'ABCDEFGH'; returns '' if it can't be a valid code."""
    code = "".join(ch for ch in (raw or "").upper() if ch.isalnum())
    if len(code) != 8 or any(ch not in USER_CODE_ALPHABET for ch in code):
        return ""
    return code


def start_device(conn: sqlite3.Connection) -> tuple[str, str]:
    """Create a pending device code. Returns (device_code, user_code-without-dash)."""
    t = now()
    conn.execute("DELETE FROM device_codes WHERE expires_at < ?", (t,))
    for _ in range(10):
        device_code = secrets.token_urlsafe(32)
        user_code = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))
        try:
            conn.execute(
                "INSERT INTO device_codes (code_hash, user_code, created_at, expires_at)"
                " VALUES (?, ?, ?, ?)", (sha256_hex(device_code), user_code, t, t + DEVICE_TTL))
            return device_code, user_code
        except sqlite3.IntegrityError:
            continue
    raise ApiError(503, "busy")


def pending_device(conn: sqlite3.Connection, user_code: str) -> sqlite3.Row | None:
    """The device row for a user code that is still waiting for approval."""
    if not user_code:
        return None
    return conn.execute(
        "SELECT * FROM device_codes WHERE user_code = ? AND expires_at >= ? AND player_id IS NULL",
        (user_code, now())).fetchone()


def approve_device(conn: sqlite3.Connection, user_code: str, player_id: str) -> bool:
    cur = conn.execute(
        "UPDATE device_codes SET player_id = ? WHERE user_code = ? AND expires_at >= ?"
        " AND player_id IS NULL", (player_id, user_code, now()))
    return cur.rowcount == 1


def poll_device(conn: sqlite3.Connection, device_code: str) -> dict:
    """pending | expired | approved (once — the code is deleted when the key is issued)."""
    h = sha256_hex(device_code or "")
    with db.tx(conn):
        row = conn.execute("SELECT * FROM device_codes WHERE code_hash = ?", (h,)).fetchone()
        if row is None or row["expires_at"] < now():
            if row is not None:
                conn.execute("DELETE FROM device_codes WHERE code_hash = ?", (h,))
            return {"status": "expired"}
        if row["player_id"] is None:
            return {"status": "pending"}
        player = get_player(conn, row["player_id"])
        conn.execute("DELETE FROM device_codes WHERE code_hash = ?", (h,))
        if player is None or player.disabled:
            return {"status": "expired"}
        api_key = issue_api_key(conn, player.id)
    return {
        "status": "approved",
        "api_key": api_key,
        "discord_uid": player.id,
        "username": player.username,
        "display_name": player.display_name,
    }


# ---------------------------------------------------------------- signing

def sign(secret: bytes, message: str) -> str:
    return hmac.new(secret, message.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_sig(secret: bytes, message: str, sig: str) -> bool:
    return hmac.compare_digest(sign(secret, message), sig or "")


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))


# ---------------------------------------------------------------- rate limiting

class RateLimiter:
    """Sliding-window counter per key (client IP). In memory; resets on restart."""

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, t: float) -> deque:
        q = self._hits.setdefault(key, deque())
        while q and q[0] <= t - self.window:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, time.monotonic())) >= self.limit

    def record(self, key: str) -> None:
        with self._lock:
            t = time.monotonic()
            if len(self._hits) > 10_000:          # keep memory bounded
                for k in [k for k, q in self._hits.items() if not q or q[-1] <= t - self.window]:
                    del self._hits[k]
            self._prune(key, t).append(t)

    def hit(self, key: str) -> bool:
        """Count one attempt; False if the key is over its limit (attempt not counted)."""
        with self._lock:
            t = time.monotonic()
            q = self._prune(key, t)
            if len(q) >= self.limit:
                return False
            q.append(t)
            return True
