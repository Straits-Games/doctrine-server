"""The optional Discord bot: turn pings, lobby announcements, per-game channels,
slash commands and the guild-membership gate for Discord sign-in.

Runs on the web server's event loop (started from the FastAPI lifespan). API
handlers run in a threadpool, so `DiscordNotifier` hands work to the loop with
`asyncio.run_coroutine_threadsafe` and never waits for it.

Intents: only `guilds` (not privileged). `Guild.fetch_member` is a plain REST call
and works without the privileged members intent.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from .. import db, games
from . import messages

log = logging.getLogger("doctrine_server.discord")

ARCHIVE_CATEGORY = "Archive"
_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)


class DoctrineBot(discord.Client):
    def __init__(self, config, db_path):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents, allowed_mentions=_MENTIONS)
        self.config = config
        self.db_path = db_path
        self.connected = False
        self.tree = app_commands.CommandTree(self)
        self._guild_obj = (discord.Object(id=int(config.discord_guild_id))
                           if config.discord_guild_id else None)
        self.tree.add_command(build_commands(self), guild=self._guild_obj)

    # ---- lifecycle ----
    async def setup_hook(self) -> None:
        try:
            await self.tree.sync(guild=self._guild_obj)
        except discord.HTTPException as e:
            log.warning("could not sync slash commands: %s", e)

    async def on_ready(self) -> None:
        self.connected = True
        log.info("Discord bot connected as %s", self.user)

    async def on_resumed(self) -> None:
        self.connected = True

    async def on_disconnect(self) -> None:
        self.connected = False

    @property
    def status(self) -> str:
        return "connected" if self.connected and not self.is_closed() else "disconnected"

    # ---- helpers ----
    async def db_call(self, fn, *args):
        """Run fn(conn, *args) on a fresh connection in a worker thread."""
        def run():
            conn = db.connect(self.db_path)
            try:
                return fn(conn, *args)
            finally:
                conn.close()
        return await asyncio.to_thread(run)

    async def channel(self, channel_id) -> discord.abc.Messageable | None:
        if not channel_id:
            return None
        try:
            cid = int(channel_id)
            return self.get_channel(cid) or await self.fetch_channel(cid)
        except (ValueError, discord.HTTPException) as e:
            log.warning("channel %s unavailable: %s", channel_id, e)
            return None

    async def guild(self) -> discord.Guild | None:
        if not self.config.discord_guild_id:
            return None
        gid = int(self.config.discord_guild_id)
        try:
            return self.get_guild(gid) or await self.fetch_guild(gid)
        except discord.HTTPException as e:
            log.warning("guild unavailable: %s", e)
            return None

    async def category(self, guild: discord.Guild, name: str) -> discord.CategoryChannel:
        for cat in guild.categories:
            if cat.name.lower() == name.lower():
                return cat
        return await guild.create_category(name, reason="Doctrine server")

    async def send(self, target, content: str, embed: discord.Embed | None = None) -> None:
        await target.send(content=content, embed=embed, allowed_mentions=_MENTIONS)

    # ---- guild gate (Discord sign-in) ----
    async def is_guild_member(self, user_id: str) -> bool | None:
        """True/False, or None if it cannot be checked right now (fail closed)."""
        if self.status != "connected":
            return None
        guild = await self.guild()
        if guild is None:
            return None
        try:
            await guild.fetch_member(int(user_id))
            return True
        except discord.NotFound:
            return False
        except discord.HTTPException as e:
            log.warning("fetch_member failed: %s", e)
            return None

    # ---- notifier events ----
    async def on_game_created(self, game: dict) -> None:
        lobby = await self.channel(self.config.discord_channel_id)
        if lobby is not None:
            await self.send(lobby, messages.challenge(game))
        if not self.config.discord_games_category:
            return
        guild = await self.guild()
        if guild is None:
            log.warning("DISCORD_GAMES_CATEGORY is set but DISCORD_GUILD_ID is not usable")
            return
        try:
            cat = await self.category(guild, self.config.discord_games_category)
            ch = await guild.create_text_channel(messages.channel_name(game), category=cat,
                                                 reason="Doctrine game created")
        except discord.HTTPException as e:
            log.warning("could not create a channel for %s: %s", game["game_id"], e)
            return
        await self.db_call(games.set_channel_id, game["game_id"], str(ch.id))
        await self.send(ch, messages.game_channel_intro(game))

    async def on_turn_posted(self, game: dict, recipient_side: int, summary: str) -> None:
        # Re-read: the channel may have been created after this game view was taken.
        game = await self.db_call(games.notify_view, game["game_id"]) or game
        content, title, desc = messages.turn_posted(game, recipient_side, summary)
        embed = discord.Embed(title=title, description=desc or None)
        target = await self.channel(game.get("channel_id"))
        if target is None:
            target = await self.channel(self.config.discord_channel_id)
        if target is not None:
            await self.send(target, content, embed)
            return
        rec = game["players"][recipient_side]
        if rec.get("kind") == "discord":
            try:
                user = await self.fetch_user(int(rec["id"]))
                await self.send(user, content, embed)
            except discord.HTTPException as e:
                log.info("could not DM %s: %s", rec["id"], e)

    async def on_game_ended(self, game: dict) -> None:
        game = await self.db_call(games.notify_view, game["game_id"]) or game
        text = messages.game_ended(game)
        ch = await self.channel(game.get("channel_id"))
        if ch is not None:
            await self.send(ch, text)
        lobby = await self.channel(self.config.discord_channel_id)
        if lobby is not None and lobby != ch:
            await self.send(lobby, text)
        if ch is not None:
            await self.archive(ch)

    async def archive(self, ch) -> None:
        """Move a game channel to "Archive" and try to make it read-only."""
        guild = getattr(ch, "guild", None)
        if guild is None:
            return
        try:
            archive = await self.category(guild, ARCHIVE_CATEGORY)
            await ch.edit(category=archive, reason="Doctrine game ended")
        except discord.HTTPException as e:
            log.warning("could not move channel %s to %s: %s", ch.id, ARCHIVE_CATEGORY, e)
            return
        try:
            await ch.set_permissions(guild.default_role, send_messages=False,
                                     reason="Doctrine game ended")
        except discord.Forbidden:
            log.warning("no Manage Roles permission: archived channel %s left writable", ch.id)
        except discord.HTTPException as e:
            log.warning("could not make channel %s read-only: %s", ch.id, e)


def build_commands(bot: DoctrineBot) -> app_commands.Group:
    group = app_commands.Group(name="doctrine", description="Doctrine dedicated server")

    @group.command(name="games", description="Your active Doctrine games and whose move it is")
    async def games_cmd(interaction: discord.Interaction) -> None:
        rows = await bot.db_call(games.list_games, str(interaction.user.id))
        await interaction.response.send_message(messages.games_list(rows), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="server", description="How to connect to this Doctrine server")
    async def server_cmd(interaction: discord.Interaction) -> None:
        address = bot.config.public_url or "(ask the server operator for the address)"
        await interaction.response.send_message(
            messages.server_info(bot.config.server_name, address), ephemeral=True)

    @group.command(name="close", description="Close a game (needs Manage Channels)")
    @app_commands.describe(game_id="The game id, e.g. rhine-a1b2c3")
    async def close_cmd(interaction: discord.Interaction, game_id: str) -> None:
        perms = interaction.permissions
        if interaction.guild is None or not perms.manage_channels:
            await interaction.response.send_message(
                "You need the Manage Channels permission in this server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        game_id = game_id.strip().lower()
        if not games.valid_game_id(game_id) or not await bot.db_call(games.close_game, game_id):
            await interaction.followup.send(f"No active game `{game_id[:80]}`.", ephemeral=True)
            return
        log.info("game %s closed via Discord by %s", game_id, interaction.user.id)
        game = await bot.db_call(games.notify_view, game_id)
        if game:
            await bot.on_game_ended(game)
        await interaction.followup.send(f"Closed `{game_id}`.", ephemeral=True)

    return group


class DiscordNotifier:
    """Notifier that schedules bot work on its event loop and returns immediately."""

    def __init__(self, bot: DoctrineBot, loop: asyncio.AbstractEventLoop):
        self.bot = bot
        self.loop = loop

    def _submit(self, coro) -> None:
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        fut.add_done_callback(_log_failure)

    def game_created(self, game: dict) -> None:
        self._submit(self.bot.on_game_created(game))

    def turn_posted(self, game: dict, recipient_side: int, summary: str) -> None:
        self._submit(self.bot.on_turn_posted(game, recipient_side, summary))

    def game_ended(self, game: dict) -> None:
        self._submit(self.bot.on_game_ended(game))

    def status(self) -> str:
        return self.bot.status


def _log_failure(fut) -> None:
    if fut.cancelled():
        return
    exc = fut.exception()
    if exc is not None:
        log.error("Discord notification failed: %r", exc)


async def start(config, db_path) -> DoctrineBot:
    """Create the bot and connect it in the background on the running loop."""
    bot = DoctrineBot(config, db_path)

    async def runner():
        try:
            await bot.start(config.discord_bot_token)
        except discord.LoginFailure:
            log.error("Discord bot login failed: check DISCORD_BOT_TOKEN")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Discord bot stopped")

    bot.runner_task = asyncio.get_running_loop().create_task(runner())
    return bot


async def stop(bot: DoctrineBot) -> None:
    try:
        await bot.close()
    finally:
        task = getattr(bot, "runner_task", None)
        if task is not None and not task.done():
            task.cancel()
