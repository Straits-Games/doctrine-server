import hashlib
import json

from conftest import new_game


def put(client, player, scenario_id, data, name="My Map", sha=None):
    meta = {"scenario_id": scenario_id, "name": name,
            "sha256": sha if sha is not None else hashlib.sha256(data).hexdigest()}
    return client.post("/scenarios", headers=player.h, data={"meta": json.dumps(meta)},
                       files={"file": (f"{scenario_id}.hexscn", data, "application/octet-stream")})


def test_upload_meta_bundle(client, app, alice, bob):
    assert client.get("/scenarios/my_map", headers=alice.h).status_code == 404
    assert client.get("/scenarios/my_map/bundle", headers=alice.h).status_code == 404
    data = b"scenario v1"
    r = put(client, alice, "my_map", data)
    assert r.status_code == 200 and r.json() == {"ok": True}
    sha = hashlib.sha256(data).hexdigest()
    assert client.get("/scenarios/my_map", headers=bob.h).json() == {
        "scenario_id": "my_map", "name": "My Map", "sha256": sha, "exists": True}
    r = client.get("/scenarios/my_map/bundle", headers=bob.h)
    assert r.status_code == 200 and r.content == data
    assert (app.state.data_dir / "scenarios" / f"{sha}.hexscn").is_file()


def test_reupload_points_at_newest_and_keeps_old(client, app, alice):
    put(client, alice, "my_map", b"v1")
    put(client, alice, "my_map", b"v2", name="My Map 2")
    meta = client.get("/scenarios/my_map", headers=alice.h).json()
    assert meta["sha256"] == hashlib.sha256(b"v2").hexdigest() and meta["name"] == "My Map 2"
    assert client.get("/scenarios/my_map/bundle", headers=alice.h).content == b"v2"
    files = {p.name for p in (app.state.data_dir / "scenarios").iterdir()}
    assert f"{hashlib.sha256(b'v1').hexdigest()}.hexscn" in files


def test_upload_validation(client, app, alice):
    r = put(client, alice, "my_map", b"data", sha="0" * 64)
    assert r.status_code == 400 and r.json() == {"detail": "sha_mismatch"}
    r = put(client, alice, "my_map", b"data", sha="../../etc/passwd")
    assert r.json() == {"detail": "sha_mismatch"}
    r = put(client, alice, "../evil", b"data")
    assert r.status_code == 400 and r.json() == {"detail": "bad_scenario"}
    r = put(client, alice, "My Map", b"data")
    assert r.json() == {"detail": "bad_scenario"}
    app.state.config.max_scenario_mb = 0.001
    r = put(client, alice, "big", b"x" * 5000)
    assert r.status_code == 413 and r.json() == {"detail": "too_large"}


def test_uppercase_sha_accepted(client, alice):
    data = b"abc"
    r = put(client, alice, "m", data, sha=hashlib.sha256(data).hexdigest().upper())
    assert r.status_code == 200


def test_custom_game_requires_uploaded_bundle(client, alice, bob):
    data = b"custom map"
    sha = hashlib.sha256(data).hexdigest()
    body = {"scenario_id": "custom_map", "opponent_uid": bob.id, "my_side": 0,
            "custom": True, "scenario_sha256": sha}
    r = client.post("/games", json=body, headers=alice.h)
    assert r.status_code == 400 and r.json() == {"detail": "scenario_missing"}
    put(client, alice, "custom_map", data)
    g = new_game(client, alice, bob, scenario_id="custom_map", custom=True, scenario_sha256=sha)
    assert g["custom"] is True and g["scenario_sha256"] == sha
    r = client.get(f"/games/{g['game_id']}", headers=bob.h).json()
    assert r["custom"] is True and r["scenario_sha256"] == sha
