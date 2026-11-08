"""Load settings from config/*.yaml and secrets from .env / environment variables.

Precedence (highest first): real environment variables > .env file > YAML defaults.
Only connection host/port and credentials can come from the environment; everything
else is versioned configuration in YAML.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

import yaml
from dotenv import dotenv_values

from scmgen.core.errors import ConfigError

Role = Literal["admin", "app"]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


@dataclass(frozen=True)
class Credentials:
    user: str
    password: str


@dataclass(frozen=True)
class MySQLSettings:
    host: str
    port: int
    charset: str
    collation: str
    connect_timeout_seconds: int
    session_time_zone: str
    min_server_version: str


@dataclass(frozen=True)
class SchemaSettings:
    control_schema: str
    workspace_prefix: str


@dataclass(frozen=True)
class LoggingSettings:
    level: str
    file: Path
    console: bool


@dataclass(frozen=True)
class Settings:
    mysql: MySQLSettings
    schemas: SchemaSettings
    logging: LoggingSettings
    data_dir: Path
    admin_credentials: Credentials | None
    app_credentials: Credentials | None

    def credentials_for(self, role: Role) -> Credentials:
        creds = self.admin_credentials if role == "admin" else self.app_credentials
        if creds is None:
            variable = "MYSQL_ADMIN_USER" if role == "admin" else "MYSQL_USER"
            raise ConfigError(
                f"No {role} MySQL login configured: set {variable} (and its password) in .env"
            )
        return creds


def load_settings(
    config_dir: Path = DEFAULT_CONFIG_DIR,
    env_file: Path | None = DEFAULT_ENV_FILE,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Build Settings from YAML files plus environment.

    `environ` defaults to os.environ; tests pass their own mapping so they never
    depend on the developer's real environment.
    """
    env = _merged_environment(env_file, os.environ if environ is None else environ)
    database = _read_yaml(config_dir / "database.yaml")
    app = _read_yaml(config_dir / "settings.yaml")

    mysql_cfg = _section(database, "mysql", "database.yaml")
    schema_cfg = _section(database, "schemas", "database.yaml")
    log_cfg = _section(app, "logging", "settings.yaml")
    path_cfg = _section(app, "paths", "settings.yaml")

    mysql = MySQLSettings(
        host=env.get("MYSQL_HOST") or _required(mysql_cfg, "host", "mysql"),
        port=_parse_port(env.get("MYSQL_PORT") or _required(mysql_cfg, "port", "mysql")),
        charset=_required(mysql_cfg, "charset", "mysql"),
        collation=_required(mysql_cfg, "collation", "mysql"),
        connect_timeout_seconds=int(_required(mysql_cfg, "connect_timeout_seconds", "mysql")),
        session_time_zone=str(_required(mysql_cfg, "session_time_zone", "mysql")),
        min_server_version=str(_required(mysql_cfg, "min_server_version", "mysql")),
    )
    schemas = SchemaSettings(
        control_schema=_required(schema_cfg, "control_schema", "schemas"),
        workspace_prefix=_required(schema_cfg, "workspace_prefix", "schemas"),
    )
    level = str(_required(log_cfg, "level", "logging")).upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ConfigError(f"logging.level must be DEBUG/INFO/WARNING/ERROR, got {level!r}")
    logging_settings = LoggingSettings(
        level=level,
        file=_project_path(_required(log_cfg, "file", "logging")),
        console=bool(log_cfg.get("console", True)),
    )

    return Settings(
        mysql=mysql,
        schemas=schemas,
        logging=logging_settings,
        data_dir=_project_path(_required(path_cfg, "data_dir", "paths")),
        admin_credentials=_credentials(env, "MYSQL_ADMIN_USER", "MYSQL_ADMIN_PASSWORD"),
        app_credentials=_credentials(env, "MYSQL_USER", "MYSQL_PASSWORD"),
    )


def _merged_environment(env_file: Path | None, environ: Mapping[str, str]) -> dict[str, str]:
    """.env values first, then real environment variables on top (they win)."""
    merged: dict[str, str] = {}
    if env_file is not None and env_file.exists():
        merged.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    merged.update(environ)
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(content, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return content


def _section(cfg: dict[str, Any], name: str, file_name: str) -> dict[str, Any]:
    section = cfg.get(name)
    if not isinstance(section, dict):
        raise ConfigError(f"Section '{name}' missing in {file_name}")
    return section


def _required(section: dict[str, Any], key: str, section_name: str) -> Any:
    value = section.get(key)
    if value is None or value == "":
        raise ConfigError(f"Setting '{section_name}.{key}' is required")
    return value


def _parse_port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"MySQL port must be an integer, got {value!r}") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"MySQL port out of range: {port}")
    return port


def _credentials(env: Mapping[str, str], user_var: str, password_var: str) -> Credentials | None:
    user = env.get(user_var, "").strip()
    if not user:
        return None
    # An empty password is legitimate (fresh Homebrew root), so no "required" check here.
    return Credentials(user=user, password=env.get(password_var, ""))


def _project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path
