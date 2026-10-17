import pytest

from doctrine_server.config import Config, ConfigError, parse_env_file
from doctrine_server.main import create_app


def test_defaults_from_empty_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = Config.from_env(environ={})
    assert c.host == "0.0.0.0" and c.port == 8080
    assert c.server_name == "Doctrine server"
    assert c.max_turn_bytes == 8 * 1024 * 1024 and c.max_scenario_bytes == 32 * 1024 * 1024
    assert c.data_dir == (tmp_path / "data").resolve()
    assert c.auth_modes() == []


def test_env_file_and_precedence(tmp_path):
    f = tmp_path / "x.env"
    f.write_text('# comment\nexport DOCTRINE_JOIN_CODE="abc def"\nDOCTRINE_PORT=9000\n'
                 "DOCTRINE_MOTD=hello # trailing\nDOCTRINE_PUBLIC_URL=https://d.example.org/\n"
                 "garbage line\n", encoding="utf-8")
    assert parse_env_file(f)["DOCTRINE_JOIN_CODE"] == "abc def"
    c = Config.from_env(f, environ={"DOCTRINE_PORT": "9100"})
    assert c.join_code == "abc def" and c.port == 9100 and c.motd == "hello"
    assert c.public_url == "https://d.example.org"
    assert c.auth_modes() == ["local"]


def test_dotenv_in_working_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("DOCTRINE_SERVER_NAME=From Dotenv\n", encoding="utf-8")
    assert Config.from_env(environ={}).server_name == "From Dotenv"


def test_missing_env_file():
    with pytest.raises(ConfigError):
        Config.from_env("does-not-exist.env", environ={})


def test_bad_number():
    with pytest.raises(ConfigError):
        Config.from_env(environ={"DOCTRINE_PORT": "eighty"})


def test_secrets_not_in_repr():
    c = Config(join_code="sekrit-join", discord_client_secret="sekrit-oauth",
               discord_bot_token="sekrit-token")
    assert "sekrit" not in repr(c)


def test_refuses_to_start_without_auth(tmp_path):
    with pytest.raises(ConfigError, match="no sign-in method"):
        create_app(Config(data_dir=tmp_path))


def test_discord_oauth_needs_public_url(tmp_path):
    with pytest.raises(ConfigError, match="DOCTRINE_PUBLIC_URL"):
        create_app(Config(data_dir=tmp_path, discord_client_id="1", discord_client_secret="s"))


def test_discord_only_is_enough(tmp_path):
    c = Config(data_dir=tmp_path, public_url="https://d.example.org", discord_client_id="1",
               discord_client_secret="s")
    c.validate()
    assert c.auth_modes() == ["discord"]
    assert c.discord_redirect_uri == "https://d.example.org/auth/discord/callback"
