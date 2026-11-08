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
    assert settings.users.app == "scm_app"
    assert settings.logging.level == "INFO"  # normalised to upper case
    assert all(creds is None for creds in settings.credentials.values())


def test_environment_overrides_host_port_and_supplies_credentials(config_dir: Path) -> None:
    environ = {
        "MYSQL_HOST": "db.internal",
        "MYSQL_PORT": "3307",
        "MYSQL_ADMIN_USER": "root",
        "MYSQL_ADMIN_PASSWORD": "",
        "MYSQL_PASSWORD": "app_pw",
        "MYSQL_READER_PASSWORD": "reader_pw",
    }
    settings = load_settings(config_dir, env_file=None, environ=environ)
    assert (settings.mysql.host, settings.mysql.port) == ("db.internal", 3307)
    assert settings.credentials_for("admin").password == ""  # empty admin password is allowed
    assert settings.credentials_for("app").user == "scm_app"  # user name from YAML
    assert settings.credentials_for("reader").password == "reader_pw"
    assert settings.credentials["anomaly"] is None


def test_user_name_can_be_overridden_from_environment(config_dir: Path) -> None:
    settings = load_settings(
        config_dir, env_file=None, environ={"MYSQL_USER": "custom_app", "MYSQL_PASSWORD": "pw"}
    )
    assert settings.credentials_for("app").user == "custom_app"


def test_real_environment_wins_over_env_file(config_dir: Path, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MYSQL_PASSWORD=file_pw\nMYSQL_READER_PASSWORD=file_reader\n")
    settings = load_settings(config_dir, env_file=env_file, environ={"MYSQL_PASSWORD": "env_pw"})
    assert settings.credentials_for("app").password == "env_pw"
    assert settings.credentials_for("reader").password == "file_reader"


def test_missing_service_password_points_to_db_init(config_dir: Path) -> None:
    settings = load_settings(config_dir, env_file=None, environ={})
    with pytest.raises(ConfigError, match=r"MYSQL_PASSWORD.*scmgen db init"):
        settings.credentials_for("app")


def test_missing_admin_login_is_reported(config_dir: Path) -> None:
    settings = load_settings(config_dir, env_file=None, environ={})
    with pytest.raises(ConfigError, match="MYSQL_ADMIN_USER"):
        settings.credentials_for("admin")


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
