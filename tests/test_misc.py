from doctrine_server import __version__


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True, "version": __version__, "discord": "off"}


def test_server_info(client):
    info = client.get("/server-info").json()
    assert info["name"] == "Test Server" and info["version"] == __version__
    assert info["motd"] == "Be nice." and info["auth"] == ["local"]
    assert "discord_invite" not in info


def test_landing_page(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "Test Server" in r.text
    assert "Doctrine dedicated server" in r.text
    assert "Multiplayer &gt; Dedicated Server" in r.text
    assert "http://testserver" in r.text
    assert "not operated by" in r.text and "Straits Games" in r.text


def test_html_is_escaped(tmp_path):
    from conftest import make_config
    from fastapi.testclient import TestClient
    from doctrine_server.main import create_app
    app = create_app(make_config(tmp_path, server_name="<script>x</script>", motd="<b>hi</b>"))
    with TestClient(app) as c:
        text = c.get("/").text
    assert "<script>x</script>" not in text and "&lt;script&gt;" in text
    assert "<b>hi</b>" not in text


def test_errors_are_detail_codes(client):
    r = client.get("/games/x/turn")
    assert r.status_code == 401 and r.json() == {"detail": "unauthorized"}


def test_upload_content_length_guard(client, alice):
    r = client.post("/games/rhine-abcdef/turn", headers={**alice.h, "Content-Length": str(10 ** 9)},
                    content=b"")
    assert r.status_code == 413 and r.json() == {"detail": "too_large"}
