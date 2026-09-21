"""Point-in-time universe (spec 2.3): same filters as the snapshot, but per date
and from data available on that date only."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.data.cache import PriceCache
from src.data.universe import (
    Constituent,
    ContractInfo,
    point_in_time_universe,
    quarterly_universe,
    select_universe,
    todays_universe,
)
from tests.support import recent_bars


def variant(cfg, **sections):
    new = cfg.model_copy(deep=True)
    for section, updates in sections.items():
        for k, v in updates.items():
            setattr(getattr(new, section), k, v)
    return new


@pytest.fixture
def cfg(refresh_cfg):
    return variant(refresh_cfg, universe={
        "adv_window_days": 3, "max_symbols": 2, "min_dollar_volume_20d": 1000.0,
        "min_price": 10.0, "min_days_listed": 5})


def panel(**series):
    n = len(next(iter(series.values())))
    idx = pd.bdate_range("2024-01-01", periods=n, name="date")
    return pd.DataFrame(series, index=idx, dtype=float)


def test_ranks_by_trailing_dollar_volume_each_date(cfg):
    n = 12
    px = panel(SPY=[100] * n, AAA=[50] * n, BBB=[50] * n, CCC=[50] * n)
    # AAA and BBB start on top; CCC's volume surges from row 8.
    vol = panel(SPY=[1] * n, AAA=[100] * n, BBB=[90] * n, CCC=[10] * 8 + [500] * 4)
    m = point_in_time_universe(px, vol, cfg)
    assert list(m.columns) == ["AAA", "BBB", "CCC"]                     # benchmark never a member
    early, late = m.iloc[7], m.iloc[-1]
    assert early.to_dict() == {"AAA": True, "BBB": True, "CCC": False}
    assert late.to_dict() == {"AAA": True, "BBB": False, "CCC": True}   # CCC displaced BBB
    assert not m.iloc[:2].any().any()                                   # ADV window not full yet


def test_row_t_depends_only_on_rows_up_to_t(cfg):
    rng = np.random.default_rng(1)
    n = 60
    px = panel(SPY=[100.0] * n, **{s: 20 + rng.random(n) * 30 for s in "ABCD"})
    vol = panel(SPY=[1.0] * n, **{s: rng.random(n) * 1e4 for s in "ABCD"})
    full = point_in_time_universe(px, vol, cfg)
    cut = 35
    trunc = point_in_time_universe(px.iloc[:cut], vol.iloc[:cut], cfg)
    pd.testing.assert_frame_equal(full.iloc[:cut], trunc)
    # ...and changing the future leaves the past alone.
    px2, vol2 = px.copy(), vol.copy()
    px2.iloc[cut:] = 1.0
    vol2.iloc[cut:] = 1e9
    pd.testing.assert_frame_equal(point_in_time_universe(px2, vol2, cfg).iloc[:cut], full.iloc[:cut])


def test_filters_price_dollar_volume_and_days_listed(cfg):
    n = 12
    px = panel(SPY=[100] * n, CHEAP=[5] * n, THIN=[50] * n, NEW=[50] * n, OK=[50] * n)
    vol = panel(SPY=[1] * n, CHEAP=[1e6] * n, THIN=[1] * n, NEW=[1e4] * n, OK=[1e4] * n)
    px.loc[px.index[:9], "NEW"] = np.nan                                 # listed at row 9
    vol.loc[vol.index[:9], "NEW"] = np.nan
    m = point_in_time_universe(px, vol, cfg)
    assert not m["CHEAP"].any()                    # price < $10
    assert not m["THIN"].any()                     # $50/day average
    assert m["OK"].iloc[-1]
    assert not m["NEW"].iloc[-1]                   # 5 calendar days needed; only 3 since first bar
    late = point_in_time_universe(
        panel(SPY=[100] * 20, NEW=[np.nan] * 9 + [50] * 11),
        panel(SPY=[1] * 20, NEW=[np.nan] * 9 + [1e4] * 11), cfg)
    assert late["NEW"].iloc[-1]


def test_a_missing_bar_in_the_window_is_not_liquid(cfg):
    n = 12
    px = panel(SPY=[100] * n, AAA=[50] * n)
    vol = panel(SPY=[1] * n, AAA=[1e4] * n)
    px.iloc[8, 1] = np.nan
    m = point_in_time_universe(px, vol, cfg)
    assert not m["AAA"].iloc[8:11].any()           # windows touching the halt
    assert m["AAA"].iloc[11]


def test_allowed_filters_security_type(cfg):
    n = 12
    px = panel(SPY=[100] * n, AAA=[50] * n, ETF=[50] * n)
    vol = panel(SPY=[1] * n, AAA=[1e4] * n, ETF=[1e5] * n)
    m = point_in_time_universe(px, vol, cfg, allowed={"AAA"})
    assert m["AAA"].iloc[-1] and not m["ETF"].any()


def test_ties_break_on_symbol(cfg):
    n = 12
    px = panel(SPY=[100] * n, CCC=[50] * n, AAA=[50] * n, BBB=[50] * n)
    vol = panel(SPY=[1] * n, CCC=[1e4] * n, AAA=[1e4] * n, BBB=[1e4] * n)
    m = point_in_time_universe(px, vol, cfg)
    assert m.iloc[-1].to_dict() == {"AAA": True, "BBB": True, "CCC": False}


def test_todays_universe_is_the_hindsight_break(cfg):
    n = 12
    px = panel(SPY=[100] * n, AAA=[50] * n, BBB=[50] * n, CCC=[50] * n)
    vol = panel(SPY=[1] * n, AAA=[100] * n, BBB=[90] * n, CCC=[10] * 8 + [500] * 4)
    pit = point_in_time_universe(px, vol, cfg)
    today = todays_universe(px, vol, cfg)
    assert today["CCC"].all()                       # CCC "was" in from day one
    assert not pit["CCC"].iloc[7]
    assert not today["BBB"].any()


def test_agrees_with_the_snapshot_filters_on_the_last_date(refresh_cfg):
    """One definition of the universe: at the last date the point-in-time mask
    picks the same symbols select_universe does from the cache."""
    cfg = variant(refresh_cfg, universe={"max_symbols": 3, "min_days_listed": 100,
                                         "min_dollar_volume_20d": 5e7})
    end = date.today()
    syms = ["SPY", "AAA", "BBB", "CCC", "DDD", "EEE"]
    cache = PriceCache(cfg.cache_path)
    bars = {}
    for i, s in enumerate(syms):
        volume = 10 if s == "EEE" else 1e6 * (i + 1)
        b = recent_bars(300, end=end, seed=i, price=20 + 10 * i, volume=volume)
        cache.write(s, b)
        bars[s] = b
    infos = {s: ContractInfo(s, 1, "COMMON", "Tech", None, None, None) for s in syms}
    const = [Constituent(s, s, end) for s in syms if s != "SPY"]
    sel = select_universe(cfg, cache, const, infos)

    closes = pd.DataFrame({s: b["close"] for s, b in bars.items()})
    volumes = pd.DataFrame({s: b["volume"] for s, b in bars.items()})
    m = point_in_time_universe(closes, volumes, cfg)
    assert sorted(m.columns[m.iloc[-1]]) == sorted(r.symbol for r in sel.rows)
    assert len(sel.rows) == 3


# ------------------------------------------------------------ quarterly hold


def daily_frame(rows, start="2024-01-02"):
    idx = pd.bdate_range(start, periods=len(rows), name="date")
    return pd.DataFrame(rows, index=idx, columns=["A", "B"], dtype=bool)


def test_membership_is_stable_within_a_quarter_and_changes_at_the_rebuild():
    n = 130                                        # 2024-01-02 .. 2024-06-28
    idx = pd.bdate_range("2024-01-02", periods=n, name="date")
    # daily answer flaps every other day, so any daily use would show it
    daily = pd.DataFrame({"A": [i % 3 == 0 for i in range(n)],
                          "B": [i % 3 != 0 for i in range(n)]}, index=idx)
    held = quarterly_universe(daily)
    for lo, hi in (("2024-01-02", "2024-03-29"), ("2024-04-01", "2024-06-28")):
        block = held.loc[lo:hi]
        assert len(block.drop_duplicates()) == 1, "membership moved inside a quarter"
    # rebuilt on the first trading day of the quarter, from that day's own row
    assert held.loc["2024-04-01"].to_dict() == daily.loc["2024-04-01"].to_dict()
    assert held.loc["2024-01-02"].to_dict() == daily.loc["2024-01-02"].to_dict()
    assert held.loc["2024-03-29"].to_dict() == daily.loc["2024-01-02"].to_dict()
    assert held.loc["2024-04-01"].to_dict() != held.loc["2024-03-29"].to_dict()


def test_first_build_happens_when_the_universe_first_exists_not_next_quarter():
    daily = daily_frame([[False, False]] * 20 + [[True, False]] * 60)   # non-empty from row 20
    held = quarterly_universe(daily)
    assert not held.iloc[:20].any().any()                               # nobody before the first build
    assert held["A"].iloc[20:].all() and not held["B"].any()


def test_quarterly_membership_uses_no_future_rows():
    rng = np.random.default_rng(4)
    daily = daily_frame(rng.random((200, 2)) > 0.5)
    full = quarterly_universe(daily)
    for cut in (30, 70, 131):
        pd.testing.assert_frame_equal(quarterly_universe(daily.iloc[:cut]), full.iloc[:cut])


def test_a_held_name_keeps_its_membership_when_it_slips_out_mid_quarter():
    rows = [[True, True]] * 40 + [[False, True]] * 60        # A leaves the daily top N on row 40
    held = quarterly_universe(daily_frame(rows))              # rows 0-59 are Q1 (Jan 2 .. Mar 22)
    assert held["A"].iloc[:64].all()                          # still a member until the next rebuild
    assert not held["A"].iloc[-1]                             # gone after the April rebuild
