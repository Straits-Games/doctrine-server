"""Research mode: the notice, the client build of each turn, `export-games` and the
telemetry listener."""
import io
import json
import zipfile

import pytest
from conftest import make_config, new_game, upload
from fastapi.testclient import TestClient

from doctrine_server import cli, db
from doctrine_server.config import DEFAULT_RESEARCH_NOTICE, Config, ConfigError
from doctrine_server.main import create_app
from doctrine_server.research_api import create_research_app

TOKEN = "t" * 24


@pytest.fixture
def run(app, capsys, monkeypatch):
    data_dir = str(app.state.data_dir)
    monkeypatch.setenv("DOCTRINE_RESEARCH", "on")

    def _run(*args):
        code = cli.main([*args, "--data-dir", data_dir])
        out = capsys.readouterr()
        return code, out.out, out.err
    return _run


@pytest.fixture
def tlm(config):
    """The telemetry listener on the same data dir as `client`."""
    config.research, config.research_port, config.research_token = True, 8081, TOKEN
    with TestClient(create_research_app(config)) as c:
        yield c


H = {"Authorization": f"Bearer {TOKEN}"}


def _finished_game(client, alice, bob):
    """A short game: both setups (the last hands the move to Bob), Bob's turn 1, then
    Alice's natural win."""
    g = new_game(client, alice, bob, my_side=0, first_side=0)
    gid = g["game_id"]
    assert upload(client, bob, gid, 0, 0, data=b"bob setup").status_code == 200
    assert upload(client, alice, gid, 0, 1, data=b"alice setup").status_code == 200
    r = client.post(f"/games/{gid}/turn", headers=bob.h,
                    data={"meta": json.dumps({"turn": 1, "active_side": 0, "scenario_id": "rhine",
                                              "summary": "moved", "client_version": "0.9.2"})},
                    files={"file": ("rhine_t01_s0.hexturn", b"bob t1", "application/octet-stream")})
    assert r.status_code == 200, r.text
    r = client.post(f"/games/{gid}/result", json={"winner": 0, "level": "decisive", "reason": "vp"},
                    headers=alice.h)
    assert r.status_code == 200, r.text
    return g


def _check_record(rec, g, alice, bob):
    assert rec["format"] == 1 and rec["scenario_id"] == "rhine"
    assert rec["first_side"] == 0 and rec["rng_seed"] == g["rng_seed"]
    assert rec["passphrase"] == g["passphrase"]
    assert (rec["status"], rec["winner"], rec["result_level"]) == ("finished", 0, "decisive")
    assert [(t["turn"], t["uploader"], t["recipient"], t["client_version"], t["file"])
            for t in rec["turns"]] == [(0, 1, 0, "", "t0_s0.hexturn"), (0, 0, 1, "", "t0_s1.hexturn"),
                                       (1, 1, 0, "0.9.2", "t1_s0.hexturn")]
    text = json.dumps(rec)
    for secret in (alice.id, bob.id, "Alice", "Bob"):
        assert secret not in text, f"{secret!r} leaked into the record"


# ---------------------------------------------------------------- config and notice

def test_server_info_has_no_research_block_by_default(client):
    assert "research" not in client.get("/server-info").json()


def test_server_info_announces_research(tmp_path):
    with TestClient(create_app(make_config(tmp_path, research=True))) as c:
        assert c.get("/server-info").json()["research"] == {"notice": DEFAULT_RESEARCH_NOTICE}
        assert "Game records" in c.get("/").text


def test_custom_notice_is_shown_and_escaped(tmp_path):
    cfg = make_config(tmp_path, research=True, research_notice="Games <kept> for AI work.")
    with TestClient(create_app(cfg)) as c:
        assert c.get("/server-info").json()["research"]["notice"] == "Games <kept> for AI work."
        d = c.post("/auth/device", json={}).json()
        page = c.get("/link", params={"code": d["user_code"]}).text
        assert "Games &lt;kept&gt; for AI work." in page


def test_research_settings_from_env(tmp_path):
    env = {"DOCTRINE_JOIN_CODE": "x", "DOCTRINE_DATA_DIR": str(tmp_path), "DOCTRINE_RESEARCH": "on",
           "DOCTRINE_RESEARCH_NOTICE": "n", "DOCTRINE_RESEARCH_HOST": "192.168.5.10",
           "DOCTRINE_RESEARCH_PORT": "8081", "DOCTRINE_RESEARCH_TOKEN": TOKEN}
    cfg = Config.from_env(environ=env)
    assert cfg.research and cfg.research_notice_text == "n"
    assert (cfg.research_host, cfg.research_port, cfg.research_token) == ("192.168.5.10", 8081, TOKEN)
    cfg.validate()
    assert TOKEN not in repr(cfg)
    off = Config.from_env(environ={"DOCTRINE_JOIN_CODE": "x", "DOCTRINE_DATA_DIR": str(tmp_path)})
    assert not off.research and off.research_port == 0 and off.research_host == "127.0.0.1"
    assert off.research_notice_text == DEFAULT_RESEARCH_NOTICE


@pytest.mark.parametrize("kw, msg", [
    (dict(research=False, research_port=8081, research_token=TOKEN), "DOCTRINE_RESEARCH=on"),
    (dict(research=True, research_port=8081, research_token="short"), "DOCTRINE_RESEARCH_TOKEN"),
    (dict(research=True, research_port=8080, research_token=TOKEN), "other than"),
    (dict(research=True, research_port=70000, research_token=TOKEN), "other than"),
])
def test_bad_telemetry_listener_settings_refuse_to_start(tmp_path, kw, msg):
    with pytest.raises(ConfigError, match=msg):
        make_config(tmp_path, **kw).validate()


# ---------------------------------------------------------------- client build

def test_turn_meta_client_version_is_stored(client, app, alice, bob):
    g = _finished_game(client, alice, bob)
    conn = db.connect(app.state.db_path)
    try:
        rows = conn.execute("SELECT turn, client_version FROM turns WHERE game_id = ? ORDER BY id",
                            (g["game_id"],)).fetchall()
    finally:
        conn.close()
    assert [(r["turn"], r["client_version"]) for r in rows] == [(0, ""), (0, ""), (1, "0.9.2")]


def test_v1_database_upgrades(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = db.connect(path)
    conn.executescript("BEGIN;\n" + db.MIGRATIONS[0] + "\nPRAGMA user_version = 1;\nCOMMIT;")
    assert db.migrate(conn) == db.SCHEMA_VERSION
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(turns)")]
    assert "client_version" in cols
    conn.close()


# ---------------------------------------------------------------- export-games

def test_export_needs_research_mode(client, app, alice, bob, tmp_path, capsys, monkeypatch):
    _finished_game(client, alice, bob)
    monkeypatch.delenv("DOCTRINE_RESEARCH", raising=False)
    code = cli.main(["export-games", str(tmp_path / "x"), "--data-dir", str(app.state.data_dir)])
    assert code == 2 and "research mode is off" in capsys.readouterr().err
    assert not (tmp_path / "x").exists()


def test_export_contents(client, run, alice, bob, tmp_path):
    g = _finished_game(client, alice, bob)
    out = tmp_path / "export"
    code, text, _ = run("export-games", str(out))
    assert code == 0 and f"exported {g['game_id']}" in text and "1 game(s) exported" in text
    folder = out / g["game_id"]
    _check_record(json.loads((folder / "game.json").read_text(encoding="utf-8")), g, alice, bob)
    assert (folder / "t0_s0.hexturn").read_bytes() == b"bob setup"
    assert (folder / "t0_s1.hexturn").read_bytes() == b"alice setup"
    assert (folder / "t1_s0.hexturn").read_bytes() == b"bob t1"


def test_export_skips_games_in_progress_unless_asked(client, run, alice, bob, tmp_path):
    g = new_game(client, alice, bob, my_side=0, first_side=0)
    upload(client, bob, g["game_id"], 0, 0)
    out = tmp_path / "export"
    code, text, _ = run("export-games", str(out))
    assert code == 0 and f"skipped {g['game_id']}: not over" in text
    code, text, _ = run("export-games", str(out), "--include-active")
    assert code == 0 and f"exported {g['game_id']}" in text


def test_export_one_game(client, run, alice, bob, tmp_path):
    g1 = _finished_game(client, alice, bob)
    _finished_game(client, alice, bob)
    out = tmp_path / "export"
    code, _, _ = run("export-games", str(out), "--game", g1["game_id"])
    assert code == 0 and [p.name for p in out.iterdir()] == [g1["game_id"]]
    code, text, _ = run("export-games", str(out), "--game", "rhine-zzzzzz")
    assert code == 1 and "no such game" in text


# ---------------------------------------------------------------- telemetry listener

def test_listener_needs_the_token(tlm):
    for headers in ({}, {"Authorization": "Bearer wrong-token-xxxxxxxxxx"},
                    {"Authorization": f"Basic {TOKEN}"}):
        r = tlm.get("/research/games", headers=headers)
        assert r.status_code == 401 and r.json() == {"detail": "unauthorized"}, headers
    assert tlm.get("/research/healthz", headers=H).json()["ok"] is True


def test_listener_serves_records(client, tlm, alice, bob):
    g = _finished_game(client, alice, bob)
    active = new_game(client, alice, bob)
    upload(client, bob, active["game_id"], 0, 0)
    listed = tlm.get("/research/games", headers=H).json()["games"]
    assert [x["game_id"] for x in listed] == [g["game_id"]]
    assert listed[0]["turns"] == 3 and "passphrase" not in listed[0]
    with_active = tlm.get("/research/games", params={"include_active": 1}, headers=H).json()["games"]
    assert [x["game_id"] for x in with_active] == [g["game_id"], active["game_id"]]
    rec = tlm.get(f"/research/games/{g['game_id']}", headers=H).json()
    _check_record(rec, g, alice, bob)


def test_listener_bundle_is_a_zip_of_the_record(client, tlm, alice, bob):
    g = _finished_game(client, alice, bob)
    r = tlm.get(f"/research/games/{g['game_id']}/bundle", headers=H)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["game.json", "t0_s0.hexturn", "t0_s1.hexturn", "t1_s0.hexturn"]
    assert z.read("t1_s0.hexturn") == b"bob t1"
    _check_record(json.loads(z.read("game.json")), g, alice, bob)


def test_listener_unknown_games(tlm):
    for path in ("/research/games/rhine-zzzzzz", "/research/games/../etc",
                 "/research/games/rhine-zzzzzz/bundle"):
        assert tlm.get(path, headers=H).status_code == 404, path


def test_listener_is_not_on_the_public_app(client, alice):
    assert client.get("/research/games", headers=H).status_code == 404
