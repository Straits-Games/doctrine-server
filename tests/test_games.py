from conftest import game_of, new_game, register, upload

GAME_KEYS = {"game_id", "scenario_id", "scenario_name", "status", "your_side", "active_side",
             "your_turn", "turn", "players", "names", "channel_id", "winner", "updated_at",
             "first_side", "rng_seed", "custom", "scenario_sha256"}


def test_create_game_dict(client, alice, bob):
    g = new_game(client, alice, bob, my_side=1, scenario_id="bastogne_winter-2")
    assert set(g) == GAME_KEYS | {"passphrase"}
    assert g["game_id"].startswith("bastogne_winter-2-") and len(g["game_id"]) == len("bastogne_winter-2-") + 6
    assert g["scenario_name"] == "Bastogne Winter 2"
    assert g["players"] == {"0": bob.id, "1": alice.id}
    assert g["names"] == {"0": "Bob", "1": "Alice"}
    assert g["your_side"] == 1 and g["first_side"] == 0
    assert g["status"] == "active" and g["turn"] == 0 and g["winner"] == -1
    assert g["active_side"] == 1 and g["your_turn"] is True   # defender (side 1) places first
    assert g["channel_id"] == "" and g["custom"] is False and g["scenario_sha256"] == ""
    assert isinstance(g["players"]["0"], str)
    assert 0 < g["rng_seed"] < 2 ** 31
    assert len(g["passphrase"]) == 24
    assert isinstance(g["updated_at"], int)


def test_create_game_options(client, alice, bob):
    g = new_game(client, alice, bob, my_side=0, first_side=1, scenario_name="Night Drop")
    assert g["first_side"] == 1 and g["active_side"] == 0 and g["scenario_name"] == "Night Drop"


def test_create_game_validation(client, alice, bob, app):
    def post(**body):
        base = {"scenario_id": "rhine", "opponent_uid": bob.id, "my_side": 0}
        base.update(body)
        return client.post("/games", json={k: v for k, v in base.items() if v is not None},
                           headers=alice.h)

    for sid in ("Rhine", "../x", "", "a" * 65, "-x", None, 7):
        r = post(scenario_id=sid)
        assert r.status_code == 400 and r.json() == {"detail": "bad_scenario"}, sid
    for opp in ("nobody", alice.id, None, ""):
        r = post(opponent_uid=opp)
        assert r.status_code == 400 and r.json() == {"detail": "bad_opponent"}, opp
    for side in (2, -1, "0", None, True):
        assert post(my_side=side).status_code == 400
    assert post(first_side=3).status_code == 400
    r = post(custom=True)
    assert r.status_code == 400 and r.json() == {"detail": "scenario_missing"}
    r = post(custom=True, scenario_sha256="0" * 64)
    assert r.json() == {"detail": "scenario_missing"}
    r = client.post("/games", content=b"not json", headers={**alice.h, "Content-Type": "application/json"})
    assert r.status_code == 400
    # any well-formed scenario id is accepted (no allowlist)
    assert post(scenario_id="brand_new_map_2027").status_code == 200


def test_disabled_opponent_rejected(client, app, alice, bob):
    from doctrine_server import auth, db
    conn = db.connect(app.state.db_path)
    auth.set_disabled(conn, bob.id, True)
    conn.close()
    r = client.post("/games", json={"scenario_id": "rhine", "opponent_uid": bob.id, "my_side": 0},
                    headers=alice.h)
    assert r.json() == {"detail": "bad_opponent"}


def test_list_and_get(client, app, alice, bob):
    from doctrine_server import db
    carol = register(client, "Carol")
    g1 = new_game(client, alice, bob, my_side=1)
    g2 = new_game(client, carol, alice, my_side=1)
    new_game(client, bob, carol)
    listed = client.get("/games", headers=alice.h).json()["games"]
    assert [g["game_id"] for g in listed] == [g2["game_id"], g1["game_id"]]   # newest first
    assert all("passphrase" not in g for g in listed)
    assert all(set(g) == GAME_KEYS for g in listed)
    assert listed[0]["your_side"] == 0 and listed[1]["your_side"] == 1
    # ordered by updated_at, not creation
    conn = db.connect(app.state.db_path)
    conn.execute("UPDATE games SET updated_at = updated_at + 100 WHERE game_id = ?", (g1["game_id"],))
    conn.close()
    listed = client.get("/games", headers=alice.h).json()["games"]
    assert listed[0]["game_id"] == g1["game_id"]
    full = game_of(client, bob, g1["game_id"])
    assert full["passphrase"] == g1["passphrase"] and full["your_side"] == 0
    assert client.get(f"/games/{g1['game_id']}", headers=carol.h).status_code == 404
    assert client.get("/games/not-a-game", headers=alice.h).status_code == 404


def test_your_turn_setup_rules(client, alice, bob):
    g = new_game(client, alice, bob, my_side=0, first_side=0)    # bob (side 1) places first
    gid = g["game_id"]
    assert game_of(client, bob, gid)["your_turn"] is True
    assert game_of(client, alice, gid)["your_turn"] is False
    upload(client, bob, gid, 0, 0)
    assert game_of(client, alice, gid)["your_turn"] is True
    assert game_of(client, bob, gid)["your_turn"] is False
    # the attacker also uploads a setup file (sealed setup): it is the defender's move now
    upload(client, alice, gid, 0, 1)
    assert game_of(client, bob, gid)["your_turn"] is True
    assert game_of(client, alice, gid)["your_turn"] is False


def test_your_turn_first_placer_after_attacker_sealed_first(client, alice, bob):
    """If the attacker uploads its sealed setup first, the defender still has to place."""
    g = new_game(client, alice, bob, my_side=0, first_side=0)
    gid = g["game_id"]
    upload(client, alice, gid, 0, 1)
    assert game_of(client, bob, gid)["your_turn"] is True
    assert game_of(client, alice, gid)["your_turn"] is False


def test_concluded_game_not_your_turn(client, alice, bob):
    g = new_game(client, alice, bob)
    client.post(f"/games/{g['game_id']}/resign", headers=alice.h)
    for p in (alice, bob):
        assert game_of(client, p, g["game_id"])["your_turn"] is False
