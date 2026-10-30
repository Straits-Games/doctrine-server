# Doctrine dedicated-server protocol, version 1

This is the REST contract between the Doctrine game client and a dedicated server. It
is a **compatibility promise**: released game builds speak exactly this, so field
names, endpoints, headers and status codes do not change within protocol 1. Some names
are historical (`discord_uid`, `X-Hexfront-*`) and are kept even for players who never
use Discord. New optional fields may be added; clients ignore unknown fields.

`GET /server-info` reports `"protocol": 1`.

## Conventions

- JSON bodies are UTF-8, `Content-Type: application/json`.
- Authenticated endpoints take `Authorization: Bearer <api_key>`. A missing, unknown or
  revoked key, or a disabled player, gives **401** `{"detail": "unauthorized"}`.
- Errors are `{"detail": "<code>"}` with the codes listed below. A malformed request
  (bad JSON, missing multipart part, wrong query type) is **400** `bad_request`.
- **Player ids are always strings**: a Discord snowflake for Discord accounts, `"l"` +
  16 hex digits for local accounts. Snowflakes exceed 2^53, so never parse them as
  numbers.
- Sides are the integers `0` and `1`. Timestamps are unix seconds.
- Ids in paths: scenario ids match `^[a-z0-9][a-z0-9_-]{0,63}$`; game ids are
  `<scenario_id>-<6 chars [a-z0-9]>`. Anything else is treated as not found.

## Sign-in (device flow)

### `POST /auth/device`
Body `{}`. **200**:
```json
{"device_code": "...", "user_code": "ABCD-EFGH",
 "verification_url": "https://server/link?code=ABCD-EFGH", "interval": 3, "expires_in": 600}
```
The game opens `verification_url` in the browser and polls. `user_code` is 8 characters
from `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`. Rate-limited per IP (20 per 10 minutes):
**429** `rate_limited`.

### `GET /link?code=ABCD-EFGH` (browser, HTML)
Shows the server name, MOTD, the code and the enabled sign-in options:

- **Local accounts** (if a join code is configured): *Create account* (display name
  2-24 characters, unique case-insensitively; password at least 8; the join code) and
  *Sign in* (display name + password). Forms `POST /link` with a CSRF token bound to
  the code and a browser cookie. Failed join-code/password attempts are limited per IP
  (10 per 10 minutes).
- **Discord** (if configured): `GET /auth/discord/start?code=...` redirects to
  Discord (scope `identify`, signed `state`); Discord returns to
  `GET /auth/discord/callback`.

On success the page says "Linked — return to Doctrine".

### `POST /auth/device/poll`
Body `{"device_code": "..."}`. **200** with one of:
```json
{"status": "pending"}
{"status": "expired"}
{"status": "approved", "api_key": "...", "discord_uid": "<player id>",
 "username": "...", "display_name": "..."}
```
`expired` covers unknown and timed-out codes. The approved payload is delivered
**once**; the device code is then deleted and later polls return `expired`. Each
approval issues a new API key (one per device).

### `POST /auth/logout` (auth)
Revokes the presenting key. **200** `{"ok": true}`.

### `GET /me` (auth)
**200** `{"discord_uid": "...", "username": "...", "display_name": "...", "kind": "local|discord"}`.

## Roster

### `GET /players` (auth)
**200** `{"players": [{"discord_uid": "<player id>", "name": "<display name>"}]}` —
every non-disabled player (the caller included). The client sorts.

## Games

### Game object
```json
{"game_id": "rhine-a1b2c3", "scenario_id": "rhine", "scenario_name": "Rhine",
 "status": "active", "your_side": 0, "active_side": 1, "your_turn": false, "turn": 0,
 "players": {"0": "<id>", "1": "<id>"}, "names": {"0": "Alice", "1": "Bob"},
 "channel_id": "", "winner": -1, "updated_at": 1790000000,
 "first_side": 0, "rng_seed": 123456789, "custom": false, "scenario_sha256": ""}
```
- `status`: `active` | `resigned` | `finished` | `closed` (closed = by an admin).
- `winner`: `0`, `1`, or `-1` while undecided.
- `turn` increments once per **full round**; `0` is the setup/deployment phase.
- `channel_id`: the Discord channel of the game, `""` if none.
- `passphrase` (24 URL-safe characters, encrypts the turn files) is included **only**
  in the responses of `POST /games` and `GET /games/{id}`.
- `your_turn` is true when the game is active and either `active_side == your_side`,
  or `turn == 0`, you have not uploaded your own setup file yet, and you are the first
  placer (`1 - first_side`) or a setup file addressed to you exists.

### `POST /games` (auth)
Body:
```json
{"scenario_id": "rhine", "opponent_uid": "<id>", "my_side": 0,
 "first_side": 0, "scenario_name": "Rhine", "custom": false, "scenario_sha256": "<hex>"}
```
`first_side` (default 0), `scenario_name` (default: the id title-cased, `_`/`-` as
spaces), `custom` and `scenario_sha256` are optional. Any well-formed scenario id is
accepted (there is no allowlist; new maps need no server update).

| status | detail | when |
|---|---|---|
| 400 | `bad_scenario` | scenario id does not match the pattern |
| 400 | `bad_side` / `bad_first_side` | not 0 or 1 |
| 400 | `bad_opponent` | unknown, disabled, or the caller |
| 400 | `scenario_missing` | `custom: true` without an uploaded scenario of that sha256 |

**200**: the game object with `passphrase`. A new game starts at `turn` 0 with
`active_side = 1 - first_side` (the defender deploys first), a random nonzero 31-bit
`rng_seed` and a random passphrase.

### `GET /games` (auth)
**200** `{"games": [ ... ]}` — the caller's games, all statuses, most recently updated
first, without `passphrase`.

### `GET /games/{game_id}` (auth)
**200** the game object with `passphrase`. **404** `not_found` for unknown games and
for non-participants.

### `POST /games/{game_id}/turn` (auth, participant) — upload a turn file
`multipart/form-data` with two parts:

- `meta`: JSON `{"turn": N, "active_side": S, "scenario_id": "...", "summary": "...",
  "client_version": "x.y.z"}` where `active_side` is the side to move **after** this
  file is loaded (the recipient), `summary` is fog-safe text (kept up to 500
  characters, shown in notifications) and the optional `client_version` is the game
  build that wrote the file (kept up to 32 characters, for research exports);
- `file`: the opaque, encrypted turn file, with a filename such as
  `rhine_t02_s1.hexturn`.

First the request itself is checked: **413** `too_large` (file above
`DOCTRINE_MAX_TURN_MB`), **400** `bad_meta` (`meta` is not a JSON object, or `turn` /
`active_side` are not integers), **404** `not_found` (not your game), **409**
`not_active` (the game is over). Then the turn rules, in order:

| # | status | detail | rule |
|---|---|---|---|
| 1 | 400 | `bad_active_side` | `meta.active_side` must be `1 - <your side>` |
| 2 | 409 | `out_of_turn` | `turn >= 1` and it is not your move (turn 0 is exempt: either side may upload its setup) |
| 3 | 409 | `duplicate_turn` | you already uploaded a file for this turn number |
| 4 | 400 | `stale_turn` | `turn < 0` or `turn < game.turn - 1` |

Then the file is stored for the recipient, `active_side = meta.active_side`,
`turn = max(turn, meta.turn)`. Uploading never changes any download cursor.
**200** `{"ok": true, "turn": <game turn>, "active_side": <side>}`.

### `GET /games/{game_id}/turn[?since=N]` (auth, participant) — download
Returns the file addressed to the caller:

1. **Setup re-delivery:** while `turn == 0` and it is the caller's move, the newest
   turn-0 file addressed to the caller is returned, whatever the cursor says (a game
   restarted during setup can fetch it again).
2. Otherwise, with `cursor = since` if given and `>= 0`, else the server's stored
   per-player cursor (initially -1), the file with the highest turn number `> cursor`
   is returned.

**200** `application/octet-stream` with headers:

| header | value |
|---|---|
| `X-Hexfront-Filename` | the uploader's filename, sanitised (`[A-Za-z0-9._-]`, at most 96 characters) |
| `X-Hexfront-Turn` | the file's turn number |
| `X-Hexfront-Side` | the recipient side (the caller's side) |

After serving, the stored cursor becomes `max(cursor, file turn)`. **204** (empty
body) when there is nothing new. Downloads keep working after the game has ended.

Why it works this way: both player-turns of a round share a turn number, so a reply
can carry the same number as the file its recipient last sent. Cursors therefore move
on download only, and turn 0 is special-cased both ways.

### `POST /games/{game_id}/resign` (auth, participant)
No body. Active game: `status = resigned`, `winner = 1 - <your side>`. Already over:
no change. **200** the game object (without passphrase).

### `POST /games/{game_id}/result` (auth, participant)
Body `{"winner": 0|1, "level": "...", "reason": "..."}` — the game's natural result.
Active game: `status = finished`. Already over: no change (the first report wins).
**400** `bad_winner` if `winner` is not 0 or 1. **200** the game object.

## Custom scenarios (Scenario Builder)

### `GET /scenarios/{scenario_id}` (auth)
**200** `{"scenario_id": "...", "name": "...", "sha256": "<hex>", "exists": true}`;
**404** `not_found`.

### `POST /scenarios` (auth)
`multipart/form-data`: `meta` = JSON `{"scenario_id", "name", "sha256"}`, `file` = the
`.hexscn` bundle. The server checks `sha256(file) == meta.sha256` (**400**
`sha_mismatch`), the id pattern (**400** `bad_scenario`) and the size (**413**
`too_large`). Files are stored by content hash; re-uploading an id points it at the
newest file (older files are kept for games that reference them). **200** `{"ok": true}`.

### `GET /scenarios/{scenario_id}/bundle` (auth)
**200** the newest bundle for that id; **404** `not_found`.

## Misc

### `GET /healthz`
**200** `{"ok": true, "version": "x.y.z", "discord": "off"|"connected"|"disconnected"}`.

### `GET /server-info`
**200** `{"name": "...", "version": "x.y.z", "protocol": 1, "motd": "...",
"auth": ["local", "discord"], "discord_invite": "...", "research": {"notice": "..."}}` —
`auth` lists the enabled sign-in methods; `discord_invite` only when configured;
`research` only in research mode (`DOCTRINE_RESEARCH`, server 0.2.0+), with the notice
the game shows on its Dedicated Server screen. The research records themselves are
served by the operator's telemetry listener, which is not part of this protocol (see
`docs/OPERATING.md`).

### `GET /`
A small HTML page explaining how to connect.
