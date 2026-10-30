"""Configuration from environment variables, optionally seeded from a `.env` file.

Precedence: real environment variables > the env file > built-in defaults.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


DEFAULT_RESEARCH_NOTICE = (
    "This server keeps a record of every game played on it, and its operator may use "
    "those games, without player names, to help improve Doctrine's computer opponent.")
RESEARCH_TOKEN_MIN = 16

_TRUE = ("1", "true", "yes", "on")


class ConfigError(Exception):
    """Raised when the configuration cannot work (the server refuses to start)."""


def parse_env_file(path: str | os.PathLike) -> dict[str, str]:
    """Parse a minimal KEY=VALUE env file (comments, blank lines, quotes, `export`)."""
    out: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        out[key] = val
    return out


@dataclass
class Config:
    public_url: str = ""
    host: str = "0.0.0.0"
    port: int = 8080
    data_dir: Path = field(default_factory=lambda: Path("data"))
    server_name: str = "Doctrine server"
    join_code: str = field(default="", repr=False)
    motd: str = ""
    max_turn_mb: float = 8
    max_scenario_mb: float = 32
    # Which reverse-proxy addresses may set X-Forwarded-For (uvicorn's setting).
    forwarded_allow_ips: str = "127.0.0.1"
    discord_client_id: str = ""
    discord_client_secret: str = field(default="", repr=False)
    discord_bot_token: str = field(default="", repr=False)
    discord_guild_id: str = ""
    discord_channel_id: str = ""
    discord_games_category: str = ""
    discord_invite_url: str = ""
    # Research mode: the server tells players it keeps game records for AI research;
    # `export-games` and the telemetry listener (research_port, LAN only, bearer
    # research_token) hand them out.
    research: bool = False
    research_notice: str = ""
    research_host: str = "127.0.0.1"
    research_port: int = 0
    research_token: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.public_url = (self.public_url or "").strip().rstrip("/")

    # ---- derived ----
    @property
    def local_enabled(self) -> bool:
        return bool(self.join_code)

    @property
    def discord_oauth_enabled(self) -> bool:
        return bool(self.discord_client_id and self.discord_client_secret)

    @property
    def bot_enabled(self) -> bool:
        return bool(self.discord_bot_token)

    @property
    def research_notice_text(self) -> str:
        return self.research_notice or DEFAULT_RESEARCH_NOTICE

    @property
    def max_turn_bytes(self) -> int:
        return int(self.max_turn_mb * 1024 * 1024)

    @property
    def max_scenario_bytes(self) -> int:
        return int(self.max_scenario_mb * 1024 * 1024)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "doctrine.sqlite3"

    @property
    def discord_redirect_uri(self) -> str:
        return f"{self.public_url}/auth/discord/callback"

    def auth_modes(self) -> list[str]:
        modes = []
        if self.local_enabled:
            modes.append("local")
        if self.discord_oauth_enabled:
            modes.append("discord")
        return modes

    def validate(self) -> None:
        """Refuse configurations that cannot sign anybody in."""
        if not self.auth_modes():
            raise ConfigError(
                "no sign-in method is configured: set DOCTRINE_JOIN_CODE (local accounts) "
                "and/or DISCORD_CLIENT_ID + DISCORD_CLIENT_SECRET (Sign in with Discord)"
            )
        if self.discord_oauth_enabled and not self.public_url:
            raise ConfigError(
                "Sign in with Discord needs DOCTRINE_PUBLIC_URL (the OAuth redirect is "
                "built from it and must match the one registered with Discord)"
            )
        if self.public_url and not self.public_url.startswith(("http://", "https://")):
            raise ConfigError("DOCTRINE_PUBLIC_URL must start with http:// or https://")
        if not (0 < self.port < 65536):
            raise ConfigError("DOCTRINE_PORT must be between 1 and 65535")
        if self.max_turn_mb <= 0 or self.max_scenario_mb <= 0:
            raise ConfigError("upload caps must be positive")
        if self.research_port:
            if not self.research:
                raise ConfigError("DOCTRINE_RESEARCH_PORT needs DOCTRINE_RESEARCH=on")
            if not (0 < self.research_port < 65536) or self.research_port == self.port:
                raise ConfigError("DOCTRINE_RESEARCH_PORT must be a free port other than "
                                  "DOCTRINE_PORT")
            if len(self.research_token) < RESEARCH_TOKEN_MIN:
                raise ConfigError(f"DOCTRINE_RESEARCH_PORT needs DOCTRINE_RESEARCH_TOKEN "
                                  f"(at least {RESEARCH_TOKEN_MIN} characters)")
        for name in ("discord_client_id", "discord_guild_id", "discord_channel_id"):
            value = getattr(self, name)
            if value and not value.isdigit():
                raise ConfigError(f"{name.upper()} must be a numeric Discord id (got {value!r})")

    # ---- loading ----
    @classmethod
    def from_env(cls, env_file: str | os.PathLike | None = None,
                 environ: Mapping[str, str] | None = None) -> "Config":
        values: dict[str, str] = {}
        if env_file is not None:
            if not Path(env_file).is_file():
                raise ConfigError(f"env file not found: {env_file}")
            values.update(parse_env_file(env_file))
        elif Path(".env").is_file():
            values.update(parse_env_file(".env"))
        values.update(os.environ if environ is None else environ)

        def get(name: str, default: str = "") -> str:
            return str(values.get(name, default)).strip()

        def num(name: str, default: str, kind=int):
            raw = get(name, default) or default
            try:
                return kind(raw)
            except ValueError:
                raise ConfigError(f"{name} must be a number (got {raw!r})") from None

        return cls(
            public_url=get("DOCTRINE_PUBLIC_URL"),
            host=get("DOCTRINE_HOST", "0.0.0.0") or "0.0.0.0",
            port=num("DOCTRINE_PORT", "8080"),
            data_dir=Path(get("DOCTRINE_DATA_DIR", "./data") or "./data").resolve(),
            server_name=get("DOCTRINE_SERVER_NAME", "Doctrine server") or "Doctrine server",
            join_code=get("DOCTRINE_JOIN_CODE"),
            motd=get("DOCTRINE_MOTD"),
            max_turn_mb=num("DOCTRINE_MAX_TURN_MB", "8", float),
            max_scenario_mb=num("DOCTRINE_MAX_SCENARIO_MB", "32", float),
            forwarded_allow_ips=get("DOCTRINE_FORWARDED_ALLOW_IPS", "127.0.0.1") or "127.0.0.1",
            discord_client_id=get("DISCORD_CLIENT_ID"),
            discord_client_secret=get("DISCORD_CLIENT_SECRET"),
            discord_bot_token=get("DISCORD_BOT_TOKEN"),
            discord_guild_id=get("DISCORD_GUILD_ID"),
            discord_channel_id=get("DISCORD_CHANNEL_ID"),
            discord_games_category=get("DISCORD_GAMES_CATEGORY"),
            discord_invite_url=get("DISCORD_INVITE_URL"),
            research=get("DOCTRINE_RESEARCH").lower() in _TRUE,
            research_notice=get("DOCTRINE_RESEARCH_NOTICE"),
            research_host=get("DOCTRINE_RESEARCH_HOST", "127.0.0.1") or "127.0.0.1",
            research_port=num("DOCTRINE_RESEARCH_PORT", "0"),
            research_token=get("DOCTRINE_RESEARCH_TOKEN"),
        )
