"""Pure-Python parts of `db init`: passwords, .env writing, SQL splitting, grant plan."""

from __future__ import annotations

import string
from pathlib import Path

import pytest
from dotenv import dotenv_values

from scmgen.core.env_file import generate_password, upsert_env_value
from scmgen.core.errors import ConfigError
from scmgen.core.settings import load_settings
from scmgen.db.bootstrap import grant_statements, like_escape
from scmgen.db.sql_files import read_sql, split_statements


def test_generated_password_has_every_character_class() -> None:
    for _ in range(200):  # randomness: check many samples, not one
        pw = generate_password()
        assert len(pw) == 32
        assert any(c in string.ascii_uppercase for c in pw)
        assert any(c in string.ascii_lowercase for c in pw)
        assert any(c in string.digits for c in pw)
        assert any(c in "_-." for c in pw)
        assert not set(pw) - set(string.ascii_letters + string.digits + "_-.")


def test_generated_passwords_are_unique() -> None:
    assert len({generate_password() for _ in range(500)}) == 500


def test_upsert_env_value_replaces_and_appends_preserving_comments(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# keep me\nMYSQL_ADMIN_USER=root\nMYSQL_PASSWORD=old\n")
    upsert_env_value(env, "MYSQL_PASSWORD", "new_pw-1.")
    upsert_env_value(env, "MYSQL_READER_PASSWORD", "reader_pw")
    content = env.read_text()
    assert "# keep me" in content
    values = dotenv_values(env)
    assert values["MYSQL_PASSWORD"] == "new_pw-1."
    assert values["MYSQL_READER_PASSWORD"] == "reader_pw"
    assert values["MYSQL_ADMIN_USER"] == "root"


def test_upsert_creates_missing_file_private(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    upsert_env_value(env, "K", "v")
    assert dotenv_values(env)["K"] == "v"
    assert (env.stat().st_mode & 0o077) == 0  # no group/other access


def test_split_statements_ignores_comment_lines() -> None:
    sql = "-- header; with semicolon\nCREATE TABLE a (x INT);\n  -- inner comment\nCREATE TABLE b (y INT);\n"
    assert split_statements(sql) == ["CREATE TABLE a (x INT)", "CREATE TABLE b (y INT)"]


def test_control_ddl_is_packaged_and_has_one_statement() -> None:
    statements = split_statements(read_sql("ddl/control/0001_workspaces.sql"))
    assert len(statements) == 1
    assert statements[0].startswith("CREATE TABLE IF NOT EXISTS workspaces")


def test_like_escape() -> None:
    assert like_escape("scm_control") == "scm\\_control"
    assert like_escape("a%b") == "a\\%b"


def test_grant_plan_matches_design(config_dir: Path) -> None:
    settings = load_settings(config_dir, env_file=None, environ={})
    plan = grant_statements(settings)
    assert plan == [
        ("app", "GRANT ALL PRIVILEGES ON `scm\\_%`.* TO 'scm_app'@'localhost'"),
        ("reader", "GRANT SELECT, SHOW VIEW ON `scm\\_%`.* TO 'scm_reader'@'localhost'"),
        ("anomaly", "GRANT SELECT, SHOW VIEW ON `scm\\_%\\_oltp`.* TO 'scm_anomaly'@'localhost'"),
        ("anomaly", "GRANT SELECT, SHOW VIEW ON `scm\\_control`.* TO 'scm_anomaly'@'localhost'"),
        ("anomaly", "GRANT ALL PRIVILEGES ON `scm\\_%\\_sim`.* TO 'scm_anomaly'@'localhost'"),
    ]


def test_unsafe_schema_prefix_is_refused(config_dir: Path) -> None:
    path = config_dir / "database.yaml"
    path.write_text(path.read_text().replace("workspace_prefix: scm_", "workspace_prefix: \"scm`; DROP\""))
    settings = load_settings(config_dir, env_file=None, environ={})
    with pytest.raises(ConfigError, match="Unsafe"):
        grant_statements(settings)
