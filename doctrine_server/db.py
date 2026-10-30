"""SQLite storage: connection helpers, schema and migrations (PRAGMA user_version).

Every request opens its own short-lived connection; the database runs in WAL mode so
the admin CLI can work on a live server. Writers that need read-check-write
atomicity (turn uploads, device approvals) wrap their work in `tx()`, which takes
the write lock up front (BEGIN IMMEDIATE).
"""
from __future__ import annotations

import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Each entry upgrades the schema by one version. Never edit a shipped entry; append.
MIGRATIONS: list[str] = [
    # v1 — initial schema
    """
    CREATE TABLE server_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE players (
        id           TEXT PRIMARY KEY,          -- Discord snowflake or "l" + 16 hex
        kind         TEXT NOT NULL CHECK (kind IN ('local', 'discord')),
        username     TEXT NOT NULL,
        display_name TEXT NOT NULL,
        name_key     TEXT NOT NULL,             -- casefolded display_name
        pw_hash      TEXT,                      -- local accounts only
        created_at   INTEGER NOT NULL,
        disabled     INTEGER NOT NULL DEFAULT 0
    );
    CREATE UNIQUE INDEX players_local_name ON players(name_key) WHERE kind = 'local';
    CREATE INDEX players_name_key ON players(name_key);

    CREATE TABLE api_keys (
        key_hash     TEXT PRIMARY KEY,          -- sha256 hex of the bearer token
        player_id    TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
        created_at   INTEGER NOT NULL,
        last_used_at INTEGER
    );
    CREATE INDEX api_keys_player ON api_keys(player_id);

    CREATE TABLE device_codes (
        code_hash   TEXT PRIMARY KEY,           -- sha256 hex of device_code
        user_code   TEXT NOT NULL UNIQUE,       -- "ABCDEFGH" (dash added for display)
        created_at  INTEGER NOT NULL,
        expires_at  INTEGER NOT NULL,
        player_id   TEXT REFERENCES players(id) ON DELETE CASCADE  -- set on approval
    );

    CREATE TABLE games (
        game_id         TEXT PRIMARY KEY,
        scenario_id     TEXT NOT NULL,
        scenario_name   TEXT NOT NULL,
        first_side      INTEGER NOT NULL,
        player0         TEXT NOT NULL REFERENCES players(id),
        player1         TEXT NOT NULL REFERENCES players(id),
        status          TEXT NOT NULL,          -- active|resigned|finished|closed
        active_side     INTEGER NOT NULL,
        turn            INTEGER NOT NULL DEFAULT 0,
        winner          INTEGER NOT NULL DEFAULT -1,
        result_level    TEXT NOT NULL DEFAULT '',
        result_reason   TEXT NOT NULL DEFAULT '',
        passphrase      TEXT NOT NULL,
        rng_seed        INTEGER NOT NULL,
        custom          INTEGER NOT NULL DEFAULT 0,
        scenario_sha256 TEXT NOT NULL DEFAULT '',
        channel_id      TEXT NOT NULL DEFAULT '',
        created_at      INTEGER NOT NULL,
        updated_at      INTEGER NOT NULL
    );
    CREATE INDEX games_player0 ON games(player0);
    CREATE INDEX games_player1 ON games(player1);

    CREATE TABLE turns (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id     TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
        turn        INTEGER NOT NULL,
        recipient   INTEGER NOT NULL,           -- side the blob is addressed to
        uploader    INTEGER NOT NULL,
        filename    TEXT NOT NULL,
        summary     TEXT NOT NULL DEFAULT '',
        size        INTEGER NOT NULL,
        uploaded_at INTEGER NOT NULL,
        UNIQUE (game_id, turn, recipient)
    );

    CREATE TABLE cursors (                      -- per-(game, player) download cursor
        game_id   TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
        player_id TEXT NOT NULL,
        cursor    INTEGER NOT NULL,
        PRIMARY KEY (game_id, player_id)
    );

    CREATE TABLE scenario_files (               -- content-addressed custom maps
        sha256      TEXT PRIMARY KEY,
        size        INTEGER NOT NULL,
        uploaded_by TEXT NOT NULL,
        uploaded_at INTEGER NOT NULL
    );

    CREATE TABLE scenarios (                    -- scenario_id -> newest sha
        scenario_id TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        sha256      TEXT NOT NULL REFERENCES scenario_files(sha256),
        uploaded_by TEXT NOT NULL,
        updated_at  INTEGER NOT NULL
    );
    """,
    # v2 — the game build that uploaded each turn (research exports)
    """
    ALTER TABLE turns ADD COLUMN client_version TEXT NOT NULL DEFAULT '';
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)


def connect(path: str | Path) -> sqlite3.Connection:
    """A connection in autocommit mode (explicit transactions via `tx`)."""
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the resulting schema version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{current} is newer than this server (v{SCHEMA_VERSION}); "
            "upgrade doctrine-server"
        )
    for version in range(current, SCHEMA_VERSION):
        conn.executescript("BEGIN;\n" + MIGRATIONS[version]
                           + f"\nPRAGMA user_version = {version + 1};\nCOMMIT;")
    return SCHEMA_VERSION


def init(data_dir: str | Path) -> Path:
    """Create the data dir, enable WAL, migrate, and make sure a signing secret exists."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "blobs").mkdir(exist_ok=True)
    (data_dir / "scenarios").mkdir(exist_ok=True)
    path = data_dir / "doctrine.sqlite3"
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        migrate(conn)
        conn.execute("INSERT OR IGNORE INTO server_meta (key, value) VALUES ('secret', ?)",
                     (secrets.token_hex(32),))
    finally:
        conn.close()
    return path


def get_secret(conn: sqlite3.Connection) -> bytes:
    """The server's random HMAC key (CSRF tokens, OAuth state). Generated on first start."""
    row = conn.execute("SELECT value FROM server_meta WHERE key = 'secret'").fetchone()
    return bytes.fromhex(row["value"])
