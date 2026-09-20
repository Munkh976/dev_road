"""`refresh --backfill`: full refetch at the configured history_years.

Incremental fetches only extend forwards, so raising history_years adds no
older bars to an existing cache. Backfill is the one path that does.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.data.cache import PriceCache
from src.data.refresh import EXIT_OK, main, refresh, smoke, update_symbol
from tests.support import FakeIB, FakeLimiter, recent_bars
from tests.unit.test_refresh import latest, snapshot


def years(cfg) -> str:
    return f"{cfg.data.history_years} Y"


def test_incremental_refresh_never_adds_older_history(refresh_cfg):
    """The problem backfill exists to solve."""
    long = recent_bars(600, seed=1)
    cache = PriceCache(refresh_cfg.cache_path)
    cache.write("AAA", long.iloc[300:])                 # short cache, up to date
    ib = FakeIB({"AAA": long})

    res = update_symbol(ib, "AAA", cache, FakeLimiter(), refresh_cfg)

    assert res.action == "incremental"
    assert cache.read("AAA").index[0] == long.index[300]


def test_backfill_replaces_cache_with_deeper_history(refresh_cfg):
    long = recent_bars(600, seed=1)
    cache = PriceCache(refresh_cfg.cache_path)
    cache.write("AAA", long.iloc[300:])
    ib = FakeIB({"AAA": long})

    res = update_symbol(ib, "AAA", cache, FakeLimiter(), refresh_cfg, backfill=True)

    assert res.action == "backfill" and res.bars_fetched == 600
    assert [kw["durationStr"] for _, kw in ib.requests] == [years(refresh_cfg)]
    got = cache.read("AAA")
    assert len(got) == 600 and got.index[0] == long.index[0]


def test_backfill_does_not_stitch_onto_restated_history(refresh_cfg):
    long = recent_bars(600, seed=1)
    cache = PriceCache(refresh_cfg.cache_path)
    cache.write("AAA", long.iloc[300:])
    ib = FakeIB({"AAA": long})
    ib.scale["AAA"] = 0.9                                # IBKR now serves restated prices

    update_symbol(ib, "AAA", cache, FakeLimiter(), refresh_cfg, backfill=True)

    got = cache.read("AAA")["close"]
    assert got.iloc[-1] == pytest.approx(long["close"].iloc[-1] * 0.9)
    assert got.iloc[0] == pytest.approx(long["close"].iloc[0] * 0.9)   # one basis throughout


def test_failed_backfill_keeps_the_old_cache(refresh_cfg):
    long = recent_bars(600, seed=1)
    cache = PriceCache(refresh_cfg.cache_path)
    cache.write("AAA", long.iloc[300:])
    ib = FakeIB({"AAA": long})
    ib.errors["AAA"] = [(162, "Historical Market Data Service error message: no permissions")]

    with pytest.raises(Exception, match="162"):
        update_symbol(ib, "AAA", cache, FakeLimiter(), refresh_cfg, backfill=True)

    assert len(cache.read("AAA")) == 300


def test_backfill_refresh_covers_snapshot_and_spy_with_full_requests(refresh_cfg, db):
    snapshot(db, ["AAA", "BBB"])
    hist = {s: recent_bars(600, seed=i) for i, s in enumerate(["SPY", "AAA", "BBB", "ZZZ"])}
    cache = PriceCache(refresh_cfg.cache_path)
    for s in ("SPY", "AAA", "BBB"):
        cache.write(s, hist[s].iloc[300:])
    ib = FakeIB(hist)

    assert refresh(refresh_cfg, ib=ib, limiter=FakeLimiter(), backfill=True) == EXIT_OK

    assert [s for s, _ in ib.requests] == ["SPY", "AAA", "BBB"]      # benchmark first, no ZZZ
    assert {kw["durationStr"] for _, kw in ib.requests} == {years(refresh_cfg)}
    assert all(len(cache.read(s)) == 600 for s in ("SPY", "AAA", "BBB"))
    run = latest(db, "runs")
    assert run["status"] == "ok" and run["notes"] == "backfill"
    assert latest(db, "data_quality")["passed"] == 1


def test_backfill_smoke_limits_to_named_symbols(refresh_cfg, capsys):
    hist = {"SPY": recent_bars(600), "AAA": recent_bars(600, seed=2)}
    cache = PriceCache(refresh_cfg.cache_path)
    cache.write("SPY", hist["SPY"].iloc[300:])
    ib = FakeIB(hist)

    assert smoke(["SPY"], refresh_cfg, ib=ib, limiter=FakeLimiter(), backfill=True) == EXIT_OK

    assert [s for s, _ in ib.requests] == ["SPY"]
    out = capsys.readouterr().out
    assert "backfill" in out and str(hist["SPY"].index[0].date()) in out


def test_backfill_and_rebuild_cannot_be_combined():
    with pytest.raises(SystemExit):
        main(["--backfill", "--rebuild-universe"])
