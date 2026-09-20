from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from src.config import DEFAULT_CONFIG_PATH, REPO_ROOT, Config

SCHEMA = REPO_ROOT / "db" / "schema.sql"


@pytest.fixture
def refresh_cfg(tmp_path, monkeypatch) -> Config:
    """Real config.yaml, pointed at a temp cache, DB and constituents file."""
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["universe"]["constituents_path"] = str(tmp_path / "constituents.csv")
    monkeypatch.setenv("WM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("WM_DB_PATH", str(tmp_path / "ops.db"))
    conn = sqlite3.connect(tmp_path / "ops.db")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.close()
    return Config(**raw)


@pytest.fixture
def db(refresh_cfg):
    conn = sqlite3.connect(refresh_cfg.db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def write_constituents(cfg: Config, rows: list[tuple[str, str]], as_of: str) -> Path:
    path = cfg.constituents_path
    lines = ["symbol,name,as_of_date"] + [f"{s},{n},{as_of}" for s, n in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
