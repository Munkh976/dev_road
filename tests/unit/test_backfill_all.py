"""`refresh --backfill --all-constituents`: every constituent, resumable.

The backtest chooses its universe point-in-time from all of them (spec 1.0.3),
so it needs full history for names that are not in today's top 150.
"""

from __future__ import annotations

import pytest

from src.data.cache import PriceCache
from src.data.refresh import EXIT_OK, main, refresh, update_symbol
from tests.conftest import write_constituents
from tests.support import FakeIB, FakeLimiter, recent_bars
from tests.unit.test_backfill import years
from tests.unit.test_refresh import latest

SYMS = ["AAA", "BBB", "CCC"]


def setup(cfg, deep=(), short_history=()):
    """Constituents CSV of SYMS. `deep` symbols are already cached to full depth."""
    write_constituents(cfg, [(s, s) for s in SYMS], "2026-09-01")
    hist = {"SPY": recent_bars(600, seed=9)}
    hist |= {s: recent_bars(600, seed=i) for i, s in enumerate(SYMS)}
    for s in short_history:
        hist[s] = recent_bars(120, seed=5)
    cache = PriceCache(cfg.cache_path)
    for s in deep:
        cache.write(s, hist[s], history_years=cfg.data.history_years)
    return cache, hist


def test_covers_every_constituent_not_just_the_snapshot(refresh_cfg, db):
    cache, hist = setup(refresh_cfg)                 # no universe snapshot exists at all
    ib = FakeIB(hist)

    code = refresh(refresh_cfg, ib=ib, limiter=FakeLimiter(), backfill=True,
                   all_constituents=True)

    assert code == EXIT_OK
    assert [s for s, _ in ib.requests] == ["SPY", *SYMS]
    assert {kw["durationStr"] for _, kw in ib.requests} == {years(refresh_cfg)}
    assert latest(db, "runs")["notes"] == "backfill all constituents"


def test_resumes_by_skipping_symbols_already_backfilled(refresh_cfg, db):
    cache, hist = setup(refresh_cfg, deep=["AAA", "BBB"])
    ib = FakeIB(hist)

    refresh(refresh_cfg, ib=ib, limiter=FakeLimiter(), backfill=True, all_constituents=True)

    assert [s for s, _ in ib.requests] == ["SPY", "CCC"]      # only what is missing
    import json
    detail = json.loads(latest(db, "data_quality")["detail"])
    assert detail["skipped"] == 2 and detail["updated"] == 2


def test_interrupted_run_can_be_repeated_and_the_second_run_fetches_nothing_new(refresh_cfg):
    cache, hist = setup(refresh_cfg)
    ib1 = FakeIB(hist)
    ib1.max_requests = 3                              # SPY, AAA, BBB, then the "laptop sleeps"
    refresh(refresh_cfg, ib=ib1, limiter=FakeLimiter(), backfill=True, all_constituents=True)
    assert [s for s, _ in ib1.requests] == ["SPY", "AAA", "BBB"]

    ib2 = FakeIB(hist)
    refresh(refresh_cfg, ib=ib2, limiter=FakeLimiter(), backfill=True, all_constituents=True)
    assert [s for s, _ in ib2.requests] == ["SPY", "CCC"]

    ib3 = FakeIB(hist)
    refresh(refresh_cfg, ib=ib3, limiter=FakeLimiter(), backfill=True, all_constituents=True)
    assert [s for s, _ in ib3.requests] == ["SPY"]           # benchmark only


def test_benchmark_is_never_skipped_so_the_data_gate_sees_fresh_bars(refresh_cfg):
    cache, hist = setup(refresh_cfg, deep=["SPY", "AAA", "BBB", "CCC"])
    ib = FakeIB(hist)

    refresh(refresh_cfg, ib=ib, limiter=FakeLimiter(), backfill=True, all_constituents=True)

    assert [s for s, _ in ib.requests] == ["SPY"]


def test_recent_listing_counts_as_backfilled_once_fetched_in_full(refresh_cfg):
    """A short history never reaches the target start; the manifest marker is
    what stops it being refetched forever."""
    cache, hist = setup(refresh_cfg, short_history=["CCC"])
    ib = FakeIB(hist)
    refresh(refresh_cfg, ib=ib, limiter=FakeLimiter(), backfill=True, all_constituents=True)
    assert len(cache.read("CCC")) == 120

    ib2 = FakeIB(hist)
    refresh(refresh_cfg, ib=ib2, limiter=FakeLimiter(), backfill=True, all_constituents=True)
    assert "CCC" not in [s for s, _ in ib2.requests]


def test_incremental_merge_keeps_the_backfill_marker(refresh_cfg):
    cache, hist = setup(refresh_cfg, deep=["AAA"])
    update_symbol(FakeIB(hist), "AAA", cache, FakeLimiter(), refresh_cfg)   # incremental
    assert cache.is_backfilled("AAA", refresh_cfg.data.history_years)


def test_shallow_cache_without_marker_is_not_backfilled(refresh_cfg):
    cache, hist = setup(refresh_cfg)
    cache.write("AAA", hist["AAA"])                   # legacy write, ~2.4 years deep
    assert not cache.is_backfilled("AAA", refresh_cfg.data.history_years)


def test_legacy_deep_cache_without_marker_is_recognised_by_its_first_bar(refresh_cfg):
    """SPY was backfilled to 2004 before the marker existed; do not pay for it twice."""
    from datetime import date, timedelta
    import pandas as pd
    cache, hist = setup(refresh_cfg)
    deep = hist["AAA"].copy()
    start = date.today() - timedelta(days=round(refresh_cfg.data.history_years * 365.25))
    deep.index = deep.index - (deep.index[0] - pd.Timestamp(start))
    cache.write("AAA", deep)                          # no history_years marker
    assert cache.is_backfilled("AAA", refresh_cfg.data.history_years)


def test_all_constituents_requires_backfill_and_excludes_smoke_and_rebuild():
    for argv in (["--all-constituents"],
                 ["--all-constituents", "--backfill", "--symbols", "SPY"],
                 ["--all-constituents", "--backfill", "--rebuild-universe"]):
        with pytest.raises(SystemExit):
            main(argv)


def test_deliberate_break_resume_ignored_refetches_everything(refresh_cfg, monkeypatch):
    """If the skip check stopped working the resume test above must notice."""
    cache, hist = setup(refresh_cfg, deep=["AAA", "BBB"])
    monkeypatch.setattr(PriceCache, "is_backfilled", lambda *a, **k: False)
    ib = FakeIB(hist)
    refresh(refresh_cfg, ib=ib, limiter=FakeLimiter(), backfill=True, all_constituents=True)
    assert [s for s, _ in ib.requests] != ["SPY", "CCC"]
    assert [s for s, _ in ib.requests] == ["SPY", *SYMS]
