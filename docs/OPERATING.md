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
| `export-games <folder> [--game <id>] [--include-active]` | research mode: export game records (see below) |
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

## Research mode and the telemetry port

Research mode lets the games played on your server help improve Doctrine's computer
opponent. Turn it on only if your players have agreed to that.

| variable | example | meaning |
|---|---|---|
| `DOCTRINE_RESEARCH` | `on` | research mode. `/server-info` and the sign-in page show the notice, and the game shows it on its Dedicated Server screen |
| `DOCTRINE_RESEARCH_NOTICE` | (optional) | your own notice text; the default says games are kept and may be used, without player names, to improve the computer opponent |
| `DOCTRINE_RESEARCH_PORT` | `8081` | the telemetry listener's port. `0` (the default) = no listener |
| `DOCTRINE_RESEARCH_HOST` | `192.168.5.115` | the address it binds. Use the machine's address on your own network (or `0.0.0.0` inside Docker, with the port published on that address only) |
| `DOCTRINE_RESEARCH_TOKEN` | 32+ random characters | required with a port. Make one with `python -c "import secrets; print(secrets.token_urlsafe(32))"` |

The server refuses to start with a port but no research mode, a token shorter than 16
characters, or the same port as `DOCTRINE_PORT`.

### What a game record is

Each game's record holds its turn files (every move both players uploaded), a custom
game's scenario bundle and a `game.json` with:
- the scenario, seed, first side, status and result;
- the game's passphrase, which decrypts its turn files;
- the turn index: turn, uploader and recipient side, time, the game build that
  uploaded it and the fog-safe summary.

Players appear only as sides 0 and 1, with no names or ids. Games played before
research mode was turned on are included: the server has always kept their turn files.

### The telemetry listener

A second, read-only web app on its own port. Every request needs
`Authorization: Bearer <DOCTRINE_RESEARCH_TOKEN>`; the game never talks to it.

| request | answer |
|---|---|
| `GET /research/healthz` | `{"ok": true, "version": "..."}` |
| `GET /research/games[?include_active=1]` | `{"games": [...]}`: every finished, resigned or closed game (and those in progress with `include_active`), without passphrases |
| `GET /research/games/<game_id>` | the game's full `game.json` |
| `GET /research/games/<game_id>/bundle` | a zip of the game's record |

**Keep it on your own network.** It hands out every game's passphrase, so:
- bind it to the machine's LAN address, or publish it only on that address;
- never add it to your reverse proxy (Caddy) or tunnel (Cloudflare);
- if the machine has a firewall, allow the port from your LAN only.

**Docker.** In `.env` set `DOCTRINE_RESEARCH=on`, `DOCTRINE_RESEARCH_PORT=8081` and
the token. `docker-compose.yml` already sets `DOCTRINE_RESEARCH_HOST=0.0.0.0` inside
the container; publish the port on the host's LAN address only by uncommenting its
`ports` line and putting your address in:

```yaml
    ports:
      - "8080:8080"
      - "192.168.5.115:8081:8081"   # telemetry: this LAN address only
```

**systemd / plain Python.** Add the four variables to the env file, with
`DOCTRINE_RESEARCH_HOST` set to the machine's LAN address, and restart. With ufw:

```sh
sudo ufw allow from 192.168.5.0/24 to any port 8081 proto tcp
```

**Check it** from another machine on the network:

```sh
curl -H "Authorization: Bearer $TOKEN" http://192.168.5.115:8081/research/healthz
curl -H "Authorization: Bearer $TOKEN" http://192.168.5.115:8081/research/games
```

Then give whoever analyses the games the listener's address and the token. Doctrine's
`tools/` has the script that downloads and converts records.

### `export-games`

The same records as folders, without the listener:

```sh
doctrine-server export-games /path/to/folder                  # finished games
doctrine-server export-games /path/to/folder --include-active # and games in progress
doctrine-server export-games /path/to/folder --game rhine-a1b2c3
```

It needs research mode on. Running it again rewrites the folders.

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
