"""Sign in with Discord, with Discord's HTTP API mocked by httpx.MockTransport."""
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from conftest import make_config, poll, start_device
from fastapi.testclient import TestClient

from doctrine_server.discord.oauth import DiscordOAuth
from doctrine_server.main import create_app

USER = {"id": "80351110224678912", "username": "nelly", "global_name": "Nelly", "avatar": None}


def discord_api(user=USER, token_status=200):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/oauth2/token":
            if token_status != 200:
                return httpx.Response(token_status, json={"error": "invalid_grant"})
            assert b"grant_type=authorization_code" in request.content
            assert b"code=good-code" in request.content
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "at-1", "token_type": "Bearer"})
        if request.url.path == "/api/v10/users/@me":
            assert request.headers["authorization"] == "Bearer at-1"
            return httpx.Response(200, json=user)
        return httpx.Response(404)
    return httpx.MockTransport(handler), seen


def make_client(tmp_path, transport, guild_checker=None, **cfg):
    config = make_config(tmp_path, public_url="http://testserver", join_code="",
                         discord_client_id="12345", discord_client_secret="shh", **cfg)
    oauth = DiscordOAuth(config.discord_client_id, config.discord_client_secret,
                         config.discord_redirect_uri, transport=transport)
    return TestClient(create_app(config, discord_oauth=oauth, guild_checker=guild_checker))


def begin(client):
    d = start_device(client)
    page = client.get("/link", params={"code": d["user_code"]})
    assert "Sign in with Discord" in page.text
    assert "Create account" not in page.text                   # local accounts off
    r = client.get("/auth/discord/start", params={"code": d["user_code"]}, follow_redirects=False)
    assert r.status_code == 303
    url = urlparse(r.headers["location"])
    q = parse_qs(url.query)
    assert url.netloc == "discord.com" and q["scope"] == ["identify"]
    assert q["redirect_uri"] == ["http://testserver/auth/discord/callback"]
    assert q["client_id"] == ["12345"] and "shh" not in r.headers["location"]
    return d, q["state"][0]


def test_discord_sign_in(tmp_path):
    transport, seen = discord_api()
    with make_client(tmp_path, transport) as client:
        assert client.get("/server-info").json()["auth"] == ["discord"]
        d, state = begin(client)
        r = client.get("/auth/discord/callback", params={"code": "good-code", "state": state})
        assert r.status_code == 200 and "Linked" in r.text and "Nelly" in r.text
        out = poll(client, d["device_code"])
        assert out["status"] == "approved"
        assert out["discord_uid"] == USER["id"] and isinstance(out["discord_uid"], str)
        assert out["username"] == "nelly" and out["display_name"] == "Nelly"
        me = client.get("/me", headers={"Authorization": f"Bearer {out['api_key']}"}).json()
        assert me["discord_uid"] == USER["id"] and me["kind"] == "discord"
        # signing in again updates, never duplicates
        d2, state2 = begin(client)
        client.get("/auth/discord/callback", params={"code": "good-code", "state": state2})
        roster = client.get("/players", headers={"Authorization": f"Bearer {out['api_key']}"}).json()
        assert roster == {"players": [{"discord_uid": USER["id"], "name": "Nelly"}]}
    assert len(seen) == 4


def test_display_name_falls_back_to_username(tmp_path):
    transport, _ = discord_api(user={"id": "42", "username": "plain", "global_name": None})
    with make_client(tmp_path, transport) as client:
        d, state = begin(client)
        client.get("/auth/discord/callback", params={"code": "good-code", "state": state})
        assert poll(client, d["device_code"])["display_name"] == "plain"


def test_tampered_or_foreign_state(tmp_path):
    transport, seen = discord_api()
    with make_client(tmp_path, transport) as client:
        d, state = begin(client)
        code, ts, sig = state.split(".")
        other = start_device(client)["user_code"].replace("-", "")
        for bad in (f"{other}.{ts}.{sig}", f"{code}.{ts}.{'0' * 64}", "garbage", ""):
            r = client.get("/auth/discord/callback", params={"code": "good-code", "state": bad})
            assert r.status_code == 400, bad
        # state is bound to the browser cookie that started the flow
        client.cookies.clear()
        r = client.get("/auth/discord/callback", params={"code": "good-code", "state": state})
        assert r.status_code == 400
        assert poll(client, d["device_code"]) == {"status": "pending"}
    assert seen == []


def test_oauth_failure_and_cancel(tmp_path):
    transport, _ = discord_api(token_status=400)
    with make_client(tmp_path, transport) as client:
        d, state = begin(client)
        r = client.get("/auth/discord/callback", params={"code": "good-code", "state": state})
        assert r.status_code == 502
        r = client.get("/auth/discord/callback", params={"error": "access_denied", "state": state})
        assert r.status_code == 400 and "cancelled" in r.text
        assert poll(client, d["device_code"]) == {"status": "pending"}


@pytest.mark.parametrize("member, status", [(True, 200), (False, 403), (None, 503)])
def test_guild_gate(tmp_path, member, status):
    asked = []

    async def checker(user_id):
        asked.append(user_id)
        return member

    transport, _ = discord_api()
    with make_client(tmp_path, transport, guild_checker=checker,
                     discord_guild_id="999", discord_invite_url="https://discord.gg/example") as client:
        d, state = begin(client)
        r = client.get("/auth/discord/callback", params={"code": "good-code", "state": state})
        assert r.status_code == status
        if member is False:
            assert "https://discord.gg/example" in r.text
        expected = "approved" if member else "pending"
        assert poll(client, d["device_code"])["status"] == expected
    assert asked == [USER["id"]]


def test_start_without_cookie_redirects_to_link(tmp_path):
    transport, _ = discord_api()
    with make_client(tmp_path, transport) as client:
        d = start_device(client)
        r = client.get("/auth/discord/start", params={"code": d["user_code"]}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/link?code=")


def test_local_forms_404_when_disabled(tmp_path):
    transport, _ = discord_api()
    with make_client(tmp_path, transport) as client:
        r = client.post("/link", data={"code": "x", "action": "register"})
        assert r.status_code == 404
