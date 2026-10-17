# Operating a server

## What is in the data directory

```
DOCTRINE_DATA_DIR/
  doctrine.sqlite3      players, hashed API keys, games, turn index, cursors, the
  doctrine.sqlite3-wal  server's random signing secret (WAL mode: keep the -wal/-shm
  doctrine.sqlite3-shm  files together with the database)
  blobs/<game_id>/      turn files, t<turn>_s<recipient>.hexturn
  scenarios/            custom scenario bundles, <sha256>.hexscn
```

## Backups

Run, while the server is up:

```sh
doctrine-server backup /path/to/new-empty-folder
```

It takes a consistent copy of the database (SQLite online backup) and copies the turn
files and scenarios. With Docker:

```sh
docker compose exec doctrine-server doctrine-server backup /data/backups/2026-01-31
docker compose cp doctrine-server:/data/backups/2026-01-31 ./backup-2026-01-31
```

Copying the folder by hand also works if the server is stopped. To restore, stop the
server, replace the data directory with the backup, start it again. Backups contain
password hashes and game passphrases: store them like secrets.

## Upgrades

```sh
# Docker
git pull && docker compose up -d --build
# pip
pip install --upgrade doctrine-server        # or: git pull && pip install .
systemctl restart doctrine-server
```

Database migrations run automatically at startup (`PRAGMA user_version`). Take a
backup first. Downgrading to an older version after a schema change is refused
(the server tells you the database is newer). Games in progress survive restarts:
clients simply retry.

## Admin CLI

All commands accept `--env-file` and `--data-dir`, and operate directly on the
database, so they also work while the server runs.

| command | does |
|---|---|
| `doctrine-server serve` | run the server (the default when no command is given) |
| `players list` | every player with id, kind, status, number of signed-in devices |
| `players disable <name\|id>` | blocks the player: keys stop working, they vanish from the roster, cannot be challenged or sign in |
| `players enable <name\|id>` | undo |
| `players reset-password <name>` | local accounts: prints a temporary password to hand over |
| `keys revoke <name\|id>` | signs out all of a player's devices |
| `games list [--all]` | active games (or all) |
| `games close <game_id>` | ends a stuck or abandoned game (status `closed`) |
| `backup <folder>` | see above |
| `version` | print the version |

If a display name matches several players (Discord names are not unique), use the id
from `players list`.

Erasing a player completely is not automated. With the server stopped (and a backup
taken), open the database with `sqlite3 doctrine.sqlite3` and run:

```sql
PRAGMA foreign_keys = ON;   -- so turns, cursors and keys are removed with their owners
DELETE FROM games WHERE player0 = '<id>' OR player1 = '<id>';
DELETE FROM players WHERE id = '<id>';
```

then delete the matching `blobs/<game_id>/` folders. Their opponents lose those games
too, so consider `players disable` instead.

## Logs

The server logs to stdout (Docker: `docker compose logs`, systemd: `journalctl -u
doctrine-server`). It logs the enabled sign-in methods at startup, account creation,
Discord sign-ins and bot problems, plus uvicorn's access log. It never logs passwords,
API keys, the join code, OAuth secrets or the bot token.

## Security notes

- **Use HTTPS on the internet.** API keys and passwords travel in every request; on a
  plain-HTTP link anyone on the path can read them. Plain HTTP is fine on a LAN you
  trust.
- **Choose a long join code** and change it if it leaks (existing accounts keep
  working; only new registrations need it). Failed join-code and password attempts are
  rate-limited per IP; behind a proxy set `DOCTRINE_FORWARDED_ALLOW_IPS` so the limits
  see real addresses (otherwise every player shares the proxy's address).
- **The operator can read games.** Turn files are encrypted with a per-game passphrase
  that this server stores and hands to both players. Anyone with access to the data
  directory can decrypt them. Say so to your players.
- Stored secrets are hashed: API keys as SHA-256 (they are long random tokens),
  passwords with salted scrypt. The database also holds a random HMAC key used for
  sign-in form tokens and the Discord OAuth state.
- Device sign-in codes expire after 10 minutes and are single-use. The sign-in page
  asks players to check that the code matches the one in their game: a code someone
  else sends you would link *their* game to *your* account.
- Uploads are capped (`DOCTRINE_MAX_TURN_MB`, `DOCTRINE_MAX_SCENARIO_MB`); set your
  proxy's body limit a little above the larger one.
- File names and ids from clients never become paths: turn files are stored under
  server-generated names, scenario bundles by hash.
- Run the server as an unprivileged user (the Docker image and the systemd unit do).
- Report vulnerabilities in the server code as described in [SECURITY.md](../SECURITY.md).
