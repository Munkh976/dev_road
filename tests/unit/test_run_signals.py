"""run_signals: cache -> signals table + runs row, and the stale-benchmark halt."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

from src.config import DEFAULT_CONFIG_PATH
from src.data.cache import PriceCache
from src.data.refresh import EXIT_ERROR, EXIT_HALTED, EXIT_OK
from src.strategy.run_signals import run
from tests.support import recent_bars

SYMBOLS = ["AAA", "BBB", "CCC", "DDD"]


def world(cfg, db, end: date | None = None, n: int = 400):
    cache = PriceCache(cfg.cache_path)
    for i, s in enumerate(["SPY"] + SYMBOLS):
        cache.write(s, recent_bars(n, end=end, seed=i))
    db.executemany(
        """INSERT INTO universe_snapshots (snapshot_date, symbol, rank, sector, stock_type,
           avg_dollar_volume_20d, price, days_listed, data_as_of)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [(date.today().isoformat(), s, i, f"Sector{i}", "COMMON", 1.0, 1.0, 1, "2026-01-01")
         for i, s in enumerate(SYMBOLS, start=1)],
    )
    db.commit()
    return cache


def test_writes_a_run_row_with_the_config_hash_and_one_row_per_symbol(refresh_cfg, db, capsys):
    world(refresh_cfg, db)

    assert run(refresh_cfg) == EXIT_OK

    r = db.execute("SELECT * FROM runs WHERE run_type='signals'").fetchone()
    assert r["status"] == "ok" and r["finished_at"]
    assert r["config_hash"] == hashlib.sha256(DEFAULT_CONFIG_PATH.read_bytes()).hexdigest()
    rows = db.execute("SELECT * FROM signals WHERE run_id=? ORDER BY rank", (r["run_id"],)).fetchall()
    assert [x["symbol"] for x in rows if x["symbol"] == "SPY"] == []    # benchmark is not a row
    assert sorted(x["symbol"] for x in rows) == SYMBOLS
    assert [x["rank"] for x in rows] == [1, 2, 3, 4]
    assert all(x["sector"] and x["dollar_volume_20d"] > 0 and x["atr_20"] > 0 for x in rows)
    assert len({x["market_on"] for x in rows}) == 1 and rows[0]["market_on"] in (0, 1)
    assert db.execute("SELECT COUNT(*) FROM v_current_signals").fetchone()[0] == 4


def test_prints_the_regime_and_the_top_symbols(refresh_cfg, db, capsys):
    world(refresh_cfg, db)
    run(refresh_cfg)
    out = capsys.readouterr().out
    assert "Regime: RISK-" in out and "Top 10:" in out
    top = db.execute("SELECT symbol FROM signals WHERE rank=1").fetchone()["symbol"]
    assert top in out


def test_signal_date_is_the_benchmarks_last_bar(refresh_cfg, db):
    world(refresh_cfg, db)
    run(refresh_cfg)
    spy_last = PriceCache(refresh_cfg.cache_path).read("SPY").index[-1].date().isoformat()
    assert db.execute("SELECT DISTINCT signal_date FROM signals").fetchall()[0][0] == spy_last


def test_stale_benchmark_halts_and_writes_no_signals(refresh_cfg, db):
    stale_end = date.today() - timedelta(days=refresh_cfg.data.max_stale_days + 10)
    world(refresh_cfg, db, end=stale_end)

    assert run(refresh_cfg) == EXIT_HALTED

    r = db.execute("SELECT * FROM runs WHERE run_type='signals'").fetchone()
    assert r["status"] == "halted" and "SPY" in r["halt_reason"]
    assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0


def test_without_a_universe_snapshot_the_run_fails_and_says_why(refresh_cfg, db):
    assert run(refresh_cfg) == EXIT_ERROR
    r = db.execute("SELECT * FROM runs WHERE run_type='signals'").fetchone()
    assert r["status"] == "failed" and "rebuild-universe" in r["error"]


def test_a_symbol_with_short_history_is_stored_unranked(refresh_cfg, db):
    cache = world(refresh_cfg, db)
    cache.write("DDD", recent_bars(60, seed=9))          # far too little history

    assert run(refresh_cfg) == EXIT_OK

    d = db.execute("SELECT * FROM signals WHERE symbol='DDD'").fetchone()
    assert d["rank"] is None and d["score"] is None and d["close"] > 0
    ranks = sorted(r[0] for r in db.execute("SELECT rank FROM signals WHERE rank IS NOT NULL"))
    assert ranks == [1, 2, 3]
