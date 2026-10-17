"""Shared fixtures: an app on a temp data dir, and helpers that sign players in through
the real device flow (POST /auth/device -> /link form -> poll)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from doctrine_server.config import Config
from doctrine_server.main import create_app

JOIN_CODE = "letmein-123"
PASSWORD = "correct horse"


@dataclass
class P:
    """A signed-in test player."""
    id: str
    name: str
    key: str

    @property
    def h(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}


def make_config(tmp_path, **kw) -> Config:
    base = dict(public_url="http://testserver", join_code=JOIN_CODE, data_dir=tmp_path / "data",
                server_name="Test Server", motd="Be nice.")
    base.update(kw)
    return Config(**base)


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def app(config):
    return create_app(config)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


def start_device(client) -> dict:
    r = client.post("/auth/device", json={})
    assert r.status_code == 200, r.text
    return r.json()


def link_form(client, user_code: str, **fields):
    page = client.get("/link", params={"code": user_code})
    assert page.status_code == 200, page.text
    data = {"code": user_code, "csrf": csrf_from(page.text)}
    data.update(fields)
    return client.post("/link", data=data)


def poll(client, device_code: str) -> dict:
    r = client.post("/auth/device/poll", json={"device_code": device_code})
    assert r.status_code == 200, r.text
    return r.json()


def register(client, name: str, password: str = PASSWORD, join_code: str = JOIN_CODE) -> P:
    d = start_device(client)
    r = link_form(client, d["user_code"], action="register", name=name, password=password,
                  join_code=join_code)
    assert r.status_code == 200 and "Linked" in r.text, r.text
    out = poll(client, d["device_code"])
    assert out["status"] == "approved", out
    return P(id=out["discord_uid"], name=out["display_name"], key=out["api_key"])


@pytest.fixture
def alice(client) -> P:
    return register(client, "Alice")


@pytest.fixture
def bob(client) -> P:
    return register(client, "Bob")


def new_game(client, creator: P, opponent: P, my_side: int = 0, **extra) -> dict:
    body = {"scenario_id": "rhine", "opponent_uid": opponent.id, "my_side": my_side}
    body.update(extra)
    r = client.post("/games", json=body, headers=creator.h)
    assert r.status_code == 200, r.text
    return r.json()


def upload(client, player: P, game_id: str, turn: int, active_side: int,
           data: bytes | None = None, filename: str | None = None, summary: str = "",
           scenario_id: str = "rhine"):
    meta = {"turn": turn, "active_side": active_side, "scenario_id": scenario_id,
            "summary": summary}
    if data is None:
        data = f"blob t{turn} -> s{active_side}".encode()
    if filename is None:
        filename = f"{scenario_id}_t{turn:02d}_s{active_side}.hexturn"
    return client.post(f"/games/{game_id}/turn", headers=player.h,
                       data={"meta": json.dumps(meta)},
                       files={"file": (filename, data, "application/octet-stream")})


def download(client, player: P, game_id: str, since: int | None = None):
    params = {"since": since} if since is not None else None
    return client.get(f"/games/{game_id}/turn", headers=player.h, params=params)


def game_of(client, player: P, game_id: str) -> dict:
    r = client.get(f"/games/{game_id}", headers=player.h)
    assert r.status_code == 200, r.text
    return r.json()
