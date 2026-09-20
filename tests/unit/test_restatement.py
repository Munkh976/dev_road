"""Adjusted-price restatement: overlap check and full refetch (spec 3.1)."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

from src.data.cache import PriceCache
from src.data.refresh import FetchError, update_symbol
from tests.support import FakeIB, FakeLimiter, recent_bars

WEEK = 5            # business days the cache is behind


@pytest.fixture
def world(refresh_cfg):
    """A 400-bar true history; the cache holds all but the last WEEK bars."""
    history = recent_bars(400)
    cache = PriceCache(refresh_cfg.cache_path)
    cache.write("AAPL", history.iloc[:-WEEK])
    ib = FakeIB({"AAPL": history})
    return refresh_cfg, cache, ib, history, FakeLimiter()


def daily_moves(df: pd.DataFrame) -> pd.Series:
    return df["close"].pct_change().dropna()


def test_rescaled_history_is_refetched_as_one_continuous_series(world):
    cfg, cache, ib, history, limiter = world
    ib.scale["AAPL"] = 0.98                    # a dividend restated every close by 2%

    res = update_symbol(ib, "AAPL", cache, limiter, cfg)

    assert res.action == "refetch"
    out = cache.read("AAPL")
    assert len(out) == len(history)
    # Whole series is the restated one: nothing left at the old level.
    np.testing.assert_allclose(out["close"].values, history["close"].values * 0.98)
    # No jump where old and new data meet: every day-to-day move, including the
    # first new bar's, matches the true path. A stitched series is off by 2% there.
    np.testing.assert_allclose(daily_moves(out).values, daily_moves(history).values)


def test_stitching_without_the_check_would_have_left_a_jump(world):
    """Guards the test above: the scenario really does produce a jump."""
    _, _, _, history, _ = world
    stitched = history.copy()
    stitched.iloc[-WEEK:, stitched.columns.get_loc("close")] *= 0.98   # new, restated bars
    join = history.index[-WEEK]
    extra = daily_moves(stitched).loc[join] - daily_moves(history).loc[join]
    assert abs(extra) > 0.015


def test_refetch_costs_one_extra_request_and_is_full_history(world):
    cfg, cache, ib, _, limiter = world
    ib.scale["AAPL"] = 0.98
    update_symbol(ib, "AAPL", cache, limiter, cfg)
    durations = [kw["durationStr"] for _, kw in ib.requests]
    assert durations[1] == f"{cfg.data.history_years} Y"
    assert durations[0].endswith(" D")
    assert limiter.events.count("acquire") == 2


def test_every_request_has_empty_end_datetime(world):
    cfg, cache, ib, _, limiter = world
    ib.scale["AAPL"] = 0.98
    update_symbol(ib, "AAPL", cache, limiter, cfg)
    assert all(kw["endDateTime"] == "" for _, kw in ib.requests)


def test_refetch_is_logged_with_its_reason(world, caplog):
    cfg, cache, ib, _, limiter = world
    ib.scale["AAPL"] = 0.98
    with caplog.at_level(logging.WARNING):
        res = update_symbol(ib, "AAPL", cache, limiter, cfg)
    assert "REFETCH AAPL" in caplog.text
    assert "restated" in res.reason and "2.00%" in res.reason


def test_unchanged_overlap_merges_incrementally(world):
    cfg, cache, ib, history, limiter = world
    res = update_symbol(ib, "AAPL", cache, limiter, cfg)
    assert res.action == "incremental"
    assert len(ib.requests) == 1
    out = cache.read("AAPL")
    assert len(out) == len(history)
    np.testing.assert_allclose(out["close"].values, history["close"].values)


def test_drift_inside_tolerance_is_not_a_restatement(world):
    cfg, cache, ib, _, limiter = world
    ib.scale["AAPL"] = 1 + cfg.data.restatement_tolerance / 2
    assert update_symbol(ib, "AAPL", cache, limiter, cfg).action == "incremental"


def test_drift_just_outside_tolerance_is_a_restatement(world):
    cfg, cache, ib, _, limiter = world
    ib.scale["AAPL"] = 1 + cfg.data.restatement_tolerance * 1.5
    assert update_symbol(ib, "AAPL", cache, limiter, cfg).action == "refetch"


def test_a_single_moved_close_triggers_refetch(world):
    cfg, cache, ib, history, limiter = world
    cached = cache.read("AAPL")
    cached.iloc[-3, cached.columns.get_loc("close")] *= 1.02      # one stale bar in cache
    cache.write("AAPL", cached)
    assert update_symbol(ib, "AAPL", cache, limiter, cfg).action == "refetch"
    np.testing.assert_allclose(cache.read("AAPL")["close"].values, history["close"].values)


def test_no_overlap_fails_closed_into_a_refetch(world):
    cfg, cache, ib, history, limiter = world
    last_cached = cache.read("AAPL").index[-1]
    only_new = history[history.index > last_cached]
    ib.history["AAPL"] = only_new                    # fetch shares no date with the cache
    real = ib.reqHistoricalData

    def serve(contract, **kw):
        if kw["durationStr"].endswith("Y"):
            ib.history["AAPL"] = history             # the full refetch sees everything
        return real(contract, **kw)

    ib.reqHistoricalData = serve
    res = update_symbol(ib, "AAPL", cache, limiter, cfg)
    assert res.action == "refetch" and "no overlap" in res.reason
    assert len(cache.read("AAPL")) == len(history)


def test_cache_older_than_a_year_is_refetched_in_full(refresh_cfg):
    history = recent_bars(600)
    cache = PriceCache(refresh_cfg.cache_path)
    cache.write("AAPL", history.iloc[:100])           # ends ~1.6 years ago
    ib = FakeIB({"AAPL": history})
    res = update_symbol(ib, "AAPL", cache, FakeLimiter(), refresh_cfg)
    assert res.action == "refetch"
    assert [kw["durationStr"] for _, kw in ib.requests] == [f"{refresh_cfg.data.history_years} Y"]
    assert len(cache.read("AAPL")) == 600


def test_nothing_cached_fetches_full_history(refresh_cfg):
    history = recent_bars(300)
    cache = PriceCache(refresh_cfg.cache_path)
    ib = FakeIB({"AAPL": history})
    res = update_symbol(ib, "AAPL", cache, FakeLimiter(), refresh_cfg)
    assert res.action == "full"
    assert len(cache.read("AAPL")) == 300


def test_failed_refetch_leaves_the_old_cache_untouched(world):
    cfg, cache, ib, _, limiter = world
    before = cache.read("AAPL")
    ib.scale["AAPL"] = 0.98
    real = ib.reqHistoricalData

    def flaky(contract, **kw):
        if kw["durationStr"].endswith("Y"):
            ib.errors["AAPL"] = [(200, "boom")]
        return real(contract, **kw)

    ib.reqHistoricalData = flaky
    with pytest.raises(FetchError):
        update_symbol(ib, "AAPL", cache, limiter, cfg)
    pd.testing.assert_frame_equal(cache.read("AAPL"), before)


def test_refetch_replaces_rather_than_merges(world):
    """Cached bars absent from the fresh history must not survive a refetch."""
    cfg, cache, ib, history, limiter = world
    cached = cache.read("AAPL")
    ghost = cached.iloc[[0]].copy()
    ghost.index = pd.DatetimeIndex([cached.index[0] - pd.Timedelta(days=30)], name="date")
    cache.write("AAPL", pd.concat([ghost, cached]))
    ib.scale["AAPL"] = 0.98
    assert update_symbol(ib, "AAPL", cache, limiter, cfg).action == "refetch"
    out = cache.read("AAPL")
    assert ghost.index[0] not in out.index
    assert len(out) == len(history)
