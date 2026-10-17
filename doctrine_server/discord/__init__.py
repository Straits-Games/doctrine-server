"""Optional Discord plugin.

`oauth` (Sign in with Discord) only needs httpx and is always importable. `bot`
needs discord.py (`pip install "doctrine-server[discord]"`) and is imported lazily,
only when DISCORD_BOT_TOKEN is set. Nothing in this package's __init__ may import
discord.py, so the core server runs without the extra.
"""
