from __future__ import annotations

from pathlib import Path

import pytest

from scmgen.core.errors import ConfigError
from scmgen.core.settings import load_settings


def test_yaml_defaults_are_loaded(config_dir: Path) -> None:
    settings = load_settings(config_dir, env_file=None, environ={})
    assert settings.mysql.host == "127.0.0.1"
    assert settings.mysql.port == 3306
    assert settings.mysql.session_time_zone == "+05:30"
    assert settings.schemas.control_schema == "scm_control"
    assert settings.logging.level == "INFO"  # normalised to upper case
    assert settings.admin_credentials is None
    assert settings.app_credentials is None


def test_environment_overrides_host_port_and_supplies_credentials(config_dir: Path) -> None:
    environ = {
        "MYSQL_HOST": "db.internal",
        "MYSQL_PORT": "3307",
        "MYSQL_ADMIN_USER": "root",
        "MYSQL_ADMIN_PASSWORD": "",
        "MYSQL_USER": "scm_app",
        "MYSQL_PASSWORD": "s3cret",
    }
    settings = load_settings(config_dir, env_file=None, environ=environ)
    assert (settings.mysql.host, settings.mysql.port) == ("db.internal", 3307)
    assert settings.credentials_for("admin").password == ""  # empty password is allowed
    assert settings.credentials_for("app").user == "scm_app"


def test_real_environment_wins_over_env_file(config_dir: Path, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MYSQL_USER=from_file\nMYSQL_PASSWORD=file_pw\n")
    settings = load_settings(config_dir, env_file=env_file, environ={"MYSQL_USER": "from_env"})
    assert settings.credentials_for("app").user == "from_env"
    assert settings.credentials_for("app").password == "file_pw"


def test_missing_login_gives_actionable_error(config_dir: Path) -> None:
    settings = load_settings(config_dir, env_file=None, environ={})
    with pytest.raises(ConfigError, match="MYSQL_USER"):
        settings.credentials_for("app")


def test_missing_config_file_is_reported(config_dir: Path) -> None:
    (config_dir / "database.yaml").unlink()
    with pytest.raises(ConfigError, match="database.yaml"):
        load_settings(config_dir, env_file=None, environ={})


@pytest.mark.parametrize("port", ["abc", "0", "70000"])
def test_invalid_port_is_rejected(config_dir: Path, port: str) -> None:
    with pytest.raises(ConfigError, match="port"):
        load_settings(config_dir, env_file=None, environ={"MYSQL_PORT": port})


def test_invalid_log_level_is_rejected(config_dir: Path) -> None:
    path = config_dir / "settings.yaml"
    path.write_text(path.read_text().replace("level: info", "level: chatty"))
    with pytest.raises(ConfigError, match="logging.level"):
        load_settings(config_dir, env_file=None, environ={})
