"""End-to-end refresh flow against a mocked IB: cadences and the data gate."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from src.data.cache import PriceCache
from src.data.refresh import EXIT_ERROR, EXIT_HALTED, EXIT_OK, refresh
from tests.conftest import write_constituents
from tests.support import FakeIB, FakeLimiter, details, recent_bars


def snapshot(db, symbols):
    db.executemany(
        """INSERT INTO universe_snapshots (snapshot_date, symbol, rank, stock_type,
           avg_dollar_volume_20d, price, days_listed, data_as_of)
           VALUES (?,?,?,?,?,?,?,?)""",
        [(date.today().isoformat(), s, i, "COMMON", 1.0, 1.0, 1, "2026-01-01")
         for i, s in enumerate(symbols, start=1)],
    )
    db.commit()


def latest(db, table):
    return db.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()


# ------------------------------------------------------------------ weekly


def test_weekly_refresh_fetches_snapshot_plus_spy_benchmark_first(refresh_cfg, db):
    snapshot(db, ["AAA", "BBB"])
    ib = FakeIB({s: recent_bars(400, seed=i) for i, s in enumerate(["SPY", "AAA", "BBB", "ZZZ"])})

    assert refresh(refresh_cfg, ib=ib, limiter=FakeLimiter()) == EXIT_OK

    assert [s for s, _ in ib.requests] == ["SPY", "AAA", "BBB"]   # ZZZ is not in the universe
    assert latest(db, "runs")["status"] == "ok"
    dq = latest(db, "data_quality")
    assert dq["passed"] == 1 and dq["symbols_expected"] == 3 and dq["symbols_fetched"] == 3


def test_weekly_refresh_without_a_snapshot_fails_and_says_why(refresh_cfg, db):
    ib = FakeIB({"SPY": recent_bars(400)})
    assert refresh(refresh_cfg, ib=ib, limiter=FakeLimiter()) == EXIT_ERROR
    run = latest(db, "runs")
    assert run["status"] == "failed" and "rebuild-universe" in run["error"]
    assert ib.requests == []


def test_summary_counts_refetches_and_they_are_stored(refresh_cfg, db):
    snapshot(db, ["AAA", "BBB"])
    hist = {s: recent_bars(400, seed=i) for i, s in enumerate(["SPY", "AAA", "BBB"])}
    cache = PriceCache(refresh_cfg.cache_path)
    for s in hist:
        cache.write(s, hist[s].iloc[:-5])
    ib = FakeIB(hist)
    ib.scale["BBB"] = 0.98

    assert refresh(refresh_cfg, ib=ib, limiter=FakeLimiter()) == EXIT_OK

    detail = json.loads(latest(db, "data_quality")["detail"])
    assert detail["refetch_count"] == 1
    assert detail["refetches"][0]["symbol"] == "BBB"
    assert "restated" in detail["refetches"][0]["reason"]


def test_pacing_violation_is_recorded_and_not_retried(refresh_cfg, db):
    snapshot(db, ["AAA", "BBB"])
    ib = FakeIB({s: recent_bars(400, seed=i) for i, s in enumerate(["SPY", "AAA", "BBB"])})
    ib.errors["AAA"] = [(420, "pacing")]
    limiter = FakeLimiter()

    assert refresh(refresh_cfg, ib=ib, limiter=limiter) == EXIT_OK   # SPY is fine

    assert limiter.cooldowns == 1
    assert [s for s, _ in ib.requests].count("AAA") == 1             # no retry
    detail = json.loads(latest(db, "data_quality")["detail"])
    assert detail["pacing_violations"] == 1 and "AAA" in detail["failed"]
    assert latest(db, "data_quality")["symbols_fetched"] == 2


def test_failed_non_benchmark_symbol_does_not_halt(refresh_cfg, db):
    snapshot(db, ["AAA"])
    ib = FakeIB({"SPY": recent_bars(400)})          # AAA unknown to IBKR -> error 200
    assert refresh(refresh_cfg, ib=ib, limiter=FakeLimiter()) == EXIT_OK
    dq = latest(db, "data_quality")
    assert dq["passed"] == 1 and dq["symbols_fetched"] == 1 and dq["symbols_expected"] == 2


# ---------------------------------------------------------------- data gate


def test_stale_spy_halts_with_a_failing_data_quality_row(refresh_cfg, db):
    snapshot(db, ["AAA"])
    old_end = date.today() - timedelta(days=refresh_cfg.data.max_stale_days + 10)
    ib = FakeIB({"SPY": recent_bars(400, end=old_end), "AAA": recent_bars(400)})

    assert refresh(refresh_cfg, ib=ib, limiter=FakeLimiter()) == EXIT_HALTED

    dq = latest(db, "data_quality")
    assert dq["passed"] == 0
    assert dq["staleness_days"] > refresh_cfg.data.max_stale_days
    run = latest(db, "runs")
    assert run["status"] == "halted" and "SPY" in run["halt_reason"]


def test_spy_right_at_the_limit_passes_and_one_day_over_fails(refresh_cfg, db):
    snapshot(db, ["AAA"])
    limit = refresh_cfg.data.max_stale_days
    # Weekend-proof: pin the last SPY bar to an exact age by trimming a synthetic frame.
    for age, expected in [(limit, EXIT_OK), (limit + 1, EXIT_HALTED)]:
        spy = recent_bars(400, end=date.today() - timedelta(days=age + 3))
        spy = spy.copy()
        spy.index = spy.index + (date.today() - timedelta(days=age) - spy.index[-1].date())
        ib = FakeIB({"SPY": spy, "AAA": recent_bars(400)})
        cache_dir = refresh_cfg.cache_path
        for f in cache_dir.glob("*"):
            f.unlink()
        assert refresh(refresh_cfg, ib=ib, limiter=FakeLimiter()) == expected, age


def test_unfetchable_spy_with_no_cache_halts(refresh_cfg, db):
    snapshot(db, ["AAA"])
    ib = FakeIB({"AAA": recent_bars(400)})          # SPY errors out
    assert refresh(refresh_cfg, ib=ib, limiter=FakeLimiter()) == EXIT_HALTED
    dq = latest(db, "data_quality")
    assert dq["passed"] == 0 and dq["benchmark_bar_date"] is None
    assert latest(db, "runs")["status"] == "halted"


# ----------------------------------------------------------------- rebuild


def rebuild_world(cfg, spy_end=None):
    write_constituents(
        cfg, [("AAA", "Alpha"), ("BRK.B", "Berkshire"), ("ETFX", "Some ETF"), ("NOPE", "Unknown")],
        date.today().isoformat(),
    )
    hist = {"SPY": recent_bars(400, end=spy_end)}
    hist |= {s: recent_bars(400, seed=i) for i, s in enumerate(["AAA", "BRK B", "ETFX", "NOPE"], 1)}
    ib = FakeIB(hist)
    ib.details = {
        "AAA": details(industry="Technology"),
        "BRK B": details(industry="Financial"),
        "ETFX": details(stock_type="ETF"),
        # NOPE: IBKR returns no contract details
    }
    return ib


def test_rebuild_refreshes_everything_then_saves_a_snapshot(refresh_cfg, db):
    ib = rebuild_world(refresh_cfg)

    assert refresh(refresh_cfg, ib=ib, limiter=FakeLimiter(), rebuild_universe=True) == EXIT_OK

    fetched = [s for s, _ in ib.requests]
    assert fetched[0] == "SPY" and set(fetched) == {"SPY", "AAA", "BRK B", "ETFX", "NOPE"}
    snap = {r["symbol"]: r["sector"] for r in db.execute("SELECT * FROM universe_snapshots")}
    assert snap == {"AAA": "Technology", "BRK B": "Financial"}    # ETF and unknown dropped
    assert db.execute("SELECT COUNT(*) FROM contract_info").fetchone()[0] == 3


def test_rebuild_with_stale_spy_halts_before_touching_the_snapshot(refresh_cfg, db):
    old_end = date.today() - timedelta(days=refresh_cfg.data.max_stale_days + 10)
    ib = rebuild_world(refresh_cfg, spy_end=old_end)

    assert refresh(refresh_cfg, ib=ib, limiter=FakeLimiter(), rebuild_universe=True) == EXIT_HALTED

    assert db.execute("SELECT COUNT(*) FROM universe_snapshots").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM contract_info").fetchone()[0] == 0


def test_rebuild_then_weekly_uses_the_saved_list(refresh_cfg, db):
    ib = rebuild_world(refresh_cfg)
    refresh(refresh_cfg, ib=ib, limiter=FakeLimiter(), rebuild_universe=True)
    ib.requests.clear()

    assert refresh(refresh_cfg, ib=ib, limiter=FakeLimiter()) == EXIT_OK
    assert sorted(s for s, _ in ib.requests) == ["AAA", "BRK B", "SPY"]


def test_injected_ib_is_not_disconnected(refresh_cfg, db):
    snapshot(db, ["AAA"])
    ib = FakeIB({"SPY": recent_bars(400), "AAA": recent_bars(400)})
    refresh(refresh_cfg, ib=ib, limiter=FakeLimiter())
    assert not ib.disconnected
