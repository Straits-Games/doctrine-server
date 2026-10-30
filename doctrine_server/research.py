"""Research mode: game records for improving Doctrine's computer opponent.

The server already keeps every turn file and each game's passphrase. In research mode
(DOCTRINE_RESEARCH=on, which tells players so) the operator can hand them out, either
with `export-games` or through the telemetry listener (`research_app`). A game's
record is:

    game.json         the game, its result, its passphrase and its turn index
    t<N>_s<S>.hexturn every turn file, as uploaded
    scenario.hexscn   a custom game's scenario bundle

Players appear only as sides 0 and 1: no names, ids or sign-in details.
"""
from __future__ import annotations

import io
import json
import shutil
import sqlite3
import zipfile
from pathlib import Path
from typing import Iterator

from . import __version__, games, scenarios

FORMAT = 1
CONCLUDED = ("finished", "resigned", "closed")


def game_rows(conn: sqlite3.Connection, include_active: bool = False) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT * FROM games ORDER BY created_at").fetchall()
    return [g for g in rows if include_active or g["status"] in CONCLUDED]


def game_record(conn: sqlite3.Connection, game: sqlite3.Row, server_name: str) -> dict:
    turns = conn.execute(
        "SELECT turn, recipient, uploader, summary, size, uploaded_at, client_version"
        " FROM turns WHERE game_id = ? ORDER BY id", (game["game_id"],)).fetchall()
    return {
        "format": FORMAT,
        "server": {"name": server_name, "version": __version__},
        "game_id": game["game_id"],
        "scenario_id": game["scenario_id"],
        "scenario_name": game["scenario_name"],
        "custom": bool(game["custom"]),
        "scenario_sha256": game["scenario_sha256"],
        "first_side": game["first_side"],
        "rng_seed": game["rng_seed"],
        "passphrase": game["passphrase"],
        "status": game["status"],
        "winner": game["winner"],
        "result_level": game["result_level"],
        "result_reason": game["result_reason"],
        "turn": game["turn"],
        "created_at": game["created_at"],
        "updated_at": game["updated_at"],
        "turns": [{
            "turn": t["turn"], "uploader": t["uploader"], "recipient": t["recipient"],
            "file": f"t{int(t['turn'])}_s{int(t['recipient'])}.hexturn",
            "size": t["size"], "uploaded_at": t["uploaded_at"],
            "client_version": t["client_version"], "summary": t["summary"],
        } for t in turns],
    }


def summary(record: dict) -> dict:
    """A record for listings: no passphrase, and the turn count instead of the index."""
    out = {k: v for k, v in record.items() if k not in ("passphrase", "turns")}
    out["turns"] = len(record["turns"])
    return out


def _files(data_dir: Path, game: sqlite3.Row, record: dict) -> Iterator[tuple[str, Path]]:
    """(name in the record, path on disk) for every file that still exists. A turn
    file lost on disk keeps its index entry with an empty `file`."""
    for t in record["turns"]:
        src = games.blob_path(data_dir, game["game_id"], t["turn"], t["recipient"])
        if src.is_file():
            yield t["file"], src
        else:
            t["file"] = ""
    if game["custom"] and game["scenario_sha256"]:
        src = scenarios.file_path(data_dir, game["scenario_sha256"])
        if src.is_file():
            yield "scenario.hexscn", src


def bundle(conn: sqlite3.Connection, data_dir: Path, game: sqlite3.Row, server_name: str) -> bytes:
    """One game's record as a zip (game.json + its files)."""
    record = game_record(conn, game, server_name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, src in list(_files(Path(data_dir), game, record)):
            z.write(src, name)
        z.writestr("game.json", json.dumps(record, indent=2) + "\n")
    return buf.getvalue()


def export_games(conn: sqlite3.Connection, data_dir: Path, out: Path, server_name: str,
                 game_id: str | None = None, include_active: bool = False) -> dict:
    """Write games to `out`, one folder each. Returns {"exported": [ids], "skipped": {id: why}}."""
    data_dir = Path(data_dir)
    out = Path(out)
    if game_id is not None:
        row = games.load_game(conn, game_id) if games.valid_game_id(game_id) else None
        if row is None:
            return {"exported": [], "skipped": {game_id: "no such game"}}
        rows = [row]
    else:
        rows = game_rows(conn, include_active=True)
    done: list[str] = []
    skipped: dict[str, str] = {}
    for game in rows:
        if game["status"] not in CONCLUDED and not include_active:
            skipped[game["game_id"]] = "not over"
            continue
        record = game_record(conn, game, server_name)
        if not record["turns"]:
            skipped[game["game_id"]] = "no turns"
            continue
        dest = out / game["game_id"]
        dest.mkdir(parents=True, exist_ok=True)
        for name, src in list(_files(data_dir, game, record)):
            shutil.copyfile(src, dest / name)
        (dest / "game.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        done.append(game["game_id"])
    return {"exported": done, "skipped": skipped}
