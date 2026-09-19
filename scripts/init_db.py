#!/usr/bin/env python
"""
Create (or migrate) the SQLite operational database.

Idempotent: safe to re-run. Uses CREATE TABLE IF NOT EXISTS throughout, so
running it against an existing database adds anything missing without
touching your history.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402

SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def main() -> int:
    cfg = load_config()
    db_path = cfg.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.cache_path.mkdir(parents=True, exist_ok=True)

    fresh = not db_path.exists()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA.read_text())

        # Seed the single system_state row. Starts ENABLED but paper-only;
        # the paper_trading guard in config is what keeps real money safe.
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT OR IGNORE INTO system_state
               (id, trading_enabled, updated_at) VALUES (1, 1, ?)""",
            (now,),
        )
        conn.commit()

        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        views = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")]
    finally:
        conn.close()

    print(f"{'Created' if fresh else 'Migrated'} {db_path}")
    print(f"  {len(tables)} tables: {', '.join(tables)}")
    print(f"  {len(views)} views:  {', '.join(views)}")
    print()
    print(f"  mode: {'PAPER' if cfg.account.paper_trading else 'LIVE'}")
    print(f"  cache dir: {cfg.cache_path}")
    print()
    print("Next: make refresh   (populates the price cache; ~25 min)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
