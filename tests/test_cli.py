import pytest
from conftest import PASSWORD, link_form, new_game, poll, register, start_device

from doctrine_server import __version__, cli


@pytest.fixture
def run(app, capsys):
    data_dir = str(app.state.data_dir)

    def _run(*args):
        code = cli.main([*args, "--data-dir", data_dir])
        out = capsys.readouterr()
        return code, out.out, out.err
    return _run


def test_version(capsys):
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_players_list_disable_enable(client, run, alice, bob):
    code, out, _ = run("players", "list")
    assert code == 0 and "Alice" in out and "Bob" in out and alice.id in out
    code, out, _ = run("players", "disable", "alice")
    assert code == 0 and "disabled Alice" in out
    assert client.get("/me", headers=alice.h).status_code == 401
    assert "disabled" in run("players", "list")[1]
    run("players", "enable", alice.id)
    assert client.get("/me", headers=alice.h).status_code == 200


def test_unknown_player(run, client):
    with pytest.raises(SystemExit) as e:
        run("players", "disable", "nobody")
    assert "no player" in str(e.value)


def test_reset_password(client, run, alice):
    code, out, _ = run("players", "reset-password", "Alice")
    assert code == 0
    temp = out.split(": ", 1)[1].splitlines()[0].strip()
    d = start_device(client)
    assert link_form(client, d["user_code"], action="signin", name="Alice",
                     password=PASSWORD).status_code == 403
    r = link_form(client, d["user_code"], action="signin", name="Alice", password=temp)
    assert "Linked" in r.text
    assert poll(client, d["device_code"])["status"] == "approved"


def test_keys_revoke(client, run, alice):
    code, out, _ = run("keys", "revoke", "Alice")
    assert code == 0 and "revoked 1 key" in out
    assert client.get("/me", headers=alice.h).status_code == 401


def test_games_list_and_close(client, run, alice, bob):
    g = new_game(client, alice, bob)
    code, out, _ = run("games", "list")
    assert code == 0 and g["game_id"] in out and "Alice vs Bob" in out
    code, out, _ = run("games", "close", g["game_id"])
    assert code == 0 and "closed" in out
    assert client.get(f"/games/{g['game_id']}", headers=alice.h).json()["status"] == "closed"
    assert g["game_id"] not in run("games", "list")[1]
    assert g["game_id"] in run("games", "list", "--all")[1]
    code, _, err = run("games", "close", g["game_id"])
    assert code == 1 and "no active game" in err


def test_serve_refuses_without_auth(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for var in ("DOCTRINE_JOIN_CODE", "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DOCTRINE_DATA_DIR", str(tmp_path / "d"))
    assert cli.main(["serve"]) == 2
    assert "no sign-in method" in capsys.readouterr().err


def test_backup(client, run, app, alice, bob, tmp_path):
    from conftest import upload
    g = new_game(client, alice, bob)
    upload(client, bob, g["game_id"], 0, 0, data=b"setup")
    dest = tmp_path / "bk"
    code, out, _ = run("backup", str(dest))
    assert code == 0 and "backed up" in out
    import sqlite3
    conn = sqlite3.connect(dest / "doctrine.sqlite3")
    assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 1
    conn.close()
    assert (dest / "blobs" / g["game_id"] / "t0_s0.hexturn").read_bytes() == b"setup"
    assert run("backup", str(dest))[0] == 1                     # refuses a non-empty folder
