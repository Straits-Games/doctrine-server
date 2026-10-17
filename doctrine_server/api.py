"""The JSON REST API spoken by the game client (protocol 1 — see docs/PROTOCOL.md).

Handlers are thin: validate the HTTP shape, call into auth/games/scenarios, then fire
notifications after the change is committed.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Form, Request, Response, UploadFile

from . import PROTOCOL_VERSION, __version__, auth, games, scenarios
from .deps import bearer_token, client_ip, current_player, get_conn
from .errors import ApiError
from .notify import safe_call, safe_status

log = logging.getLogger("doctrine_server.api")
router = APIRouter()


def _notify(request: Request, conn, method: str, game_id: str, *extra) -> None:
    """Fire-and-forget: nothing in here may fail the request."""
    try:
        view = games.notify_view(conn, game_id)
        if view is not None:
            safe_call(request.app.state.notifier, method, view, *extra)
    except Exception:  # noqa: BLE001
        log.exception("notification %s for %s failed", method, game_id)


def _read_capped(upload: UploadFile, cap: int) -> bytes:
    data = upload.file.read(cap + 1)
    if len(data) > cap:
        raise ApiError(413, "too_large")
    return data


def public_base(request: Request) -> str:
    """DOCTRINE_PUBLIC_URL, or the address the client used (LAN setups without one)."""
    return request.app.state.config.public_url or str(request.base_url).rstrip("/")


# ---------------------------------------------------------------- misc

@router.get("/healthz")
def healthz(request: Request):
    return {"ok": True, "version": __version__,
            "discord": safe_status(request.app.state.notifier)}


@router.get("/server-info")
def server_info(request: Request):
    cfg = request.app.state.config
    info = {"name": cfg.server_name, "version": __version__, "protocol": PROTOCOL_VERSION,
            "motd": cfg.motd, "auth": cfg.auth_modes()}
    if cfg.discord_invite_url:
        info["discord_invite"] = cfg.discord_invite_url
    return info


# ---------------------------------------------------------------- auth

@router.post("/auth/device")
def device_start(request: Request, conn=Depends(get_conn)):
    if not request.app.state.device_limiter.hit(client_ip(request)):
        raise ApiError(429, "rate_limited")
    device_code, user_code = auth.start_device(conn)
    shown = auth.format_user_code(user_code)
    return {
        "device_code": device_code,
        "user_code": shown,
        "verification_url": f"{public_base(request)}/link?code={shown}",
        "interval": auth.DEVICE_INTERVAL,
        "expires_in": auth.DEVICE_TTL,
    }


@router.post("/auth/device/poll")
def device_poll(body: Any = Body(None), conn=Depends(get_conn)):
    code = body.get("device_code") if isinstance(body, dict) else None
    if not isinstance(code, str) or not code:
        raise ApiError(400, "bad_request")
    return auth.poll_device(conn, code)


@router.post("/auth/logout")
def logout(request: Request, player=Depends(current_player), conn=Depends(get_conn)):
    auth.revoke_key(conn, bearer_token(request))
    return {"ok": True}


@router.get("/me")
def me(player=Depends(current_player)):
    return {"discord_uid": player.id, "username": player.username,
            "display_name": player.display_name, "kind": player.kind}


@router.get("/players")
def players(player=Depends(current_player), conn=Depends(get_conn)):
    return {"players": [{"discord_uid": p.id, "name": p.display_name}
                        for p in auth.roster(conn)]}


# ---------------------------------------------------------------- games

@router.post("/games")
def create_game(request: Request, body: Any = Body(None), player=Depends(current_player),
                conn=Depends(get_conn)):
    game = games.create_game(conn, player.id, body)
    side = games.side_of(game, player.id)
    _notify(request, conn, "game_created", game["game_id"])
    return games.game_dict(conn, game, side, with_passphrase=True)


@router.get("/games")
def list_games(player=Depends(current_player), conn=Depends(get_conn)):
    return {"games": games.list_games(conn, player.id)}


@router.get("/games/{game_id}")
def get_game(game_id: str, player=Depends(current_player), conn=Depends(get_conn)):
    game, side = games.game_for_player(conn, game_id, player.id)
    return games.game_dict(conn, game, side, with_passphrase=True)


@router.post("/games/{game_id}/turn")
def post_turn(game_id: str, request: Request, meta: str = Form(...),
              file: UploadFile = File(...), player=Depends(current_player),
              conn=Depends(get_conn)):
    parsed = games.parse_meta(meta)
    data = _read_capped(file, request.app.state.config.max_turn_bytes)
    result = games.upload_turn(conn, request.app.state.data_dir, game_id, player.id,
                               parsed, data, file.filename or "")
    _notify(request, conn, "turn_posted", game_id, result["recipient"], result["summary"])
    return {"ok": True, "turn": result["turn"], "active_side": result["active_side"]}


@router.get("/games/{game_id}/turn")
def get_turn(game_id: str, request: Request, since: int | None = None,
             player=Depends(current_player), conn=Depends(get_conn)):
    blob = games.fetch_turn(conn, request.app.state.data_dir, game_id, player.id, since)
    if blob is None:
        return Response(status_code=204)
    return Response(
        content=blob.data,
        media_type="application/octet-stream",
        headers={
            "X-Hexfront-Filename": blob.filename,
            "X-Hexfront-Turn": str(blob.turn),
            "X-Hexfront-Side": str(blob.side),
            "Content-Disposition": f'attachment; filename="{blob.filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/games/{game_id}/resign")
def resign(game_id: str, request: Request, player=Depends(current_player),
           conn=Depends(get_conn)):
    game, side, changed = games.resign(conn, game_id, player.id)
    if changed:
        _notify(request, conn, "game_ended", game_id)
    return games.game_dict(conn, game, side)


@router.post("/games/{game_id}/result")
def result(game_id: str, request: Request, body: Any = Body(None),
           player=Depends(current_player), conn=Depends(get_conn)):
    game, side, changed = games.report_result(conn, game_id, player.id, body)
    if changed:
        _notify(request, conn, "game_ended", game_id)
    return games.game_dict(conn, game, side)


# ---------------------------------------------------------------- scenarios

@router.get("/scenarios/{scenario_id}")
def scenario_meta(scenario_id: str, player=Depends(current_player), conn=Depends(get_conn)):
    meta = scenarios.get_meta(conn, scenario_id)
    if meta is None:
        raise ApiError(404, "not_found")
    return meta


@router.post("/scenarios")
def scenario_upload(request: Request, meta: str = Form(...), file: UploadFile = File(...),
                    player=Depends(current_player), conn=Depends(get_conn)):
    parsed = games.parse_meta(meta)
    data = _read_capped(file, request.app.state.config.max_scenario_bytes)
    scenarios.store(conn, request.app.state.data_dir, player.id, parsed, data)
    return {"ok": True}


@router.get("/scenarios/{scenario_id}/bundle")
def scenario_bundle(scenario_id: str, request: Request, player=Depends(current_player),
                    conn=Depends(get_conn)):
    data = scenarios.bundle(conn, request.app.state.data_dir, scenario_id)
    if data is None:
        raise ApiError(404, "not_found")
    return Response(content=data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{scenario_id}.hexscn"'})
