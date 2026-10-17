# Discord plugin

Everything here is optional. The server works without Discord; local accounts and
Discord sign-in can be enabled side by side.

There are two independent parts:

| part | needs | does |
|---|---|---|
| **Sign in with Discord** | `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DOCTRINE_PUBLIC_URL` | players link the game with their Discord account (OAuth2, `identify` scope only) |
| **The bot** | `DISCORD_BOT_TOKEN` and `pip install "doctrine-server[discord]"` (included in the Docker image) | turn pings, lobby announcements, per-game channels, slash commands, the guild gate |

## 1. Create the application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications)
   and create an application (name it after your server).
2. **OAuth2** page: copy the *Client ID* and reset/copy the *Client Secret* into
   `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET`.
3. Add the redirect `{DOCTRINE_PUBLIC_URL}/auth/discord/callback`, for example
   `https://doctrine.example.org/auth/discord/callback`. It must match
   `DOCTRINE_PUBLIC_URL` exactly (scheme, host, port).
4. **Bot** page (only if you want the bot): reset/copy the token into
   `DISCORD_BOT_TOKEN`. No privileged gateway intents are needed — leave *Presence*,
   *Server Members* and *Message Content* off.

Keep the secret and the token out of version control (`.env` is git-ignored).

## 2. Invite the bot

In **OAuth2 > URL Generator** pick the scopes `bot` and `applications.commands` and
these bot permissions:

| permission | why |
|---|---|
| View Channels, Send Messages | post notifications |
| Embed Links | turn summaries are sent as embeds |
| Manage Channels | create per-game channels and move ended ones to "Archive" (only with `DISCORD_GAMES_CATEGORY`) |
| Manage Roles (optional) | make archived channels read-only; without it they are archived but stay writable (a warning is logged) |

Open the generated URL and add the bot to your Discord server.

## 3. Configure

| variable | meaning |
|---|---|
| `DISCORD_GUILD_ID` | your Discord server's id (Developer Mode > right-click the server > Copy Server ID). Slash commands are registered there (instantly) instead of globally. With the bot running, **Discord sign-in is limited to members** of this server. |
| `DISCORD_CHANNEL_ID` | lobby channel for new challenges and results |
| `DISCORD_GAMES_CATEGORY` | category name; if set, each game gets its own text channel there |
| `DISCORD_INVITE_URL` | invite link shown to non-members and in `/server-info` |

The guild gate uses the bot's `fetch_member` REST call, which works **without** the
privileged Server Members intent. If the bot is offline when someone signs in, the
sign-in fails closed ("try again later"). If `DISCORD_GUILD_ID` is set without a bot
token, the gate cannot be checked and Discord sign-in is open to anyone (a warning is
logged at startup).

The roster in the game always lists every linked player (local and Discord), not
every guild member.

## What the bot does

- **New game:** announces the challenge in the lobby channel and, with
  `DISCORD_GAMES_CATEGORY`, creates a channel for the game.
- **Turn uploaded:** pings the recipient with the scenario, turn and the game's
  fog-safe summary — in the game's channel if there is one, else in the lobby, else
  by DM. Players with local accounts are named, not pinged (and get no DM).
- **Game ended** (result, resignation or admin close): announces the result and moves
  the game channel to a category named `Archive` (created if missing), then tries to
  make it read-only.
- Mentions are restricted to users; `@everyone`, roles and markdown in player names
  are neutralised.

### Slash commands

| command | who | does |
|---|---|---|
| `/doctrine games` | anyone | your active games and whose move it is (only you see the reply) |
| `/doctrine server` | anyone | the server address and how to connect |
| `/doctrine close <game_id>` | members with Manage Channels | closes the game and archives its channel |

Commands are synced at startup: per guild when `DISCORD_GUILD_ID` is set (immediate),
otherwise globally (Discord can take up to an hour to show them).

### Limits

Discord allows 50 channels per category and 500 per server. Busy servers should clean
up the `Archive` category now and then (deleting channels there does not affect games).
