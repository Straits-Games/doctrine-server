"""The turn relay. The four `test_regression_*` tests pin the three deadlocks the old
server had plus a whole game; keep them."""
from conftest import download, game_of, new_game, upload

from doctrine_server import db, games
from doctrine_server.errors import ApiError


def _cursor(app, game_id, player):
    conn = db.connect(app.state.db_path)
    try:
        return games.get_cursor(conn, game_id, player.id)
    finally:
        conn.close()


def _ok(r, turn=None):
    assert r.status_code == 200, r.text
    if turn is not None:
        assert r.headers["x-hexfront-turn"] == str(turn)
    return r


# ------------------------------------------------------------- regression tests

def test_regression_a_defender_setup_accepted_when_attacker_moves_first(client, alice, bob):
    """Deadlock #1: the defender places first; its turn-0 upload must be accepted while
    the attacker is `first_side` (and must work for either first_side)."""
    for first_side in (0, 1):
        g = new_game(client, alice, bob, my_side=0, first_side=first_side)
        attacker = alice if first_side == 0 else bob
        defender = bob if first_side == 0 else alice
        defender_side = 1 - first_side
        assert g["active_side"] == defender_side            # setup is defender-first
        assert game_of(client, defender, g["game_id"])["your_turn"] is True
        assert game_of(client, attacker, g["game_id"])["your_turn"] is False
        r = upload(client, defender, g["game_id"], 0, first_side, summary="Deployment")
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "turn": 0, "active_side": first_side}
        assert game_of(client, attacker, g["game_id"])["your_turn"] is True
        assert game_of(client, defender, g["game_id"])["your_turn"] is False


def test_regression_a2_setup_upload_exempt_from_turn_ownership(client, alice, bob):
    """Turn 0 is sealed-simultaneous: either side may upload its setup out of order."""
    g = new_game(client, alice, bob, my_side=0, first_side=0)    # alice attacker, bob places first
    r = upload(client, alice, g["game_id"], 0, 1)                # attacker first: still accepted
    assert r.status_code == 200, r.text
    assert upload(client, bob, g["game_id"], 0, 0).status_code == 200
    assert upload(client, bob, g["game_id"], 0, 0).json() == {"detail": "duplicate_turn"}


def test_regression_b_attacker_refetches_setup(client, app, alice, bob):
    """Deadlock #2: a client restarted mid-setup must be able to fetch the defender's
    turn-0 file again — both on a plain cursor poll and with ?since=0."""
    g = new_game(client, alice, bob, my_side=0, first_side=0)    # alice = attacker
    gid = g["game_id"]
    assert download(client, alice, gid).status_code == 204
    upload(client, bob, gid, 0, 0, data=b"defender setup", filename="rhine_t00_s0.hexturn")
    r = _ok(download(client, alice, gid), turn=0)
    assert r.content == b"defender setup"
    assert r.headers["x-hexfront-side"] == "0"
    assert r.headers["x-hexfront-filename"] == "rhine_t00_s0.hexturn"
    assert r.headers["content-type"] == "application/octet-stream"
    assert _cursor(app, gid, alice) == 0
    # restart: the cursor already passed turn 0, but the setup file is re-delivered
    assert _ok(download(client, alice, gid), turn=0).content == b"defender setup"
    assert _ok(download(client, alice, gid, since=0), turn=0).content == b"defender setup"
    # the defender has nothing to fetch meanwhile
    assert download(client, bob, gid).status_code == 204


def test_regression_c_same_numbered_reply_reaches_first_mover(client, app, alice, bob):
    """Deadlock #3: both player-turns of a round share turn N. The round's first mover
    uploads N, the reply is also numbered N, and a cursor poll must deliver it. The
    old server bumped the uploader's cursor to N on upload, hiding the reply forever."""
    g = new_game(client, alice, bob, my_side=0, first_side=0)    # alice moves first
    gid = g["game_id"]
    upload(client, bob, gid, 0, 0)                               # defender setup
    _ok(download(client, alice, gid), turn=0)
    assert _ok(upload(client, alice, gid, 1, 1)).json()["turn"] == 1   # round 1, first mover
    assert _cursor(app, gid, alice) == 0                         # upload never moves a cursor
    assert download(client, alice, gid).status_code == 204       # nothing new yet
    _ok(download(client, bob, gid), turn=1)
    _ok(upload(client, bob, gid, 1, 0, data=b"reply in round 1"))    # same number N=1
    r = _ok(download(client, alice, gid), turn=1)                # plain cursor poll
    assert r.content == b"reply in round 1"
    assert r.headers["x-hexfront-side"] == "0"
    assert _cursor(app, gid, alice) == 1
    assert download(client, alice, gid).status_code == 204       # delivered once on the cursor


def test_regression_d_full_game_flow(client, app, alice, bob):
    """Setup -> turns 1..3 -> result, numbered exactly the way the game client numbers
    files (the round's second mover uploads with the already-incremented turn)."""
    g = new_game(client, alice, bob, my_side=0, first_side=0)    # alice attacker (moves first)
    gid = g["game_id"]
    assert g["turn"] == 0 and g["active_side"] == 1 and len(g["passphrase"]) == 24

    def your_turn(p):
        return game_of(client, p, gid)["your_turn"]

    # setup: defender deploys and uploads t0; attacker deploys, then plays turn 1
    upload(client, bob, gid, 0, 0, summary="Deployment")
    assert your_turn(alice) and not your_turn(bob)
    assert _ok(download(client, alice, gid), turn=0)

    sends = [  # (sender, turn number on the file, recipient side)
        (alice, 1, 1), (bob, 2, 0),
        (alice, 2, 1), (bob, 3, 0),
        (alice, 3, 1), (bob, 4, 0),
    ]
    for sender, turn, recipient in sends:
        receiver = bob if sender is alice else alice
        assert your_turn(sender) and not your_turn(receiver)
        payload = f"{sender.name} turn {turn}".encode()
        r = upload(client, sender, gid, turn, recipient, data=payload,
                   summary=f"{sender.name} moved")
        assert r.status_code == 200, (turn, r.text)
        assert r.json()["active_side"] == recipient
        # the sender, waiting, sees nothing new
        assert download(client, sender, gid).status_code == 204
        assert your_turn(receiver) and not your_turn(sender)
        got = _ok(download(client, receiver, gid), turn=turn)
        assert got.content == payload and got.headers["x-hexfront-side"] == str(recipient)
        assert download(client, receiver, gid).status_code == 204
        # a resume pull (since=0) returns the newest file addressed to the receiver
        assert _ok(download(client, receiver, gid, since=0), turn=turn).content == payload

    assert game_of(client, alice, gid)["turn"] == 4
    r = client.post(f"/games/{gid}/result", headers=alice.h,
                    json={"winner": 0, "level": "decisive", "reason": "objectives"})
    assert r.status_code == 200 and r.json()["status"] == "finished" and r.json()["winner"] == 0
    # the loser may still download the deciding file after the game is over
    assert download(client, bob, gid, since=0).status_code == 200
    # late reports are idempotent: first report wins
    r = client.post(f"/games/{gid}/result", headers=bob.h,
                    json={"winner": 1, "level": "x", "reason": "y"})
    assert r.status_code == 200 and r.json()["winner"] == 0
    assert not your_turn(alice) and not your_turn(bob)
    assert upload(client, bob, gid, 4, 0).json() == {"detail": "not_active"}


# ------------------------------------------------------------- upload rules

def _past_setup(client, alice, bob):
    g = new_game(client, alice, bob, my_side=0, first_side=0)
    upload(client, bob, g["game_id"], 0, 0)
    return g["game_id"]


def test_bad_active_side(client, alice, bob):
    gid = _past_setup(client, alice, bob)
    r = upload(client, alice, gid, 1, 0)                         # must hand to side 1
    assert r.status_code == 400 and r.json() == {"detail": "bad_active_side"}


def test_out_of_turn(client, alice, bob):
    gid = _past_setup(client, alice, bob)
    r = upload(client, bob, gid, 1, 0)                           # alice is to move
    assert r.status_code == 409 and r.json() == {"detail": "out_of_turn"}


def test_duplicate_turn(client, alice, bob):
    gid = _past_setup(client, alice, bob)
    upload(client, alice, gid, 1, 1)
    upload(client, bob, gid, 2, 0)
    r = upload(client, alice, gid, 1, 1)
    assert r.status_code == 409 and r.json() == {"detail": "duplicate_turn"}


def test_stale_turn(client, alice, bob):
    gid = _past_setup(client, alice, bob)
    upload(client, alice, gid, 1, 1)
    upload(client, bob, gid, 4, 0)                               # clients may jump ahead
    r = upload(client, alice, gid, 2, 1)
    assert r.status_code == 400 and r.json() == {"detail": "stale_turn"}
    r = upload(client, alice, gid, -1, 1)
    assert r.status_code == 400 and r.json() == {"detail": "stale_turn"}
    assert upload(client, alice, gid, 3, 1).status_code == 200   # game.turn - 1 is fine


def test_bad_meta_and_missing_parts(client, alice, bob):
    gid = _past_setup(client, alice, bob)
    r = client.post(f"/games/{gid}/turn", headers=alice.h, data={"meta": "{not json"},
                    files={"file": ("x.hexturn", b"x")})
    assert r.status_code == 400 and r.json() == {"detail": "bad_meta"}
    r = client.post(f"/games/{gid}/turn", headers=alice.h, data={"meta": '{"turn": "1"}'},
                    files={"file": ("x.hexturn", b"x")})
    assert r.json() == {"detail": "bad_meta"}
    r = client.post(f"/games/{gid}/turn", headers=alice.h, data={"meta": "{}"})
    assert r.status_code == 400 and r.json() == {"detail": "bad_request"}


def test_turn_too_large(client, app, alice, bob):
    gid = _past_setup(client, alice, bob)
    app.state.config.max_turn_mb = 0.001                         # ~1 KB
    r = upload(client, alice, gid, 1, 1, data=b"x" * 5000)
    assert r.status_code == 413 and r.json() == {"detail": "too_large"}


def test_non_participant_gets_404(client, alice, bob):
    from conftest import register
    carol = register(client, "Carol")
    gid = _past_setup(client, alice, bob)
    assert upload(client, carol, gid, 1, 1).status_code == 404
    assert download(client, carol, gid).status_code == 404
    assert client.get(f"/games/{gid}", headers=carol.h).status_code == 404
    assert client.get("/games/..%2F..%2Fetc/turn", headers=alice.h).status_code == 404


def test_filename_sanitized(client, app, alice, bob):
    gid = _past_setup(client, alice, bob)
    upload(client, alice, gid, 1, 1, filename="..\\..\\evil name!.hexturn")
    r = _ok(download(client, bob, gid))
    assert r.headers["x-hexfront-filename"] == "evilname.hexturn"
    upload(client, bob, gid, 2, 0, filename="..")
    assert _ok(download(client, alice, gid)).headers["x-hexfront-filename"] == f"{gid}_t2_s0.hexturn"
    # blobs land only at the server-chosen path
    blobs = sorted(p.name for p in (app.state.data_dir / "blobs" / gid).iterdir())
    assert blobs == ["t0_s0.hexturn", "t1_s1.hexturn", "t2_s0.hexturn"]


def test_sanitize_filename_unit():
    fb = "fallback.hexturn"
    assert games.sanitize_filename("rhine_t02_s1.hexturn", fb) == "rhine_t02_s1.hexturn"
    assert games.sanitize_filename("/a/b/c.hexturn", fb) == "c.hexturn"
    assert games.sanitize_filename(".hidden", fb) == fb
    assert games.sanitize_filename("", fb) == fb
    assert len(games.sanitize_filename("a" * 300, fb)) == 96


def test_summary_truncated_and_stored(client, app, alice, bob):
    gid = _past_setup(client, alice, bob)
    upload(client, alice, gid, 1, 1, summary="s" * 900)
    conn = db.connect(app.state.db_path)
    summary = conn.execute("SELECT summary FROM turns WHERE turn = 1").fetchone()[0]
    conn.close()
    assert len(summary) == 500


def test_since_overrides_cursor(client, alice, bob):
    gid = _past_setup(client, alice, bob)
    upload(client, alice, gid, 1, 1, data=b"one")
    upload(client, bob, gid, 2, 0, data=b"two")
    upload(client, alice, gid, 2, 1, data=b"three")
    # bob never downloaded: newest file addressed to bob wins
    assert _ok(download(client, bob, gid), turn=2).content == b"three"
    assert _ok(download(client, bob, gid, since=0), turn=2).content == b"three"
    assert download(client, bob, gid, since=2).status_code == 204
    # since < 0 means "use the stored cursor" (now 2): nothing new
    assert download(client, bob, gid, since=-1).status_code == 204
    assert download(client, bob, gid).status_code == 204
    # since=1 still finds the newest file above it
    assert _ok(download(client, bob, gid, since=1), turn=2).content == b"three"


def test_resign(client, alice, bob):
    gid = _past_setup(client, alice, bob)
    r = client.post(f"/games/{gid}/resign", headers=bob.h)
    assert r.status_code == 200
    assert r.json()["status"] == "resigned" and r.json()["winner"] == 0
    assert "passphrase" not in r.json()
    r = client.post(f"/games/{gid}/resign", headers=alice.h)     # idempotent
    assert r.status_code == 200 and r.json()["winner"] == 0
    r = client.post(f"/games/{gid}/result", headers=alice.h, json={"winner": 1, "level": "", "reason": ""})
    assert r.json()["status"] == "resigned"


def test_result_validation(client, alice, bob):
    gid = _past_setup(client, alice, bob)
    for body in ({"winner": 2}, {"winner": "0"}, {}, {"winner": True}):
        r = client.post(f"/games/{gid}/result", headers=alice.h, json=body)
        assert r.status_code == 400 and r.json() == {"detail": "bad_winner"}


def test_logic_raises_api_errors_directly(app):
    conn = db.connect(app.state.db_path)
    try:
        try:
            games.fetch_turn(conn, app.state.data_dir, "nope-abcdef", "lx")
        except ApiError as e:
            assert (e.status, e.code) == (404, "not_found")
        else:
            raise AssertionError("expected ApiError")
    finally:
        conn.close()
