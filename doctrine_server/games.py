"""Games and the turn relay: pure functions over the database + the blob directory.

Turn numbering (fixed by the game client): `turn` increments once per FULL ROUND, so
both player-turns of a round can carry the same number, and turn 0 is the setup /
deployment phase in which each side may upload a sealed setup file (defender first).
The rules below exist because an earlier server deadlocked in three ways:

  1. it refused the defender's turn-0 upload when the attacker was `first_side`
     (turn 0 is now exempt from the turn-ownership check);
  2. a client restarted mid-setup could not re-fetch the turn-0 file it had already
     downloaded (turn-0 files addressed to the side to move are re-delivered);
  3. it bumped the uploader's download cursor on upload, so a reply carrying the
     same turn number was never delivered (cursors now move on DOWNLOAD only).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import string
from dataclasses import dataclass
from pathlib import Path

from . import auth, db, scenarios
from .errors import ApiError

ID_RE = scenarios.ID_RE                                     # scenario ids
GAME_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}-[a-z0-9]{6}$")
_FILENAME_BAD = re.compile(r"[^A-Za-z0-9._-]")
_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits
STATUSES = ("active", "resigned", "finished", "closed")
SUMMARY_MAX = 500


def valid_scenario_id(value) -> bool:
    return isinstance(value, str) and bool(ID_RE.match(value))


def valid_game_id(value) -> bool:
    return isinstance(value, str) and bool(GAME_ID_RE.match(value))


def _side_value(value) -> int | None:
    """0/1 from JSON (ints only — bools and strings are rejected)."""
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1):
        return None
    return value


def _int_value(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def default_scenario_name(scenario_id: str) -> str:
    return scenario_id.replace("_", " ").replace("-", " ").title()


def _clean_text(value, limit: int) -> str:
    text = "" if value is None else str(value)
    text = "".join(ch for ch in text if ch == "\n" or ch.isprintable())
    return text.strip()[:limit]


def sanitize_filename(name: str, fallback: str) -> str:
    """Basename only, [A-Za-z0-9._-], <= 96 chars, no leading dot; else the fallback."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _FILENAME_BAD.sub("", base)[:96]
    if not base or base.startswith(".") or set(base) <= {"."}:
        return fallback
    return base


def blob_path(data_dir: Path, game_id: str, turn: int, recipient: int) -> Path:
    # Every component is server-controlled: a validated game id and two ints.
    return Path(data_dir) / "blobs" / game_id / f"t{int(turn)}_s{int(recipient)}.hexturn"


# ---------------------------------------------------------------- records

def load_game(conn: sqlite3.Connection, game_id: str) -> sqlite3.Row | None:
    if not valid_game_id(game_id):
        return None
    return conn.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()


def side_of(game: sqlite3.Row, player_id: str) -> int | None:
    if game["player0"] == player_id:
        return 0
    if game["player1"] == player_id:
        return 1
    return None


def game_for_player(conn: sqlite3.Connection, game_id: str, player_id: str):
    """(game row, caller side) or ApiError 404 for unknown games and non-participants."""
    game = load_game(conn, game_id)
    side = side_of(game, player_id) if game is not None else None
    if side is None:
        raise ApiError(404, "not_found")
    return game, side


def _has_blob(conn, game_id: str, turn: int, *, recipient: int | None = None,
              uploader: int | None = None) -> bool:
    sql = "SELECT 1 FROM turns WHERE game_id = ? AND turn = ?"
    args: list = [game_id, turn]
    if recipient is not None:
        sql += " AND recipient = ?"
        args.append(recipient)
    if uploader is not None:
        sql += " AND uploader = ?"
        args.append(uploader)
    return conn.execute(sql, args).fetchone() is not None


def your_turn(conn: sqlite3.Connection, game: sqlite3.Row, side: int) -> bool:
    """Whether `side` should act now.

    Normally: the game is active and it is that side's move. During setup (turn 0) a
    side that has not uploaded its own setup yet may also act if it is the first
    placer (1 - first_side) or a setup file addressed to it already exists.
    """
    if game["status"] != "active":
        return False
    if game["active_side"] == side:
        return True
    if game["turn"] == 0 and not _has_blob(conn, game["game_id"], 0, uploader=side):
        if side == 1 - game["first_side"] or _has_blob(conn, game["game_id"], 0, recipient=side):
            return True
    return False


def _names(conn, game) -> dict[str, str]:
    out = {}
    for s in (0, 1):
        p = auth.get_player(conn, game[f"player{s}"])
        out[str(s)] = p.display_name if p else game[f"player{s}"]
    return out


def game_dict(conn: sqlite3.Connection, game: sqlite3.Row, side: int,
              with_passphrase: bool = False) -> dict:
    d = {
        "game_id": game["game_id"],
        "scenario_id": game["scenario_id"],
        "scenario_name": game["scenario_name"],
        "status": game["status"],
        "your_side": side,
        "active_side": game["active_side"],
        "your_turn": your_turn(conn, game, side),
        "turn": game["turn"],
        "players": {"0": game["player0"], "1": game["player1"]},
        "names": _names(conn, game),
        "channel_id": game["channel_id"] or "",
        "winner": game["winner"],
        "updated_at": game["updated_at"],
        "first_side": game["first_side"],
        "rng_seed": game["rng_seed"],
        "custom": bool(game["custom"]),
        "scenario_sha256": game["scenario_sha256"] or "",
    }
    if with_passphrase:
        d["passphrase"] = game["passphrase"]
    return d


def notify_view(conn: sqlite3.Connection, game_id: str) -> dict | None:
    """A plain snapshot of a game for notifiers (no secrets; int side keys)."""
    game = conn.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
    if game is None:
        return None
    players = {}
    for s in (0, 1):
        p = auth.get_player(conn, game[f"player{s}"])
        players[s] = {
            "id": game[f"player{s}"],
            "name": p.display_name if p else game[f"player{s}"],
            "kind": p.kind if p else "local",
        }
    return {
        "game_id": game["game_id"],
        "scenario_id": game["scenario_id"],
        "scenario_name": game["scenario_name"],
        "status": game["status"],
        "turn": game["turn"],
        "active_side": game["active_side"],
        "first_side": game["first_side"],
        "winner": game["winner"],
        "result_level": game["result_level"],
        "result_reason": game["result_reason"],
        "channel_id": game["channel_id"] or "",
        "players": players,
    }


# ---------------------------------------------------------------- create / list

def create_game(conn: sqlite3.Connection, caller_id: str, body: dict) -> sqlite3.Row:
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request")
    scenario_id = body.get("scenario_id")
    if not valid_scenario_id(scenario_id):
        raise ApiError(400, "bad_scenario")
    my_side = _side_value(body.get("my_side"))
    if my_side is None:
        raise ApiError(400, "bad_side")
    first_side = _side_value(body.get("first_side", 0))
    if first_side is None:
        raise ApiError(400, "bad_first_side")
    opponent_id = body.get("opponent_uid")
    # ids are strings on the wire; tolerate a bare int, never a bool
    if isinstance(opponent_id, int) and not isinstance(opponent_id, bool):
        opponent_id = str(opponent_id)
    opponent = auth.get_player(conn, opponent_id) if isinstance(opponent_id, str) and opponent_id else None
    if opponent is None or opponent.disabled or opponent.id == caller_id:
        raise ApiError(400, "bad_opponent")
    custom = body.get("custom") is True
    sha = body.get("scenario_sha256") or ""
    if not isinstance(sha, str):
        raise ApiError(400, "scenario_missing")
    sha = sha.lower()
    if custom:
        if not scenarios.file_exists(conn, sha):
            raise ApiError(400, "scenario_missing")
    name = _clean_text(body.get("scenario_name"), 64) or default_scenario_name(scenario_id)

    players = {my_side: caller_id, 1 - my_side: opponent.id}
    t = auth.now()
    for _ in range(20):
        suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(6))
        game_id = f"{scenario_id}-{suffix}"
        try:
            conn.execute(
                "INSERT INTO games (game_id, scenario_id, scenario_name, first_side, player0,"
                " player1, status, active_side, turn, winner, passphrase, rng_seed, custom,"
                " scenario_sha256, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'active', ?, 0, -1, ?, ?, ?, ?, ?, ?)",
                (game_id, scenario_id, name, first_side, players[0], players[1],
                 1 - first_side,                      # setup is defender-first
                 secrets.token_urlsafe(18)[:24],      # 24 urlsafe chars
                 secrets.randbelow(2 ** 31 - 1) + 1,  # nonzero 31-bit seed
                 int(custom), sha if scenarios.valid_sha(sha) else "", t, t))
            return load_game(conn, game_id)
        except sqlite3.IntegrityError:
            continue
    raise ApiError(503, "busy")


def list_games(conn: sqlite3.Connection, player_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM games WHERE player0 = ? OR player1 = ? ORDER BY updated_at DESC, rowid DESC",
        (player_id, player_id)).fetchall()
    return [game_dict(conn, g, side_of(g, player_id)) for g in rows]


def all_games(conn: sqlite3.Connection, include_concluded: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM games"
    if not include_concluded:
        sql += " WHERE status = 'active'"
    return conn.execute(sql + " ORDER BY updated_at DESC").fetchall()


def set_channel_id(conn: sqlite3.Connection, game_id: str, channel_id: str) -> None:
    conn.execute("UPDATE games SET channel_id = ? WHERE game_id = ?", (channel_id, game_id))


# ---------------------------------------------------------------- turns

@dataclass
class TurnBlob:
    data: bytes
    filename: str
    turn: int
    side: int          # recipient side


def parse_meta(raw) -> dict:
    try:
        meta = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (ValueError, TypeError):
        raise ApiError(400, "bad_meta") from None
    if not isinstance(meta, dict):
        raise ApiError(400, "bad_meta")
    return meta


def upload_turn(conn: sqlite3.Connection, data_dir: Path, game_id: str, player_id: str,
                meta: dict, data: bytes, filename: str) -> dict:
    turn = _int_value(meta.get("turn"))
    new_active = _int_value(meta.get("active_side"))
    if turn is None or new_active is None:
        raise ApiError(400, "bad_meta")
    summary = _clean_text(meta.get("summary"), SUMMARY_MAX)

    with db.tx(conn):
        game, side = game_for_player(conn, game_id, player_id)
        if game["status"] != "active":
            raise ApiError(409, "not_active")
        # 1. the file hands the move to the other side
        if new_active != 1 - side:
            raise ApiError(400, "bad_active_side")
        # 2. after setup you may only upload on your own move (turn 0 is exempt)
        if turn >= 1 and game["active_side"] != side:
            raise ApiError(409, "out_of_turn")
        recipient = 1 - side
        # 3. one file per (turn, uploader side)
        if _has_blob(conn, game_id, turn, recipient=recipient):
            raise ApiError(409, "duplicate_turn")
        # 4. lenient staleness check — never trips on the normal flow
        if turn < 0 or turn < game["turn"] - 1:
            raise ApiError(400, "stale_turn")
        # 5. store
        path = blob_path(data_dir, game_id, turn, recipient)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        stored_name = sanitize_filename(filename, f"{game_id}_t{turn}_s{recipient}.hexturn")
        t = auth.now()
        conn.execute(
            "INSERT INTO turns (game_id, turn, recipient, uploader, filename, summary, size,"
            " uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (game_id, turn, recipient, side, stored_name, summary, len(data), t))
        # 6. hand the move over. 7. NO cursor changes here (that was deadlock #3).
        new_turn = max(game["turn"], turn)
        conn.execute("UPDATE games SET active_side = ?, turn = ?, updated_at = ? WHERE game_id = ?",
                     (new_active, new_turn, t, game_id))
    return {"ok": True, "turn": new_turn, "active_side": new_active,
            "recipient": recipient, "summary": summary}


def get_cursor(conn: sqlite3.Connection, game_id: str, player_id: str) -> int:
    row = conn.execute("SELECT cursor FROM cursors WHERE game_id = ? AND player_id = ?",
                       (game_id, player_id)).fetchone()
    return row["cursor"] if row else -1


def fetch_turn(conn: sqlite3.Connection, data_dir: Path, game_id: str, player_id: str,
               since: int | None = None) -> TurnBlob | None:
    """The blob to hand the caller, or None (HTTP 204)."""
    with db.tx(conn):
        game, side = game_for_player(conn, game_id, player_id)
        row = None
        # Setup re-delivery (deadlock #2): while it is still the setup phase and this
        # side is to move, its turn-0 file is served regardless of the cursor.
        if game["turn"] == 0 and game["active_side"] == side:
            row = conn.execute(
                "SELECT * FROM turns WHERE game_id = ? AND turn = 0 AND recipient = ?"
                " ORDER BY id DESC LIMIT 1", (game_id, side)).fetchone()
        stored = get_cursor(conn, game_id, player_id)
        if row is None:
            cursor = since if since is not None and since >= 0 else stored
            row = conn.execute(
                "SELECT * FROM turns WHERE game_id = ? AND recipient = ? AND turn > ?"
                " ORDER BY turn DESC, uploaded_at DESC, id DESC LIMIT 1",
                (game_id, side, cursor)).fetchone()
        if row is None:
            return None
        path = blob_path(data_dir, game_id, row["turn"], row["recipient"])
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise ApiError(410, "blob_missing") from None
        # The cursor advances on download only.
        conn.execute(
            "INSERT INTO cursors (game_id, player_id, cursor) VALUES (?, ?, ?)"
            " ON CONFLICT(game_id, player_id) DO UPDATE SET cursor = MAX(cursor, excluded.cursor)",
            (game_id, player_id, row["turn"]))
    return TurnBlob(data=data, filename=row["filename"], turn=row["turn"], side=row["recipient"])


# ---------------------------------------------------------------- endings

def resign(conn: sqlite3.Connection, game_id: str, player_id: str) -> tuple[sqlite3.Row, int, bool]:
    """Returns (game, caller side, changed). Idempotent on concluded games."""
    with db.tx(conn):
        game, side = game_for_player(conn, game_id, player_id)
        changed = game["status"] == "active"
        if changed:
            conn.execute(
                "UPDATE games SET status = 'resigned', winner = ?, result_reason = 'resigned',"
                " updated_at = ? WHERE game_id = ?", (1 - side, auth.now(), game_id))
    return load_game(conn, game_id), side, changed


def report_result(conn: sqlite3.Connection, game_id: str, player_id: str,
                  body) -> tuple[sqlite3.Row, int, bool]:
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request")
    winner = _side_value(body.get("winner"))
    if winner is None:
        raise ApiError(400, "bad_winner")
    level = _clean_text(body.get("level"), 64)
    reason = _clean_text(body.get("reason"), 200)
    with db.tx(conn):
        game, side = game_for_player(conn, game_id, player_id)
        changed = game["status"] == "active"           # first report wins
        if changed:
            conn.execute(
                "UPDATE games SET status = 'finished', winner = ?, result_level = ?,"
                " result_reason = ?, updated_at = ? WHERE game_id = ?",
                (winner, level, reason, auth.now(), game_id))
    return load_game(conn, game_id), side, changed


def close_game(conn: sqlite3.Connection, game_id: str) -> bool:
    """Admin close (CLI / Discord). True if the game existed and was still active."""
    cur = conn.execute(
        "UPDATE games SET status = 'closed', updated_at = ? WHERE game_id = ? AND status = 'active'",
        (auth.now(), game_id))
    return cur.rowcount == 1
