import re

from conftest import JOIN_CODE, PASSWORD, link_form, poll, register, start_device

from doctrine_server import auth, db


def test_device_start_shape(client):
    d = start_device(client)
    assert set(d) == {"device_code", "user_code", "verification_url", "interval", "expires_in"}
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}", d["user_code"])
    assert d["verification_url"] == f"http://testserver/link?code={d['user_code']}"
    assert d["interval"] == 3 and d["expires_in"] == 600


def test_poll_pending_then_approved_once(client):
    d = start_device(client)
    assert poll(client, d["device_code"]) == {"status": "pending"}
    r = link_form(client, d["user_code"], action="register", name="Alice", password=PASSWORD,
                  join_code=JOIN_CODE)
    assert "Linked" in r.text and "return to Doctrine" in r.text
    out = poll(client, d["device_code"])
    assert out["status"] == "approved"
    assert set(out) == {"status", "api_key", "discord_uid", "username", "display_name"}
    assert out["discord_uid"].startswith("l") and len(out["discord_uid"]) == 17
    assert out["display_name"] == "Alice"
    # delivered once: the code is gone afterwards
    assert poll(client, d["device_code"]) == {"status": "expired"}


def test_poll_unknown_and_expired(client, app):
    assert poll(client, "nope") == {"status": "expired"}
    d = start_device(client)
    conn = db.connect(app.state.db_path)
    conn.execute("UPDATE device_codes SET expires_at = 0")
    conn.close()
    assert poll(client, d["device_code"]) == {"status": "expired"}
    assert "expired" in client.get("/link", params={"code": d["user_code"]}).text


def test_poll_requires_device_code(client):
    r = client.post("/auth/device/poll", json={})
    assert r.status_code == 400 and r.json() == {"detail": "bad_request"}


def test_link_page_shows_server_and_forms(client):
    d = start_device(client)
    page = client.get("/link", params={"code": d["user_code"].lower().replace("-", "")})
    assert page.status_code == 200
    assert "Test Server" in page.text and "Be nice." in page.text
    assert d["user_code"] in page.text
    assert "Create account" in page.text and "Sign in" in page.text
    assert "Sign in with Discord" not in page.text           # OAuth not configured
    assert client.get("/link").status_code == 200              # code entry form
    assert client.get("/link", params={"code": "???"}).status_code == 400


def test_wrong_join_code(client):
    d = start_device(client)
    r = link_form(client, d["user_code"], action="register", name="Mallory",
                  password=PASSWORD, join_code="guess")
    assert r.status_code == 403 and "Wrong join code" in r.text
    assert poll(client, d["device_code"]) == {"status": "pending"}


def test_registration_validation(client):
    register(client, "Alice")
    d = start_device(client)
    for name, pw, msg in (("A", PASSWORD, "characters"), ("x" * 25, PASSWORD, "characters"),
                          ("Bad<name>", PASSWORD, "characters"), ("Carol", "short", "Passwords"),
                          ("aLiCe", PASSWORD, "already taken")):
        r = link_form(client, d["user_code"], action="register", name=name, password=pw,
                      join_code=JOIN_CODE)
        assert r.status_code in (400, 409) and msg in r.text, (name, r.text)


def test_sign_in_again_without_join_code(client):
    alice = register(client, "Alice")
    d = start_device(client)
    r = link_form(client, d["user_code"], action="signin", name="ALICE", password="wrong pass")
    assert r.status_code == 403 and "Wrong display name or password" in r.text
    r = link_form(client, d["user_code"], action="signin", name="alice", password=PASSWORD)
    assert "Linked" in r.text
    out = poll(client, d["device_code"])
    assert out["discord_uid"] == alice.id
    assert out["api_key"] != alice.key                         # one key per device


def test_csrf_required(client):
    d = start_device(client)
    client.get("/link", params={"code": d["user_code"]})
    r = client.post("/link", data={"code": d["user_code"], "csrf": "forged", "action": "register",
                                   "name": "Eve", "password": PASSWORD, "join_code": JOIN_CODE})
    assert r.status_code == 400 and "Form expired" in r.text
    # a token for a different code does not transfer
    d2 = start_device(client)
    page = client.get("/link", params={"code": d2["user_code"]})
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = client.post("/link", data={"code": d["user_code"], "csrf": token, "action": "register",
                                   "name": "Eve", "password": PASSWORD, "join_code": JOIN_CODE})
    assert r.status_code == 400
    assert poll(client, d["device_code"]) == {"status": "pending"}


def test_failed_attempts_rate_limited(client):
    register(client, "Alice")
    d = start_device(client)
    for _ in range(10):
        r = link_form(client, d["user_code"], action="signin", name="Alice", password="wrongwrong")
        assert r.status_code == 403
    r = link_form(client, d["user_code"], action="signin", name="Alice", password=PASSWORD)
    assert r.status_code == 429


def test_device_start_rate_limited(client):
    for _ in range(20):
        assert client.post("/auth/device", json={}).status_code == 200
    r = client.post("/auth/device", json={})
    assert r.status_code == 429 and r.json() == {"detail": "rate_limited"}


def test_me_players_logout(client, alice, bob):
    r = client.get("/me", headers=alice.h)
    assert r.status_code == 200
    assert r.json()["discord_uid"] == alice.id and r.json()["display_name"] == "Alice"
    roster = client.get("/players", headers=alice.h).json()["players"]
    assert {"discord_uid": bob.id, "name": "Bob"} in roster
    assert all(set(p) == {"discord_uid", "name"} for p in roster)
    assert client.post("/auth/logout", headers=alice.h).json() == {"ok": True}
    r = client.get("/me", headers=alice.h)
    assert r.status_code == 401 and r.json() == {"detail": "unauthorized"}


def test_unauthorized(client):
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}):
        for method, url in (("get", "/me"), ("get", "/players"), ("get", "/games"),
                            ("post", "/auth/logout"), ("get", "/scenarios/x")):
            r = getattr(client, method)(url, headers=headers)
            assert r.status_code == 401 and r.json() == {"detail": "unauthorized"}, url


def test_disabled_player_is_locked_out(client, app, alice, bob):
    conn = db.connect(app.state.db_path)
    auth.set_disabled(conn, alice.id, True)
    conn.close()
    assert client.get("/me", headers=alice.h).status_code == 401
    roster = client.get("/players", headers=bob.h).json()["players"]
    assert alice.id not in [p["discord_uid"] for p in roster]
    d = start_device(client)
    r = link_form(client, d["user_code"], action="signin", name="Alice", password=PASSWORD)
    assert r.status_code == 403 and "disabled" in r.text


def test_only_key_hash_stored(client, app, alice):
    conn = db.connect(app.state.db_path)
    hashes = [r[0] for r in conn.execute("SELECT key_hash FROM api_keys")]
    pw = conn.execute("SELECT pw_hash FROM players WHERE id = ?", (alice.id,)).fetchone()[0]
    conn.close()
    assert alice.key not in hashes and auth.sha256_hex(alice.key) in hashes
    assert pw.startswith("scrypt$") and PASSWORD not in pw


def test_password_hashing():
    h = auth.hash_password("hunter22")
    assert auth.verify_password("hunter22", h)
    assert not auth.verify_password("hunter23", h)
    assert not auth.verify_password("x", "garbage")
    assert h != auth.hash_password("hunter22")                 # per-user salt
