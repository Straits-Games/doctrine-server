"""Notifier wiring: called after commits with a fog-safe game view, and never able to
fail a request."""
from conftest import download, make_config, register, upload
from fastapi.testclient import TestClient

from doctrine_server.main import create_app
from doctrine_server.notify import NullNotifier, Notifier


class Recorder:
    def __init__(self):
        self.calls = []

    def game_created(self, game):
        self.calls.append(("game_created", game))

    def turn_posted(self, game, recipient_side, summary):
        self.calls.append(("turn_posted", game, recipient_side, summary))

    def game_ended(self, game):
        self.calls.append(("game_ended", game))

    def status(self):
        return "connected"


class Exploding:
    def game_created(self, game):
        raise RuntimeError("boom")

    turn_posted = game_ended = lambda self, *a: (_ for _ in ()).throw(RuntimeError("boom"))

    def status(self):
        raise RuntimeError("boom")


def _client(tmp_path, notifier):
    return TestClient(create_app(make_config(tmp_path), notifier=notifier))


def test_protocol_conformance():
    assert isinstance(NullNotifier(), Notifier)
    assert isinstance(Recorder(), Notifier)
    assert NullNotifier().status() == "off"


def test_events_are_reported(tmp_path):
    rec = Recorder()
    with _client(tmp_path, rec) as client:
        alice, bob = register(client, "Alice"), register(client, "Bob")
        g = client.post("/games", json={"scenario_id": "rhine", "opponent_uid": bob.id,
                                        "my_side": 0}, headers=alice.h).json()
        upload(client, bob, g["game_id"], 0, 0, summary="Deployment done")
        client.post(f"/games/{g['game_id']}/resign", headers=alice.h)
        client.post(f"/games/{g['game_id']}/resign", headers=alice.h)   # no second event
        assert client.get("/healthz").json()["discord"] == "connected"
    names = [c[0] for c in rec.calls]
    assert names == ["game_created", "turn_posted", "game_ended"]
    _, view, recipient, summary = rec.calls[1]
    assert recipient == 0 and summary == "Deployment done"
    assert view["players"][0] == {"id": alice.id, "name": "Alice", "kind": "local"}
    assert "passphrase" not in view
    assert rec.calls[2][1]["status"] == "resigned" and rec.calls[2][1]["winner"] == 1


def test_raising_notifier_does_not_fail_requests(tmp_path):
    with _client(tmp_path, Exploding()) as client:
        alice, bob = register(client, "Alice"), register(client, "Bob")
        r = client.post("/games", json={"scenario_id": "rhine", "opponent_uid": bob.id,
                                        "my_side": 0}, headers=alice.h)
        assert r.status_code == 200
        gid = r.json()["game_id"]
        r = upload(client, bob, gid, 0, 0, data=b"setup")
        assert r.status_code == 200 and r.json()["ok"] is True
        assert download(client, alice, gid).content == b"setup"
        assert client.post(f"/games/{gid}/result", headers=alice.h,
                           json={"winner": 0, "level": "", "reason": ""}).status_code == 200
        assert client.get("/healthz").json()["discord"] == "disconnected"
