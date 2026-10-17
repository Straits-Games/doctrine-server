"""FastAPI dependencies shared by the JSON API and the HTML pages."""
from __future__ import annotations

from typing import Iterator

from fastapi import Depends, HTTPException, Request

from . import auth, db


def get_conn(request: Request) -> Iterator:
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def current_player(request: Request, conn=Depends(get_conn)) -> auth.Player:
    token = bearer_token(request)
    player = auth.player_for_key(conn, token)
    if player is None:
        raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
    auth.touch_key(conn, token)
    return player


def client_ip(request: Request) -> str:
    # uvicorn rewrites request.client from X-Forwarded-For for trusted proxies
    # (DOCTRINE_FORWARDED_ALLOW_IPS), so this is the real client behind Caddy etc.
    return request.client.host if request.client else "unknown"
