"""Signals: hand-checked values, look-ahead, and the NaN / ranking rules."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import yaml

from src.config import DEFAULT_CONFIG_PATH, Config
from src.strategy.signals import (
    average_true_range,
    blended_momentum,
    compute,
    compute_panel,
    market_regime,
    realized_volatility,
)

BENCH = "SPY"


# ------------------------------------------------------------------ helpers


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config(**yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def small_cfg() -> Config:
    """Real config with tiny windows so every value can be computed by hand."""
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    s = raw["signals"]
    s["momentum"].update(skip_days=2, lookback_short=4, lookback_long=6)
    s["volatility"]["window"] = 5
    s["trend_filter"]["ma_period"] = 4
    s["atr"]["period"] = 3
    return Config(**raw)


def frames(**series: list[float]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(close, high, low) frames from {symbol: closes}, high/low = close +/- 1."""
    n = len(next(iter(series.values())))
    idx = pd.bdate_range("2024-01-01", periods=n, name="date")
    close = pd.DataFrame(series, index=idx, dtype=float)
    return close, close + 1, close - 1


def make_panel(n_days: int = 450, n_syms: int = 12, seed: int = 0):
    """Random-walk panel with the awkward cases in it: a symbol with a gap in
    the middle, one that lists late, a benchmark, and a membership mask that
    changes over time."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2019-01-01", periods=n_days, name="date")
    names = [f"S{i:02d}" for i in range(n_syms)] + [BENCH]
    drift = rng.normal(0.0004, 0.0003, len(names))
    vol = rng.uniform(0.008, 0.025, len(names))
    close = pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(drift, vol, (n_days, len(names))), axis=0)),
        index=idx, columns=names,
    )
    spread = pd.DataFrame(rng.uniform(0.001, 0.02, close.shape), index=idx, columns=names)
    high, low = close * (1 + spread), close * (1 - spread)
    close.loc[idx[150:170], "S03"] = np.nan          # a halt
    close.loc[idx[:200], "S04"] = np.nan             # late listing
    high, low = high.where(close.notna()), low.where(close.notna())
    mask = pd.DataFrame(True, index=idx, columns=[c for c in names if c != BENCH])
    mask.loc[idx[:300], "S05"] = False               # joined the universe late
    mask.loc[idx[350:], "S06"] = False               # left it
    return close, high, low, mask


# --------------------------------------------------------------- hand-checked
#
# Symbol A, ten bars (index 0..9), t = 9. Small config: skip 2, lookbacks 4 and
# 6, vol window 5, ATR period 3, SMA period 4. The expected values below were
# worked out on paper / with plain-Python arithmetic, independent of the
# implementation.

A = [100, 102, 101, 105, 107, 110, 108, 112, 115, 113]
A_HIGH = [101, 103, 103, 106, 108, 111, 111, 113, 116, 116]
A_LOW = [99, 101, 99, 103, 105, 108, 107, 109, 113, 112]
SPY_UP = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109]
SPY_DOWN = [109, 108, 107, 106, 105, 104, 103, 102, 101, 100]


def hand_panel(spy: list[float]):
    idx = pd.bdate_range("2024-01-01", periods=len(A), name="date")
    # B grows 0.5% a day: zero volatility, so the 0.10 floor must bind.
    close = pd.DataFrame(
        {"A": A, "B": [50 * 1.005**i for i in range(len(A))], BENCH: spy},
        index=idx, dtype=float,
    )
    high, low = close.copy(), close.copy()
    high["A"], low["A"] = A_HIGH, A_LOW
    return close, high, low


def test_hand_computed_signals(small_cfg):
    """
    mom_6m  = A[7]/A[5] - 1 = 112/110 - 1
    mom_12m = A[7]/A[3] - 1 = 112/105 - 1
    blended = mean of the two
    vol     = sample stdev of the last 5 returns (A[5..9] over A[4..8]) * sqrt(252)
    score   = blended / vol            (vol 0.4256 is above the 0.10 floor)
    ATR     = mean of true ranges 5, 4, 4
    """
    close, high, low = hand_panel(SPY_UP)
    row = compute(close, high, low, small_cfg).table

    a = row.loc["A"]
    assert a["close"] == 113
    assert a["mom_6m"] == pytest.approx(0.018181818181818)
    assert a["mom_12m"] == pytest.approx(0.066666666666667)
    assert a["blended_momentum"] == pytest.approx(0.042424242424242)
    assert a["vol_63"] == pytest.approx(0.425565763783373)
    assert a["score"] == pytest.approx(0.099689039943161)
    assert a["atr_20"] == pytest.approx(13 / 3)

    # B: 1.005^2 - 1 and 1.005^4 - 1, blended, over the 0.10 floor (its vol ~ 0).
    b = row.loc["B"]
    assert b["mom_6m"] == pytest.approx(0.010025)
    assert b["mom_12m"] == pytest.approx(0.020150500625)
    assert b["vol_63"] < 1e-9
    assert b["score"] == pytest.approx(0.0150877503125 / 0.10)

    assert row.loc["B", "rank"] == 1 and row.loc["A", "rank"] == 2
    assert BENCH not in row.index                         # the benchmark is never ranked


def test_true_range_uses_the_prior_close(small_cfg):
    close, high, low = hand_panel(SPY_UP)
    a = (high[["A"]], low[["A"]], close[["A"]])
    # Bar 7: high-low is only 4 (113-109) but the prior close was 108, and
    # |113-108| = 5. Only the prior-close term can produce the 5.
    assert average_true_range(*a, period=1)["A"].iloc[7] == 5
    atr = average_true_range(*a, period=small_cfg.signals.atr.period)["A"]
    assert atr.iloc[6] == pytest.approx(11 / 3)           # true ranges 3, 4, 4
    assert atr.iloc[9] == pytest.approx(13 / 3)           # true ranges 5, 4, 4
    assert atr.iloc[:3].isna().all()                      # bar 0 has no prior close: no TR


def test_a_gap_counts_as_range():
    idx = pd.bdate_range("2024-01-01", periods=3)
    close = pd.DataFrame({"X": [100.0, 110.0, 110.0]}, index=idx)
    high, low = close + 1, close - 1                      # intraday range is only 2
    atr = average_true_range(high, low, close, period=2)
    # TR[1] = max(2, |111-100|, |109-100|) = 11, TR[2] = 2  ->  mean 6.5
    assert atr["X"].iloc[2] == pytest.approx(6.5)


def test_regime_hand_checked(small_cfg):
    up = market_regime(hand_panel(SPY_UP)[0][BENCH], small_cfg)
    assert bool(up.iloc[-1]) is True                      # 109 > (106+107+108+109)/4 = 107.5
    down = market_regime(hand_panel(SPY_DOWN)[0][BENCH], small_cfg)
    assert bool(down.iloc[-1]) is False                   # 100 < (103+102+101+100)/4 = 101.5
    assert not up.iloc[:3].any()                          # no full SMA window yet: fail closed


def test_regime_frame_reports_what_it_compared(small_cfg):
    f = compute(*hand_panel(SPY_UP), small_cfg)
    assert f.market_on and f.benchmark_close == 109 and f.benchmark_sma == pytest.approx(107.5)


def test_real_config_windows_on_a_geometric_series(cfg):
    """1% a day for 400 bars. With skip 21 and lookbacks 147 and 273:
    mom_6m = 1.01^(147-21) - 1, mom_12m = 1.01^(273-21) - 1."""
    close, high, low = frames(
        A=[100 * 1.01**i for i in range(400)],
        **{BENCH: [100 * 1.001**i for i in range(400)]},
    )
    row = compute(close, high, low, cfg).table.loc["A"]
    assert row["mom_6m"] == pytest.approx(1.01**126 - 1)
    assert row["mom_12m"] == pytest.approx(1.01**252 - 1)
    assert row["blended_momentum"] == pytest.approx(0.5 * (1.01**126 - 1) + 0.5 * (1.01**252 - 1))
    assert row["vol_63"] < 1e-9
    assert row["score"] == pytest.approx(row["blended_momentum"] / cfg.signals.volatility.floor)


# ---------------------------------------------------------------- look-ahead

SIGNALS = ["mom_6m", "mom_12m", "blended_momentum", "vol_63", "score", "rank", "atr_20"]
CUTS = [220, 300, 301, 380, 449]


@pytest.fixture(scope="module")
def panel_inputs():
    return make_panel()


@pytest.fixture(scope="module")
def full_panel(cfg, panel_inputs):
    close, high, low, mask = panel_inputs
    return compute_panel(close, high, low, cfg, mask)


@pytest.mark.parametrize("cut", CUTS)
@pytest.mark.parametrize("signal", SIGNALS)
def test_signal_has_no_lookahead(cfg, panel_inputs, full_panel, signal, cut):
    """Compute on everything, then on data cut off at t: every row <= t must be
    identical. Any use of a later bar (negative shift, centered window, a
    full-sample statistic) changes a row before t and fails this."""
    close, high, low, mask = panel_inputs
    t = close.index[cut]
    truncated = compute_panel(close.loc[:t], high.loc[:t], low.loc[:t], cfg, mask.loc[:t])
    pd.testing.assert_frame_equal(getattr(full_panel, signal).loc[:t], getattr(truncated, signal))


@pytest.mark.parametrize("cut", CUTS)
def test_market_regime_has_no_lookahead(cfg, panel_inputs, full_panel, cut):
    close, high, low, mask = panel_inputs
    t = close.index[cut]
    truncated = compute_panel(close.loc[:t], high.loc[:t], low.loc[:t], cfg, mask.loc[:t])
    pd.testing.assert_series_equal(full_panel.market_on.loc[:t], truncated.market_on)


@pytest.mark.parametrize("signal", SIGNALS)
def test_future_garbage_changes_nothing(cfg, panel_inputs, full_panel, signal):
    """Second angle on the same property: overwrite every bar after t with wild
    values (rather than deleting them) and the signals at or before t hold."""
    close, high, low, mask = panel_inputs
    t = close.index[300]
    rng = np.random.default_rng(1)
    junk = [f.copy() for f in (close, high, low)]
    for f in junk:
        f.loc[f.index > t] = rng.uniform(1, 1e4, f.loc[f.index > t].shape)
    poisoned = compute_panel(*junk, cfg, mask)
    pd.testing.assert_frame_equal(
        getattr(full_panel, signal).loc[:t], getattr(poisoned, signal).loc[:t]
    )


@pytest.mark.parametrize("cut", CUTS)
def test_compute_single_date_matches_the_panel_and_ignores_the_future(
    cfg, panel_inputs, full_panel, cut
):
    close, high, low, mask = panel_inputs
    t = close.index[cut]
    on_full = compute(close, high, low, cfg, as_of=t, universe=mask)
    on_cut = compute(close.loc[:t], high.loc[:t], low.loc[:t], cfg, universe=mask.loc[:t])
    pd.testing.assert_frame_equal(on_full.table, on_cut.table)
    pd.testing.assert_frame_equal(on_full.table, full_panel.at(t).table)
    assert on_full.market_on == on_cut.market_on == bool(full_panel.market_on.loc[t])


# ------------------------------------------------------------ NaN and ranking


def test_short_history_is_nan_and_left_out_of_the_ranking(panel_inputs, full_panel):
    close, *_ = panel_inputs
    f = full_panel.at(close.index[220])                   # S04 listed at 200: only 20 bars
    assert np.isnan(f.table.loc["S04", "score"]) and pd.isna(f.table.loc["S04", "rank"])
    ranks = f.table["rank"].dropna().astype(int).sort_values().tolist()
    assert ranks == list(range(1, len(ranks) + 1))        # contiguous: S04 took no slot
    assert f.rank_of("S04") is None


def test_a_gap_is_never_forward_filled(cfg):
    idx = pd.bdate_range("2023-01-01", periods=400, name="date")
    px = pd.DataFrame(
        {"A": 100 * 1.01 ** np.arange(400), BENCH: 100 * 1.001 ** np.arange(400)}, index=idx
    )
    px.iloc[-1 - cfg.signals.momentum.skip_days, px.columns.get_loc("A")] = np.nan
    f = compute(px, px + 1, px - 1, cfg).table.loc["A"]
    # P[t-21] is the missing bar: both momenta, and so the score and rank, are NaN.
    assert pd.isna(f[["mom_6m", "mom_12m", "score", "rank"]]).all()
    assert pd.isna(f["vol_63"])                           # it is inside the return window too


def test_missing_bar_on_the_signal_date_drops_the_symbol(cfg):
    close, high, low, _ = make_panel(n_days=400, n_syms=8)
    close.iloc[-1, close.columns.get_loc("S01")] = np.nan
    assert "S01" not in compute(close, high, low, cfg).table.index   # nothing to trade at


def test_rank_is_only_among_the_universe_in_force(cfg, panel_inputs):
    close, high, low, mask = panel_inputs
    t = close.index[449]
    everyone = compute(close, high, low, cfg, as_of=t).table
    best = everyone["rank"].idxmin()
    without_best = mask.copy()
    without_best[best] = False
    rest = compute(close, high, low, cfg, as_of=t, universe=without_best).table
    assert best not in rest.index
    ranks = rest["rank"].dropna().astype(int).sort_values().tolist()
    assert ranks == list(range(1, len(ranks) + 1))
    assert rest["rank"].idxmin() == everyone["rank"].drop(best).idxmin()   # runner-up moves up


def test_universe_can_be_given_as_symbols(cfg, panel_inputs):
    close, high, low, _ = panel_inputs
    tbl = compute(close, high, low, cfg, universe=["S00", "S01", "S02"]).table
    assert sorted(tbl.index) == ["S00", "S01", "S02"]


def test_a_symbol_not_yet_in_the_universe_is_not_ranked(panel_inputs, full_panel):
    close, *_ = panel_inputs                              # S05 joins the universe at 300
    assert "S05" not in full_panel.at(close.index[250]).table.index
    assert "S05" in full_panel.at(close.index[320]).table.index


def test_benchmark_is_never_ranked_and_must_be_present(cfg, panel_inputs):
    close, high, low, _ = panel_inputs
    assert BENCH not in compute(close, high, low, cfg).table.index
    with pytest.raises(ValueError, match=BENCH):
        compute(close.drop(columns=BENCH), high.drop(columns=BENCH), low.drop(columns=BENCH), cfg)


def test_regime_is_off_without_a_full_sma_window(cfg):
    close, high, low = frames(A=list(range(100, 150)), **{BENCH: list(range(100, 150))})
    assert compute(close, high, low, cfg).market_on is False


def test_as_of_resolves_to_the_last_trading_day(cfg, panel_inputs):
    close, high, low, _ = panel_inputs
    friday = close.index[close.index.dayofweek == 4][60]
    got = compute(close, high, low, cfg, as_of=friday + pd.Timedelta(days=1))
    assert got.signal_date == friday
    with pytest.raises(ValueError):
        compute(close, high, low, cfg, as_of=close.index[0] - pd.Timedelta(days=1))


def test_misaligned_inputs_are_rejected(cfg, panel_inputs):
    close, high, low, _ = panel_inputs
    with pytest.raises(ValueError, match="aligned"):
        compute_panel(close, high.iloc[1:], low, cfg)
    with pytest.raises(ValueError, match="increasing"):
        compute_panel(close.iloc[::-1], high.iloc[::-1], low.iloc[::-1], cfg)


def test_top_and_rank_of(cfg, panel_inputs):
    close, high, low, _ = panel_inputs
    f = compute(close, high, low, cfg)
    assert [f.rank_of(s) for s in f.top(3)] == [1, 2, 3]
    assert f.rank_of("NOPE") is None


def test_public_pieces_agree_with_the_panel(cfg, panel_inputs, full_panel):
    close, *_ = panel_inputs
    px = close.drop(columns=BENCH).sort_index(axis=1)
    pd.testing.assert_frame_equal(blended_momentum(px, cfg), full_panel.blended_momentum)
    pd.testing.assert_frame_equal(realized_volatility(px, cfg), full_panel.vol_63)


def test_compute_never_hands_later_bars_to_the_panel_code(cfg, panel_inputs, monkeypatch):
    """The cut at as_of is belt and braces: while every signal is causal it
    changes no output, so only a spy can see it. It guarantees a future
    look-ahead bug in the panel code cannot leak through compute()."""
    import src.strategy.signals as signals

    seen = []
    real = signals.compute_panel

    def spy(closes, high, low, *args, **kwargs):
        seen.append((closes.index.max(), high.index.max(), low.index.max()))
        return real(closes, high, low, *args, **kwargs)

    monkeypatch.setattr(signals, "compute_panel", spy)
    close, high, low, _ = panel_inputs
    t = close.index[300]
    compute(close, high, low, cfg, as_of=t)
    assert seen == [(t, t, t)]


def test_top_never_includes_unranked_symbols(cfg, panel_inputs):
    """Regression: `nsmallest` on a rank column containing NaN pads the result
    with the unranked symbols when fewer than n are ranked."""
    close, high, low, _ = panel_inputs
    f = compute(close, high, low, cfg, as_of=close.index[220])   # S04 has 20 bars: unranked
    ranked = int(f.table["rank"].notna().sum())
    assert "S04" in f.table.index and f.rank_of("S04") is None
    top = f.top(len(f.table))
    assert len(top) == ranked and "S04" not in top
