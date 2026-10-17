"""Text for Discord messages. Pure functions (no discord.py) so they are easy to test.

`game` is the dict from `games.notify_view`. Discord players are mentioned
(`<@id>`); local players are named in bold and never pinged.
"""
from __future__ import annotations

import re

_MD = re.compile(r"([\\*_~`|>\[\]])")


def escape(text: str) -> str:
    """Neutralise Markdown and mass mentions in user-supplied text."""
    text = _MD.sub(r"\\\1", str(text))
    return text.replace("@", "@​")


def who(player: dict, ping: bool = True) -> str:
    if ping and player.get("kind") == "discord" and str(player.get("id", "")).isdigit():
        return f"<@{player['id']}>"
    return f"**{escape(player.get('name', '?'))}**"


def channel_name(game: dict) -> str:
    return game["game_id"][:100]


def challenge(game: dict) -> str:
    p = game["players"]
    return (f"New game: {who(p[0])} vs {who(p[1])} on **{escape(game['scenario_name'])}** "
            f"(`{game['game_id']}`).")


def game_channel_intro(game: dict) -> str:
    p = game["players"]
    return (f"**{escape(game['scenario_name'])}**: {who(p[0])} vs {who(p[1])}. Turn "
            "notifications for this game are posted here.")


def turn_posted(game: dict, recipient_side: int, summary: str) -> tuple[str, str, str]:
    """(content with the mention, embed title, embed description)."""
    rec = game["players"][recipient_side]
    turn = game["turn"]
    label = "deployment" if turn == 0 else f"turn {turn}"
    content = f"{who(rec)}, your move in **{escape(game['scenario_name'])}** ({label})."
    title = f"{game['scenario_name']} - {label}"[:256]
    return content, title, (summary or "")[:4000]


def game_ended(game: dict) -> str:
    p = game["players"]
    name = escape(game["scenario_name"])
    status = game["status"]
    winner = game.get("winner", -1)
    if status == "closed":
        return f"Game `{game['game_id']}` ({name}) was closed by an admin."
    if winner in (0, 1):
        loser = 1 - winner
        how = "by resignation" if status == "resigned" else ""
        level = escape(game.get("result_level") or "")
        tail = f" ({level})" if level and status == "finished" else ""
        return (f"{who(p[winner], ping=False)} defeated {who(p[loser], ping=False)} on "
                f"**{name}** {how}{tail}".rstrip() + ".")
    return f"Game `{game['game_id']}` ({name}) has ended."


def games_list(rows: list[dict]) -> str:
    """For /doctrine games: rows are API game dicts (with your_turn) for the caller."""
    active = [g for g in rows if g["status"] == "active"]
    if not active:
        return "You have no active games on this server."
    lines = ["Your active games:"]
    for g in active[:20]:
        opp = g["names"][str(1 - g["your_side"])]
        state = "**your move**" if g["your_turn"] else "waiting on opponent"
        lines.append(f"- `{g['game_id']}` {escape(g['scenario_name'])} vs {escape(opp)}, "
                     f"turn {g['turn']}: {state}")
    if len(active) > 20:
        lines.append(f"... and {len(active) - 20} more.")
    return "\n".join(lines)


def server_info(server_name: str, address: str) -> str:
    return (f"**{escape(server_name)}** is a Doctrine dedicated server.\n"
            f"In Doctrine choose **Multiplayer > Dedicated Server**, enter `{address}` and "
            "sign in.")
