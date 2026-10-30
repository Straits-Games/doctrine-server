"""The telemetry listener: a separate, read-only app that serves research records.

It runs on its own port (DOCTRINE_RESEARCH_HOST / DOCTRINE_RESEARCH_PORT), meant to be
reachable from the operator's own network only and never through the public reverse
proxy, and every request needs `Authorization: Bearer <DOCTRINE_RESEARCH_TOKEN>`. The
game client never talks to it. See docs/OPERATING.md, "Research mode".

    GET /research/healthz                    {"ok": true, "version": ...}
    GET /research/games[?include_active=1]   {"games": [summary, ...]}
    GET /research/games/{game_id}            the full record (with passphrase)
    GET /research/games/{game_id}/bundle     zip: game.json + turn files (+ scenario)
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from . import __version__, auth, db, games, research
from .config import Config
from .errors import ApiError


def create_research_app(config: Config) -> FastAPI:
    config.validate()
    db_path = db.init(config.data_dir)

    app = FastAPI(title="doctrine-server research", version=__version__,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse({"detail": exc.code}, status_code=exc.status)

    def _authorized(request: Request) -> None:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if (not config.research_token or scheme.lower() != "bearer"
                or not auth.constant_time_equals(token.strip(), config.research_token)):
            raise ApiError(401, "unauthorized")

    def _conn():
        conn = db.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _game(conn, game_id: str):
        row = games.load_game(conn, game_id) if games.valid_game_id(game_id) else None
        if row is None:
            raise ApiError(404, "not_found")
        return row

    @app.get("/research/healthz", dependencies=[Depends(_authorized)])
    def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/research/games", dependencies=[Depends(_authorized)])
    def list_games(include_active: bool = False, conn=Depends(_conn)):
        return {"games": [research.summary(research.game_record(conn, g, config.server_name))
                          for g in research.game_rows(conn, include_active)]}

    @app.get("/research/games/{game_id}", dependencies=[Depends(_authorized)])
    def game(game_id: str, conn=Depends(_conn)):
        return research.game_record(conn, _game(conn, game_id), config.server_name)

    @app.get("/research/games/{game_id}/bundle", dependencies=[Depends(_authorized)])
    def bundle(game_id: str, conn=Depends(_conn)):
        data = research.bundle(conn, config.data_dir, _game(conn, game_id), config.server_name)
        return Response(content=data, media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{game_id}.zip"',
            "Cache-Control": "no-store"})

    return app
