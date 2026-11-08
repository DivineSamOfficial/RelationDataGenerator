from __future__ import annotations

from pathlib import Path

import pytest

DATABASE_YAML = """
mysql:
  host: 127.0.0.1
  port: 3306
  charset: utf8mb4
  collation: utf8mb4_0900_ai_ci
  connect_timeout_seconds: 5
  session_time_zone: "+05:30"
  min_server_version: "8.0.16"
schemas:
  control_schema: scm_control
  workspace_prefix: scm_
users:
  host: localhost
  app: scm_app
  reader: scm_reader
  anomaly: scm_anomaly
"""

SETTINGS_YAML = """
logging:
  level: info
  file: logs/test.log
  console: false
paths:
  data_dir: data
"""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A throwaway config/ directory with valid default files."""
    directory = tmp_path / "config"
    directory.mkdir()
    (directory / "database.yaml").write_text(DATABASE_YAML)
    (directory / "settings.yaml").write_text(SETTINGS_YAML)
    return directory
