"""App factory: `create_app(config)` wires config, database, routers and plugins."""
from __future__ import annotations

import asyncio
import importlib.util
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__, api, auth, db, web
from .config import Config, ConfigError
from .errors import ApiError
from .notify import NullNotifier

log = logging.getLogger("doctrine_server")

# Upload routes and the cap that applies to each (checked from Content-Length before
# the multipart body is parsed; the handlers re-check the real size).
_SLACK = 256 * 1024


def _upload_cap(config: Config, method: str, path: str) -> int | None:
    if method != "POST":
        return None
    if path == "/scenarios":
        return config.max_scenario_bytes + _SLACK
    if path.startswith("/games/") and path.endswith("/turn"):
        return config.max_turn_bytes + _SLACK
    return None


def create_app(config: Config, *, notifier=None, discord_oauth=None, guild_checker=None,
               start_bot: bool = True) -> FastAPI:
    """Build the ASGI app.

    `notifier`, `discord_oauth` and `guild_checker` can be injected (tests); by default
    they come from the config. The Discord bot is started in the lifespan when
    DISCORD_BOT_TOKEN is set (unless a notifier was injected or start_bot=False).
    """
    config.validate()
    run_bot = start_bot and notifier is None and config.bot_enabled
    if run_bot and importlib.util.find_spec("discord") is None:
        raise ConfigError("DISCORD_BOT_TOKEN is set but discord.py is not installed: "
                          "pip install \"doctrine-server[discord]\"")

    db_path = db.init(config.data_dir)
    conn = db.connect(db_path)
    try:
        secret = db.get_secret(conn)
    finally:
        conn.close()

    if discord_oauth is None and config.discord_oauth_enabled:
        from .discord.oauth import DiscordOAuth
        discord_oauth = DiscordOAuth(config.discord_client_id, config.discord_client_secret,
                                     config.discord_redirect_uri)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bot = None
        if run_bot:
            from .discord import bot as bot_mod
            bot = await bot_mod.start(config, db_path)
            app.state.notifier = bot_mod.DiscordNotifier(bot, asyncio.get_running_loop())
            if app.state.guild_checker is None:
                app.state.guild_checker = bot.is_guild_member
        try:
            yield
        finally:
            if bot is not None:
                await bot_mod.stop(bot)

    app = FastAPI(title="doctrine-server", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.data_dir = config.data_dir
    app.state.db_path = db_path
    app.state.secret = secret
    app.state.notifier = notifier or NullNotifier()
    app.state.discord_oauth = discord_oauth
    app.state.guild_checker = guild_checker
    app.state.device_limiter = auth.RateLimiter(20, 600)     # device-code starts per IP
    app.state.fail_limiter = auth.RateLimiter(10, 600)       # failed join-code/password per IP

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse({"detail": exc.code}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "bad_request"}, status_code=400)

    @app.middleware("http")
    async def _upload_limit(request: Request, call_next):
        cap = _upload_cap(config, request.method, request.url.path)
        if cap is not None:
            try:
                length = int(request.headers.get("content-length") or 0)
            except ValueError:
                length = 0
            if length > cap:
                return JSONResponse({"detail": "too_large"}, status_code=413)
        return await call_next(request)

    app.include_router(api.router)
    app.include_router(web.router)

    modes = []
    if config.local_enabled:
        modes.append("local accounts (join code)")
    if config.discord_oauth_enabled:
        modes.append("Sign in with Discord")
    log.info("doctrine-server %s: data in %s; sign-in: %s", __version__, config.data_dir,
             ", ".join(modes))
    if run_bot:
        gate = "on" if config.discord_guild_id else "off"
        log.info("Discord bot: enabled (guild gate %s)", gate)
    elif config.discord_guild_id and config.discord_oauth_enabled and guild_checker is None:
        log.warning("DISCORD_GUILD_ID is set but the bot is not: Discord sign-in is NOT "
                    "limited to guild members (the gate needs DISCORD_BOT_TOKEN)")
    if not config.public_url:
        log.warning("DOCTRINE_PUBLIC_URL is not set: sign-in links use the address each "
                    "client connected to (fine on a LAN; set it for internet use)")
    return app


def run(config: Config) -> None:
    import uvicorn

    app = create_app(config)
    if not config.research_port:
        uvicorn.run(app, host=config.host, port=config.port, proxy_headers=True,
                    forwarded_allow_ips=config.forwarded_allow_ips, log_level="info")
        return
    # Research mode's telemetry listener: a second, read-only app on its own port.
    from .research_api import create_research_app
    log.info("telemetry listener on %s:%d (research records, bearer token)",
             config.research_host, config.research_port)
    servers = [
        uvicorn.Server(uvicorn.Config(app, host=config.host, port=config.port,
                                      proxy_headers=True, log_level="info",
                                      forwarded_allow_ips=config.forwarded_allow_ips)),
        uvicorn.Server(uvicorn.Config(create_research_app(config), host=config.research_host,
                                      port=config.research_port, log_level="info")),
    ]

    async def serve_all() -> None:
        await asyncio.gather(*(s.serve() for s in servers))

    asyncio.run(serve_all())
