# doctrine-server

A self-hostable **dedicated server** for *Doctrine: Squad-Based Tactics*. Run one for
your friends, club or community: it holds the list of players and games and relays
the encrypted turn files between the two players of each game, so nobody has to swap
files by hand. An optional Discord plugin adds "Sign in with Discord", turn pings,
per-game channels and slash commands.

> **Community hosting.** Straits Games does not run, monitor or support any Doctrine
> server; whoever runs a server is responsible for it and for the data on it.

- Python 3.11+, FastAPI, SQLite. No external database or cache.
- Two ways to sign in: local accounts (display name + password, gated by a join code)
  and/or Discord. At least one must be configured.
- The REST contract the game speaks is documented in [docs/PROTOCOL.md](docs/PROTOCOL.md)
  (protocol 1).

## Quick start

### Docker (recommended)

```sh
git clone <this repository> doctrine-server && cd doctrine-server
cp .env.example .env        # edit: set DOCTRINE_JOIN_CODE (and/or the Discord keys)
docker compose up -d --build
docker compose logs -f
```

The server listens on port 8080. Data lives in the `doctrine-data` Docker volume.

### Plain Python (Linux, Windows, macOS)

```sh
python3 -m venv .venv
.venv/bin/pip install .                 # Windows: .venv\Scripts\pip install .
                                        # add the Discord plugin with: pip install ".[discord]"
cp .env.example .env                    # edit it
.venv/bin/doctrine-server serve         # Windows: .venv\Scripts\doctrine-server serve
```

`doctrine-server` reads `.env` from the current folder (or `--env-file path`); real
environment variables win over the file. For a Linux service, see
[deploy/doctrine-server.service](deploy/doctrine-server.service).

## Exposing it

- **LAN:** plain `http://<this-machine>:8080` works. Leave `DOCTRINE_PUBLIC_URL` empty
  and sign-in links use whatever address the game connected to.
- **Internet:** put HTTPS in front and set `DOCTRINE_PUBLIC_URL=https://your.domain`.
  - [Caddy](https://caddyserver.com/) gets certificates automatically: see
    [deploy/Caddyfile.example](deploy/Caddyfile.example) (and the commented Caddy
    service in `docker-compose.yml`).
  - [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
    works without opening ports: point a tunnel at `http://localhost:8080`.
  - Behind any proxy, set `DOCTRINE_FORWARDED_ALLOW_IPS` to the proxy's address so
    per-IP rate limits see real client addresses.

## Connecting from the game

In Doctrine choose **Multiplayer > Dedicated Server**, enter the server address
(for example `https://doctrine.example.org` or `http://192.0.2.10:8080`) and press
**Sign in**. Your browser opens the server's sign-in page showing the same code as the
game; create an account with the join code (or sign in with Discord) and the game
finishes linking by itself. Then pick an opponent from the roster and challenge them.

## Discord plugin (optional)

Install with `pip install "doctrine-server[discord]"` (the Docker image already has
it) and set the `DISCORD_*` variables. Setup, permissions and what the bot does:
[docs/DISCORD.md](docs/DISCORD.md).

## Configuration

All settings are environment variables (or lines in `.env`). See
[.env.example](.env.example).

| variable | default | meaning |
|---|---|---|
| `DOCTRINE_PUBLIC_URL` | (empty) | external base URL, e.g. `https://doctrine.example.org`. Builds the sign-in link and the Discord OAuth redirect. Required for Discord sign-in; on a LAN it may stay empty |
| `DOCTRINE_HOST` / `DOCTRINE_PORT` | `0.0.0.0` / `8080` | bind address |
| `DOCTRINE_DATA_DIR` | `./data` | SQLite database, turn files, custom scenarios |
| `DOCTRINE_SERVER_NAME` | `Doctrine server` | shown on the sign-in pages and `/server-info` |
| `DOCTRINE_JOIN_CODE` | (empty) | enables local accounts; required to *create* one (not to sign in again) |
| `DOCTRINE_MOTD` | (empty) | message on the sign-in page |
| `DOCTRINE_RESEARCH` | off | research mode: tell players this server keeps game records for AI research, and allow `export-games` and the telemetry listener (see [docs/OPERATING.md](docs/OPERATING.md#research-mode-and-the-telemetry-port)) |
| `DOCTRINE_RESEARCH_NOTICE` | (a standard notice) | the research notice shown in the game and on the sign-in page |
| `DOCTRINE_RESEARCH_HOST` / `DOCTRINE_RESEARCH_PORT` | `127.0.0.1` / `0` (off) | the telemetry listener's bind address and port: your own network only, never the public proxy |
| `DOCTRINE_RESEARCH_TOKEN` | (empty) | bearer token the telemetry listener requires (at least 16 characters) |
| `DOCTRINE_MAX_TURN_MB` / `DOCTRINE_MAX_SCENARIO_MB` | `8` / `32` | upload caps (HTTP 413 above) |
| `DOCTRINE_FORWARDED_ALLOW_IPS` | `127.0.0.1` | proxies trusted to send `X-Forwarded-For` (`*` = any) |
| `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` | (empty) | enable "Sign in with Discord" |
| `DISCORD_BOT_TOKEN` | (empty) | enable the bot |
| `DISCORD_GUILD_ID` | (empty) | your Discord server; with the bot, Discord sign-in requires membership |
| `DISCORD_CHANNEL_ID` | (empty) | lobby channel for challenges and results |
| `DISCORD_GAMES_CATEGORY` | (empty) | one channel per game under this category; ended games move to "Archive" |
| `DISCORD_INVITE_URL` | (empty) | invite link shown in `/server-info` and to non-members |

The server refuses to start if neither `DOCTRINE_JOIN_CODE` nor the Discord OAuth
keys are set.

## Running it

Admin commands work on the database directly, also while the server runs:

```sh
doctrine-server players list
doctrine-server players disable <name|id>     # and: players enable <name|id>
doctrine-server players reset-password <name> # prints a temporary password
doctrine-server keys revoke <name|id>         # signs out all of a player's devices
doctrine-server games list [--all]
doctrine-server games close <game_id>
doctrine-server backup <empty-folder>
doctrine-server export-games <folder>         # research mode: game records
doctrine-server version
```

Backups, upgrades and security notes: [docs/OPERATING.md](docs/OPERATING.md).

## Privacy notes for operators

Running a server means you hold other people's data. What this server stores:

- player display names and, for local accounts, salted scrypt password hashes;
- Discord user ids, usernames and display names, if Discord sign-in is used;
- hashed API keys (one per signed-in device);
- turn files, custom scenario files uploaded for games, short fog-safe turn
  summaries written by the game, and game results.

Turn files are encrypted with a per-game passphrase, but **the server holds that
passphrase** (it hands it to both players), so the operator could technically decrypt
them. Players should only use servers run by people they trust. Nothing is sent to
Straits Games or any third party, except Discord when you enable the plugin. Research
mode tells your players their games are kept for AI research and lets you export them
(`export-games`, or the telemetry listener on your own network); what you do with an
export is up to you, so say so in your notice and make sure your players agree. Tell your
players what you keep and for how long. `doctrine-server players disable` locks an
account out; erasing a player's data completely is a manual database job (see
[docs/OPERATING.md](docs/OPERATING.md)).

## Development

```sh
python -m venv .venv
.venv/bin/pip install -e ".[discord,dev]"    # Windows: .venv\Scripts\pip ...
.venv/bin/pytest
```

Security issues: see [SECURITY.md](SECURITY.md).

## License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Straits Games LLC.
The license covers this server's code, not the game.
