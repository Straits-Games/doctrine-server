"""The core server must import and run without the optional discord.py extra."""
import subprocess
import sys
import textwrap


def _run(code: str, tmp_path):
    script = textwrap.dedent("""
        import sys
        sys.modules["discord"] = None   # makes `import discord` raise ImportError
        import importlib.util
        _orig = importlib.util.find_spec
        importlib.util.find_spec = lambda name, *a: None if name == "discord" else _orig(name, *a)
    """) + textwrap.dedent(code)
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          cwd=str(tmp_path), timeout=120)


def test_app_runs_without_discord_py(tmp_path):
    r = _run(f"""
        from fastapi.testclient import TestClient
        from doctrine_server.config import Config
        from doctrine_server.main import create_app
        cfg = Config(join_code="x", data_dir={str(tmp_path / 'd')!r},
                     discord_client_id="1", discord_client_secret="s",
                     public_url="http://testserver")
        with TestClient(create_app(cfg)) as c:
            assert c.get("/healthz").json()["discord"] == "off"
            assert c.get("/server-info").json()["auth"] == ["local", "discord"]
        print("OK")
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_bot_token_without_discord_py_is_a_clear_error(tmp_path):
    r = _run(f"""
        from doctrine_server.config import Config, ConfigError
        from doctrine_server.main import create_app
        try:
            create_app(Config(join_code="x", data_dir={str(tmp_path / 'd')!r},
                              discord_bot_token="secret-token-value"))
        except ConfigError as e:
            print("ERR", e)
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "ERR" in r.stdout and "doctrine-server[discord]" in r.stdout
    assert "secret-token-value" not in r.stdout + r.stderr
