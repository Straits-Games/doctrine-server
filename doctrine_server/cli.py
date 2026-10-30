"""`doctrine-server` command line: run the server, or administer its database.

Admin commands work directly on the SQLite database in DOCTRINE_DATA_DIR and are
safe to run while the server is up (WAL mode).
"""
from __future__ import annotations

import argparse
import logging
import secrets
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from . import __version__, auth, db, games, research
from .config import Config, ConfigError


def _common(parser: argparse.ArgumentParser, suppress: bool) -> None:
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument("--env-file", default=default,
                        help="read settings from this env file (default: ./.env if present)")
    parser.add_argument("--data-dir", default=default,
                        help="override DOCTRINE_DATA_DIR")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="doctrine-server",
                                description="Doctrine dedicated server (turn relay + game list)")
    _common(p, suppress=False)
    sub = p.add_subparsers(dest="cmd")

    serve = sub.add_parser("serve", help="run the server (default)")
    _common(serve, suppress=True)
    serve.add_argument("--host", help="override DOCTRINE_HOST")
    serve.add_argument("--port", type=int, help="override DOCTRINE_PORT")

    sub.add_parser("version", help="print the version")

    players = sub.add_parser("players", help="manage players")
    psub = players.add_subparsers(dest="action", required=True)
    _common(psub.add_parser("list", help="list all players"), suppress=True)
    for name, help_ in (("disable", "disable a player (they can no longer sign in or play)"),
                        ("enable", "re-enable a disabled player"),
                        ("reset-password", "set a temporary password for a local account")):
        sp = psub.add_parser(name, help=help_)
        sp.add_argument("who", help="display name or player id")
        _common(sp, suppress=True)

    g = sub.add_parser("games", help="manage games")
    gsub = g.add_subparsers(dest="action", required=True)
    gl = gsub.add_parser("list", help="list active games")
    gl.add_argument("--all", action="store_true", help="include concluded games")
    _common(gl, suppress=True)
    gc = gsub.add_parser("close", help="close a game")
    gc.add_argument("game_id")
    _common(gc, suppress=True)

    k = sub.add_parser("keys", help="manage API keys")
    ksub = k.add_subparsers(dest="action", required=True)
    kr = ksub.add_parser("revoke", help="revoke all of a player's API keys (signs out every device)")
    kr.add_argument("who", help="display name or player id")
    _common(kr, suppress=True)

    b = sub.add_parser("backup", help="copy the database, turn files and scenarios to a new folder")
    b.add_argument("dest", help="destination folder (must not exist or be empty)")
    _common(b, suppress=True)

    x = sub.add_parser("export-games",
                       help="research mode: export game records for AI research")
    x.add_argument("dest", help="destination folder (one subfolder per game)")
    x.add_argument("--game", help="export only this game")
    x.add_argument("--include-active", action="store_true",
                   help="include games still in progress")
    _common(x, suppress=True)
    return p


def _config(args) -> Config:
    config = Config.from_env(getattr(args, "env_file", None))
    if getattr(args, "data_dir", None):
        config.data_dir = Path(args.data_dir).resolve()
    return config


def _open(config: Config):
    return db.connect(db.init(config.data_dir))


def _one_player(conn, who: str) -> auth.Player:
    found = auth.find_players(conn, who)
    if not found:
        raise SystemExit(f"error: no player matches {who!r}")
    if len(found) > 1:
        ids = ", ".join(p.id for p in found)
        raise SystemExit(f"error: {who!r} matches several players ({ids}); use the id")
    return found[0]


def _date(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _print_table(rows: list[list[str]], header: list[str]) -> None:
    widths = [max(len(str(r[i])) for r in [header] + rows) for i in range(len(header))]
    for r in [header] + rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())


def cmd_players(args, conn) -> int:
    if args.action == "list":
        rows = conn.execute("SELECT * FROM players ORDER BY name_key").fetchall()
        _print_table([[r["id"], r["kind"], r["display_name"],
                       "disabled" if r["disabled"] else "ok",
                       str(auth.key_count(conn, r["id"])), _date(r["created_at"])] for r in rows],
                     ["id", "kind", "name", "status", "keys", "created"])
        return 0
    player = _one_player(conn, args.who)
    if args.action == "disable":
        auth.set_disabled(conn, player.id, True)
        print(f"disabled {player.display_name} ({player.id})")
    elif args.action == "enable":
        auth.set_disabled(conn, player.id, False)
        print(f"enabled {player.display_name} ({player.id})")
    elif args.action == "reset-password":
        if player.kind != "local":
            raise SystemExit("error: only local accounts have passwords (this is a Discord account)")
        temp = secrets.token_urlsafe(9)
        auth.set_password(conn, player.id, temp)
        print(f"temporary password for {player.display_name}: {temp}")
        print("They should sign in with it; existing devices stay signed in "
              "(use `keys revoke` to sign them out).")
    return 0


def cmd_games(args, conn) -> int:
    if args.action == "list":
        rows = games.all_games(conn, include_concluded=args.all)
        names = {r["id"]: r["display_name"] for r in conn.execute("SELECT id, display_name FROM players")}
        _print_table([[g["game_id"], g["status"], f"{names.get(g['player0'], g['player0'])} vs "
                       f"{names.get(g['player1'], g['player1'])}", str(g["turn"]),
                       str(g["active_side"]), _date(g["updated_at"])] for g in rows],
                     ["game", "status", "players", "turn", "to move", "updated"])
        return 0
    if args.action == "close":
        if games.close_game(conn, args.game_id):
            print(f"closed {args.game_id}")
            return 0
        print(f"error: no active game {args.game_id!r}", file=sys.stderr)
        return 1
    return 2


def cmd_keys(args, conn) -> int:
    player = _one_player(conn, args.who)
    n = auth.revoke_all_keys(conn, player.id)
    print(f"revoked {n} key(s) for {player.display_name} ({player.id})")
    return 0


def cmd_backup(args, config: Config) -> int:
    dest = Path(args.dest).resolve()
    if dest.exists() and any(dest.iterdir()):
        print(f"error: {dest} is not empty", file=sys.stderr)
        return 1
    dest.mkdir(parents=True, exist_ok=True)
    src = db.connect(db.init(config.data_dir))
    out = sqlite3.connect(str(dest / "doctrine.sqlite3"))
    try:
        src.backup(out)        # consistent online copy, safe while the server runs
    finally:
        out.close()
        src.close()
    for sub in ("blobs", "scenarios"):
        shutil.copytree(config.data_dir / sub, dest / sub, dirs_exist_ok=True)
    print(f"backed up {config.data_dir} to {dest}")
    return 0


def cmd_export(args, config: Config) -> int:
    if not config.research:
        print("error: research mode is off (set DOCTRINE_RESEARCH=on, which also tells "
              "players their games are kept for AI research)", file=sys.stderr)
        return 2
    conn = _open(config)
    try:
        result = research.export_games(conn, config.data_dir, Path(args.dest).resolve(),
                                       config.server_name, game_id=args.game,
                                       include_active=args.include_active)
    finally:
        conn.close()
    for game_id in result["exported"]:
        print(f"exported {game_id}")
    for game_id, why in result["skipped"].items():
        print(f"skipped {game_id}: {why}")
    print(f"{len(result['exported'])} game(s) exported to {Path(args.dest).resolve()}")
    return 1 if args.game and not result["exported"] else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.cmd or "serve"
    if cmd == "version":
        print(__version__)
        return 0
    try:
        config = _config(args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if cmd == "serve":
        if getattr(args, "host", None):
            config.host = args.host
        if getattr(args, "port", None):
            config.port = args.port
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        try:
            from .main import run
            run(config)
        except ConfigError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return 0

    if cmd == "backup":
        return cmd_backup(args, config)
    if cmd == "export-games":
        return cmd_export(args, config)

    conn = _open(config)
    try:
        return {"players": cmd_players, "games": cmd_games, "keys": cmd_keys}[cmd](args, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
