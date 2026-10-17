"""The Discord plugin without a network: message text, notifier scheduling, routing."""
import asyncio
import threading

import pytest

from doctrine_server.config import Config
from doctrine_server.discord import messages

GAME = {
    "game_id": "rhine-abc123", "scenario_id": "rhine", "scenario_name": "Rhine Crossing",
    "status": "active", "turn": 2, "active_side": 1, "first_side": 0, "winner": -1,
    "result_level": "", "result_reason": "", "channel_id": "",
    "players": {0: {"id": "80351110224678912", "name": "Nelly", "kind": "discord"},
                1: {"id": "l0123456789abcdef", "name": "Bob_*the*_@everyone", "kind": "local"}},
}


def test_messages_mention_discord_and_name_local_players():
    content, title, desc = messages.turn_posted(GAME, 0, "Fog-safe summary")
    assert content.startswith("<@80351110224678912>") and "turn 2" in content
    assert desc == "Fog-safe summary" and "Rhine Crossing" in title
    content, _, _ = messages.turn_posted(GAME, 1, "")
    assert "<@" not in content and "**Bob\\_\\*the\\*\\_@​everyone**" in content
    assert "@everyone" not in messages.challenge(GAME)
    setup = dict(GAME, turn=0)
    assert "deployment" in messages.turn_posted(setup, 0, "")[0]


def test_game_ended_texts():
    resigned = dict(GAME, status="resigned", winner=0)
    assert "defeated" in messages.game_ended(resigned) and "resignation" in messages.game_ended(resigned)
    assert "<@" not in messages.game_ended(resigned)            # results don't ping
    finished = dict(GAME, status="finished", winner=1, result_level="decisive")
    assert "decisive" in messages.game_ended(finished)
    assert "closed" in messages.game_ended(dict(GAME, status="closed"))


def test_games_list_text():
    rows = [{"game_id": "rhine-abc123", "scenario_name": "Rhine", "status": "active",
             "your_side": 0, "your_turn": True, "turn": 3, "names": {"0": "Me", "1": "Them"}},
            {"game_id": "x-zzzzzz", "scenario_name": "X", "status": "finished",
             "your_side": 0, "your_turn": False, "turn": 9, "names": {"0": "Me", "1": "Z"}}]
    text = messages.games_list(rows)
    assert "rhine-abc123" in text and "your move" in text and "x-zzzzzz" not in text
    assert "no active games" in messages.games_list([])


discord = pytest.importorskip("discord")
from doctrine_server.discord import bot as bot_mod  # noqa: E402


def _config(**kw):
    base = dict(public_url="https://d.example.org", join_code="x", discord_bot_token="t",
                discord_guild_id="111", discord_channel_id="222")
    base.update(kw)
    return Config(**base)


def test_bot_builds_commands(tmp_path):
    bot = bot_mod.DoctrineBot(_config(), tmp_path / "db.sqlite3")
    cmds = bot.tree.get_commands(guild=discord.Object(id=111))
    assert [c.name for c in cmds] == ["doctrine"]
    assert sorted(c.name for c in cmds[0].commands) == ["close", "games", "server"]
    assert bot.status == "disconnected"
    assert not bot.intents.members and not bot.intents.message_content


def test_notifier_schedules_on_loop_without_blocking():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    done = threading.Event()
    calls = []

    class FakeBot:
        status = "connected"

        async def on_turn_posted(self, game, side, summary):
            calls.append((game["game_id"], side, summary))
            done.set()

        async def on_game_created(self, game):
            raise RuntimeError("discord down")              # logged, never raised

        async def on_game_ended(self, game):
            pass

    n = bot_mod.DiscordNotifier(FakeBot(), loop)
    n.game_created(GAME)
    n.turn_posted(GAME, 0, "hi")
    assert done.wait(5)
    assert calls == [("rhine-abc123", 0, "hi")] and n.status() == "connected"
    loop.call_soon_threadsafe(loop.stop)
    t.join(5)


class FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.sent = []

    async def send(self, content=None, embed=None, allowed_mentions=None):
        self.sent.append((content, embed))


def _routing_bot(tmp_path, channels, **cfg):
    bot = bot_mod.DoctrineBot(_config(**cfg), tmp_path / "db.sqlite3")
    dms = []

    async def channel(cid):
        return channels.get(str(cid)) if cid else None

    async def db_call(fn, *args):
        return None                                        # keep the passed game view

    class FakeUser:
        async def send(self, content=None, embed=None, allowed_mentions=None):
            dms.append(content)

    async def fetch_user(uid):
        return FakeUser()

    bot.channel = channel
    bot.db_call = db_call
    bot.fetch_user = fetch_user
    return bot, dms


def test_turn_routing_game_channel_then_lobby_then_dm(tmp_path):
    lobby, game_ch = FakeChannel(222), FakeChannel(333)
    bot, dms = _routing_bot(tmp_path, {"222": lobby, "333": game_ch})
    asyncio.run(bot.on_turn_posted(dict(GAME, channel_id="333"), 0, "sum"))
    assert len(game_ch.sent) == 1 and not lobby.sent
    content, embed = game_ch.sent[0]
    assert "<@80351110224678912>" in content and embed.description == "sum"
    asyncio.run(bot.on_turn_posted(GAME, 0, "sum"))
    assert len(lobby.sent) == 1
    bot2, dms2 = _routing_bot(tmp_path, {}, discord_channel_id="")
    asyncio.run(bot2.on_turn_posted(GAME, 0, "sum"))
    assert len(dms2) == 1
    asyncio.run(bot2.on_turn_posted(GAME, 1, "sum"))           # local player: no DM possible
    assert len(dms2) == 1


def test_guild_gate_fails_closed_when_disconnected(tmp_path):
    bot = bot_mod.DoctrineBot(_config(), tmp_path / "db.sqlite3")
    assert asyncio.run(bot.is_guild_member("1")) is None
