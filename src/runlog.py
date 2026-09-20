"""The `runs` table: one row per pipeline execution, the spine of the audit
trail. Shared by every entry point so the config hash is computed one way."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone

from src.config import DEFAULT_CONFIG_PATH, Config


def config_hash() -> str:
    """sha256 of config.yaml as it is on disk right now."""
    return hashlib.sha256(DEFAULT_CONFIG_PATH.read_bytes()).hexdigest()


def start_run(conn: sqlite3.Connection, cfg: Config, run_type: str, notes: str) -> str:
    run_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            """INSERT INTO runs (run_id, started_at, run_type, strategy_name,
               strategy_version, config_hash, mode, status, notes)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (run_id, datetime.now(timezone.utc).isoformat(), run_type,
             cfg.strategy.name, cfg.strategy.version, config_hash(),
             "paper" if cfg.account.paper_trading else "live", "running", notes),
        )
    return run_id


def finish_run(
    conn: sqlite3.Connection, run_id: str, status: str,
    halt_reason: str | None = None, error: str | None = None,
    notes: str | None = None,
) -> None:
    """`notes`, if given, replaces the note written at start (e.g. a verdict)."""
    with conn:
        conn.execute(
            "UPDATE runs SET finished_at=?, status=?, halt_reason=?, error=?, "
            "notes=COALESCE(?, notes) WHERE run_id=?",
            (datetime.now(timezone.utc).isoformat(), status, halt_reason, error, notes, run_id),
        )
