"""Loader configuration: strategy per table (``config/loader/tables.yaml``; 1G §7, D69)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from scmgen.core.errors import ConfigError
from scmgen.core.settings import DEFAULT_CONFIG_DIR

STRATEGIES = ("FULL", "APPEND", "MERGE")
WATERMARK_COLUMN = {"APPEND": "created_at", "MERGE": "updated_at"}


@dataclass(frozen=True)
class LoaderConfig:
    strategies: dict[str, str]          # table → FULL / APPEND / MERGE
    batch_size: int
    retries: int
    duckdb_threads: int

    def watermark_column(self, table: str) -> str | None:
        return WATERMARK_COLUMN.get(self.strategies[table])


def load_loader_config(config_dir: Path = DEFAULT_CONFIG_DIR) -> LoaderConfig:
    path = Path(config_dir) / "loader" / "tables.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Can't read {path}: {exc}") from exc
    unknown = sorted(set(raw) - {"defaults", *STRATEGIES})
    if unknown:
        raise ConfigError(f"loader/tables.yaml: unknown section(s) {unknown}")
    strategies: dict[str, str] = {}
    for strategy in STRATEGIES:
        for table in raw.get(strategy) or []:
            if table in strategies:
                raise ConfigError(f"loader/tables.yaml: {table} is listed under {strategies[table]} and {strategy}")
            strategies[str(table)] = strategy
    defaults = raw.get("defaults") or {}
    batch = defaults.get("batch_size", 50000)
    retries = defaults.get("retries", 3)
    threads = defaults.get("duckdb_threads", 4)
    for name, value, low, high in (("batch_size", batch, 100, 1_000_000), ("retries", retries, 0, 10),
                                   ("duckdb_threads", threads, 1, 64)):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ConfigError(f"loader/tables.yaml: defaults.{name} must be a whole number in [{low}, {high}]")
    return LoaderConfig(strategies, batch, retries, threads)
