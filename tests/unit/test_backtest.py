"""Backtest engine: costs, schedule, windows, hand-checked trades, look-ahead,
acceptance, and deliberate breaks. Synthetic markets only; no real data."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import yaml

from src.backtest import walkforward as wf
from src.backtest.walkforward import (
    BacktestError,
    BacktestOptions,
    BacktestResult,
    MarketData,
    WindowResult,
    apply_costs,
    buy_and_hold,
    check_acceptance,
    format_report,
    max_affordable_shares,
    oos_windows,
    simulate,
    walk_forward,
    weekly_schedule,
)
from src.config import DEFAULT_CONFIG_PATH, Config
from tests.mutants import load_mutant


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config(**yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")))


def variant(cfg: Config, **sections) -> Config:
    new = cfg.model_copy(deep=True)
    for section, updates in sections.items():
        target = getattr(new, section)
        for key, value in updates.items():
            if isinstance(value, dict):
                sub = getattr(target, key)
                for k, v in value.items():
                    setattr(sub, k, v)
            else:
                setattr(target, key, value)
    return new


SMALL_SIGNALS = {
    "momentum": {"skip_days": 1, "lookback_short": 2, "lookback_long": 3},
    "volatility": {"window": 2},
    "trend_filter": {"ma_period": 2},
    "atr": {"period": 2},
}
SMALL_UNIVERSE = {"adv_window_days": 2, "min_dollar_volume_20d": 1000.0,
                  "min_price": 10.0, "min_days_listed": 0}


@pytest.fixture(scope="module")
def tiny(cfg) -> Config:
    """Windows of 1-3 bars so a market of a few weeks can be worked on paper."""
    return variant(cfg, signals=SMALL_SIGNALS, sizing={"covariance_window": 2},
                   universe=SMALL_UNIVERSE)


# ------------------------------------------------------------- tiny markets


def path(c0: float, rets: list[float]) -> list[float]:
    out = [c0]
    for r in rets:
        out.append(out[-1] * (1 + r))
    return out


def alternating(n: int, odd: float, even: float) -> list[float]:
    return [odd if t % 2 else even for t in range(1, n)]


def hand_market(
    n: int = 23, a_rets: list[float] | None = None, spy_rets: list[float] | None = None,
    a_vol: float = 1e4, b_vol: float | list[float] = 1e4,
) -> MarketData:
    """Row 0 is Monday 2024-01-01. A rises 3%/1% alternately (rank 1, positive
    momentum); B falls and is never bought (E3); SPY rises 1% a day. A opens
    at 0.99 x its close so a next-open fill differs from a same-bar one."""
    idx = pd.bdate_range("2024-01-01", periods=n, name="date")
    a = path(100, a_rets if a_rets is not None else alternating(n, 0.03, 0.01))
    b = path(100, alternating(n, -0.01, -0.02))
    s = path(100, spy_rets if spy_rets is not None else [0.01] * (n - 1))
    close = pd.DataFrame({"A": a, "B": b, "SPY": s}, index=idx)
    opens = close * pd.Series({"A": 0.99, "B": 1.0, "SPY": 1.0})
    vol = pd.DataFrame({"A": a_vol, "B": b_vol, "SPY": 1e6}, index=idx)
    return MarketData(opens, close * 1.01, close * 0.99, close, vol,
                      sectors={"A": "Tech", "B": "Health", "SPY": "ETF"})


def fills(sim, side=None):
    return [t for t in sim.trades if side is None or t.side == side]


# ==================================================================== costs


def test_apply_costs_hand_values(cfg):
    # $3,000 of a ~$110 stock: 27 shares -> commission floors at $1.00;
    # slippage 5 bps + half of the 3 bps spread = 6.5 bps of $3,000 = $1.95.
    assert apply_costs(3000.0, 27.185, cfg, True) == pytest.approx(1.0 + 1.95)
    # 1,000 shares at $50: commission 1000 x 0.005 = $5.00; 6.5 bps of $50,000 = $32.50.
    assert apply_costs(50000.0, 1000, cfg, True) == pytest.approx(5.0 + 32.5)
    # 200 shares is exactly where the $1 minimum stops binding.
    assert apply_costs(0.0, 200, cfg, False) == pytest.approx(1.0)
    assert apply_costs(0.0, 201, cfg, False) == pytest.approx(1.005)
    # a sell pays what a buy pays
    assert apply_costs(3000.0, 27.185, cfg, False) == apply_costs(3000.0, 27.185, cfg, True)


@pytest.mark.parametrize("cash,price", [(15000, 110.35), (150, 110.35), (2.5, 110.35),
                                        (1e6, 10.0), (1e7, 3.0), (10000, 900.0)])
def test_affordable_shares_spend_the_cash_and_never_more(cfg, cash, price):
    q = max_affordable_shares(cash, price, cfg)
    spent = q * price + apply_costs(q * price, q, cfg, True)
    assert spent <= cash + 1e-9
    if q > 0:
        assert spent == pytest.approx(cash, rel=1e-9)                # uses all of it


def test_affordable_shares_edge_cases(cfg):
    assert max_affordable_shares(0.5, 100.0, cfg) == 0.0             # cannot even pay the $1
    assert max_affordable_shares(1000.0, 0.0, cfg) == 0.0
    assert max_affordable_shares(1000.0, 100.0, cfg, costs_on=False) == 10.0


# ================================================================= schedule


def test_schedule_decisions_are_the_last_bar_of_each_week():
    idx = pd.bdate_range("2024-01-01", "2024-02-16")
    idx = idx.drop(pd.Timestamp("2024-01-26"))                       # a Friday holiday
    decisions, _ = weekly_schedule(idx)
    dates = [idx[d].strftime("%a %m-%d") for d in decisions]
    assert dates[:4] == ["Fri 01-05", "Fri 01-12", "Fri 01-19", "Thu 01-25"]


def test_schedule_entry_week_is_the_first_weekly_fill_of_each_month():
    idx = pd.bdate_range("2024-01-01", "2024-06-28")
    decisions, entries = weekly_schedule(idx)
    fills_ = sorted(idx[d + 1] for d in entries)
    # decision on the Friday before the first Monday (or first trading day) of the month
    assert [d.strftime("%Y-%m-%d") for d in fills_] == [
        "2024-01-08", "2024-02-05", "2024-03-04", "2024-04-01", "2024-05-06", "2024-06-03"]
    assert {d for d in entries} <= set(decisions)


def test_schedule_never_uses_prices():
    """Same calendar, same schedule: it is a function of dates alone."""
    idx = pd.bdate_range("2024-01-01", periods=80)
    assert weekly_schedule(idx) == weekly_schedule(pd.DatetimeIndex(idx.values))


# ================================================================== windows


def test_windows_start_three_years_after_the_first_signal_and_tile_the_rest(cfg):
    idx = pd.bdate_range("2005-01-03", "2026-09-18")
    first = pd.Timestamp("2005-10-31")
    w = oos_windows(idx, first, cfg)
    assert idx[w[0].start_row] == pd.Timestamp("2008-10-31")
    for prev, nxt in zip(w, w[1:]):
        assert nxt.start_row == prev.end_row + 1                      # no gap, no overlap
    assert not any(x.partial for x in w[:-1])
    assert w[-1].partial and w[-1].end_row == len(idx) - 1            # 2025-10-31 .. today
    assert len(w) == 18
    assert idx[w[0].start_row] < pd.Timestamp("2009-03-01")           # covers the 2008-09 crash


def test_windows_complete_when_data_covers_the_last_year(cfg):
    idx = pd.bdate_range("2005-01-03", "2011-11-30")
    w = oos_windows(idx, pd.Timestamp("2005-10-31"), cfg)
    # 2008-10-31, 2009-10-31 and 2010-10-31 are whole years; the fourth has a month of data.
    assert [x.partial for x in w] == [False, False, False, True]
    assert idx[w[3].start_row] == pd.Timestamp("2011-10-31")


def test_windows_refuse_a_step_that_would_overlap_or_leave_gaps(cfg):
    bad = variant(cfg, backtest={"walk_forward": {"step_months": 6}})
    with pytest.raises(BacktestError, match="step_months"):
        oos_windows(pd.bdate_range("2005-01-03", "2015-01-02"), pd.Timestamp("2005-10-31"), bad)


def test_windows_never_begin_before_the_protocol_start(cfg):
    late = variant(cfg, backtest={"start": "2010-01-01"})
    idx = pd.bdate_range("2005-01-03", "2015-01-02")
    w = oos_windows(idx, pd.Timestamp("2005-10-31"), late)
    assert idx[w[0].start_row] >= pd.Timestamp("2010-01-01")


def test_too_little_history_is_an_error_not_an_empty_result(cfg):
    idx = pd.bdate_range("2005-01-03", "2007-06-01")
    assert oos_windows(idx, pd.Timestamp("2005-10-31"), cfg) == []


# ========================================================== hand-checked run
#
# The market (hand_market): 23 bars, Mon 2024-01-01 .. Wed 2024-01-31. Tiny
# windows (skip 1, lookbacks 2 and 3, vol window 2, SMA 2, ATR 2).
#
#   * Signals first exist at row 3 (Thu Jan 4). The first Friday is Jan 5 (row
#     4), and the fill on Mon Jan 8 (row 5) is the first of January: an entry
#     week. Every later January fill is not.
#   * At the Jan 5 close: A ranks 1, momentum > 0, vol 22.4% (< 80%), SPY above
#     its 2-day SMA. B ranks 2 but momentum < 0 (E3). One name, so sizing pins A
#     at the 20% cap: target 0.20 x $15,000 = $3,000. The vol target does not
#     bind: 0.2 x 22.4% = 4.5% << 15%.
#   * The fill is at A's open on Jan 8: 0.99 x C5, with
#       C5 = 100 x 1.03 x 1.01 x 1.03 x 1.01 x 1.03 = 111.4691
#       open = 110.3544, shares = 3000 / 110.3544 = 27.18514
#   * Cost of that buy: commission max(27.185 x 0.005, $1) = $1.00, plus 6.5 bps
#     of $3,000 = $1.95: $2.95. Cash = 15,000 - 3,000 - 2.95.
#   * Nothing else trades (no exits; no entry week; B never qualifies), so the
#     final equity is cash + 27.18514 x C22, with C22 = 154.4346.


A_SHARES = 27.185144039745346          # 3000 / 110.35439045730001
A_OPEN_JAN8 = 110.35439045730001       # 0.99 x C5
A_CLOSE_JAN31 = 154.43459139492745     # C22
FINAL_NO_COST = 16198.326611790319     # 12000 + A_SHARES x C22
BUY_COST = 1.0 + 3000.0 * 6.5e-4                                # 2.95


def test_hand_checked_entry_and_hold(tiny):
    sim = simulate(hand_market(), tiny)

    (buy,) = sim.trades                                              # the only trade
    assert (buy.symbol, buy.side, buy.rule) == ("A", "BUY", "ENTRY")
    assert buy.date == pd.Timestamp("2024-01-08")                    # next bar's open, not Jan 5
    assert buy.price == pytest.approx(A_OPEN_JAN8, rel=1e-6)
    assert buy.shares == pytest.approx(A_SHARES, rel=1e-6)
    assert buy.value == pytest.approx(3000.0)
    assert buy.cost == pytest.approx(BUY_COST)

    assert sim.first_signal == pd.Timestamp("2024-01-04")
    assert sim.equity.iloc[0] == pytest.approx(15000.0)              # nothing bought yet
    assert sim.equity.loc["2024-01-05"] == pytest.approx(15000.0)
    assert sim.equity.loc["2024-01-08"] == pytest.approx(
        12000.0 - BUY_COST + A_SHARES * 111.4691, rel=1e-6)          # marked at the close
    assert sim.equity.iloc[-1] == pytest.approx(FINAL_NO_COST - BUY_COST, rel=1e-9)
    assert sim.exposure.iloc[-1] == pytest.approx(
        A_SHARES * A_CLOSE_JAN31 / sim.equity.iloc[-1])


def test_hand_checked_costs_are_exactly_the_difference(tiny):
    with_costs = simulate(hand_market(), tiny)
    without = simulate(hand_market(), tiny, BacktestOptions(apply_costs=False))
    assert without.equity.iloc[-1] == pytest.approx(FINAL_NO_COST, rel=1e-9)
    assert without.equity.iloc[-1] - with_costs.equity.iloc[-1] == pytest.approx(BUY_COST)


def _x4_market():
    """A rises to row 10, then falls 2% a day for four days (rows 11-14), then
    is flat. At the Jan 19 close (row 14): blended momentum
    0.5 x (C13/C12 - 1) + 0.5 x (C13/C11 - 1) = -0.0298 (X4). The trailing stop is
    NOT hit: high 121.84 (C10), ATR 3.452, stop 121.84 - 3 x 3.452 = 111.48 and
    C14 = 112.38 > stop. A ranks 1 or 2 of 2 (X1 needs > 10). Market is on."""
    n = 23
    rets = alternating(11, 0.03, 0.01) + [-0.02] * 4 + [0.0] * (n - 1 - 14)
    return hand_market(n, a_rets=rets)


def test_hand_checked_exit_on_negative_momentum(tiny):
    sim = simulate(_x4_market(), tiny)
    buy, sell = sim.trades
    assert sell.side == "SELL" and sell.rule == "X4"
    assert sell.date == pd.Timestamp("2024-01-22")                   # Monday after the Jan 19 signal
    assert sell.shares == pytest.approx(buy.shares)                  # whole position

    c = path(100, alternating(11, 0.03, 0.01) + [-0.02] * 4 + [0.0] * 8)
    open_sell = 0.99 * c[15]                                         # C15 = C14 (flat day)
    proceeds = A_SHARES * open_sell
    assert sell.price == pytest.approx(open_sell, rel=1e-6)
    assert sell.cost == pytest.approx(1.0 + proceeds * 6.5e-4)       # $1 minimum again
    final = 12000.0 - BUY_COST + proceeds - (1.0 + proceeds * 6.5e-4)
    assert sim.equity.iloc[-1] == pytest.approx(final, rel=1e-6)
    assert sim.exposure.iloc[-1] == 0.0                              # flat, and stays flat
    assert len(sim.trades) == 2                                      # no re-entry: not an entry week


def test_x2_market_off_sells_a_healthy_position_at_the_next_open(tiny):
    # SPY dips on row 9 (Fri Jan 12): below its 2-day SMA, so market_off.
    spy = [0.01] * 8 + [-0.02] + [0.01] * 13
    sim = simulate(hand_market(spy_rets=spy), tiny)
    sell = fills(sim, "SELL")[0]
    assert sell.rule == "X2" and sell.date == pd.Timestamp("2024-01-15")


def test_a_stopped_out_name_is_not_bought_back_the_same_week():
    """Tight stop, so a one-day 3% dip fires X3 on Fri Feb 2 (row 24) - an entry
    week - while A is still rank 1 with positive momentum and vol 67% (< 80%), so
    it passes E1-E9. Without the guard the same open would sell A and buy it
    straight back (the mutant test below shows that it does)."""
    base = variant(_load_cfg(), signals=SMALL_SIGNALS, sizing={"covariance_window": 2},
                   universe=SMALL_UNIVERSE, exit={"trailing_stop_atr": 0.5})
    n = 30
    rets = alternating(24, 0.03, 0.01) + [-0.03] + [0.01] * (n - 1 - 24)
    sim = simulate(hand_market(n, a_rets=rets), base)
    feb5 = [t for t in sim.trades if t.date == pd.Timestamp("2024-02-05")]
    assert [(t.side, t.symbol, t.rule) for t in feb5] == [("SELL", "A", "X3")]


def _load_cfg() -> Config:
    return Config(**yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")))


def test_break_allowing_the_same_week_rebuy_is_caught():
    bad = load_mutant("src.backtest.walkforward",
                      'candidates = replace(sf, table=sf.table.drop(index=list(sold), errors="ignore"))',
                      "candidates = sf")
    base = variant(_load_cfg(), signals=SMALL_SIGNALS, sizing={"covariance_window": 2},
                   universe=SMALL_UNIVERSE, exit={"trailing_stop_atr": 0.5})
    n = 30
    rets = alternating(24, 0.03, 0.01) + [-0.03] + [0.01] * (n - 1 - 24)
    sim = bad.simulate(hand_market(n, a_rets=rets), base)
    feb5 = [(t.side, t.rule) for t in sim.trades if t.date == pd.Timestamp("2024-02-05")]
    assert ("BUY", "ENTRY") in feb5


def test_hand_checked_benchmark_buy_and_hold(tiny):
    """SPY (open = close in this market) bought with all $15,000 at the open of
    the first out-of-sample row (5). 6.5 bps per-share cost and the $1 commission
    minimum leave exactly qty x (price x 1.00065) + 1 = $15,000 spent."""
    data = hand_market()
    curve = buy_and_hold(data, tiny, BacktestOptions(), first_row=5)
    s5 = 100 * 1.01 ** 5
    qty = 14999.0 / (s5 * (1 + 6.5e-4))
    assert curve.index[0] == pd.Timestamp("2024-01-05")               # the close before the fill
    assert curve.iloc[0] == 15000.0
    assert curve.loc["2024-01-08"] == pytest.approx(qty * s5, rel=1e-9)   # costs already paid
    assert curve.iloc[-1] == pytest.approx(qty * 100 * 1.01 ** 22, rel=1e-9)
    free = buy_and_hold(data, tiny, BacktestOptions(apply_costs=False), first_row=5)
    assert free.loc["2024-01-08"] == pytest.approx(15000.0 / s5 * s5)     # no drag at all
    assert curve.iloc[-1] < free.iloc[-1]


# ============================================================ deliberate breaks


def two_stock_market() -> MarketData:
    """A (rising) has the LOWER dollar volume until row 12, then overtakes B
    (falling). With one universe slot, membership is B, chosen at the first
    build, and stays B for the whole first quarter even after A overtakes it on
    volume. The Apr 1 rebuild picks A; the first entry week after that is May 6
    (April's entry decision was Fri Mar 29, before the rebuild). Hindsight
    membership is A throughout."""
    n = 100                                    # Mon 2024-01-01 .. Fri 2024-05-17
    b_vol = [5e4] * 12 + [1e3] * (n - 12)
    return hand_market(n, b_vol=b_vol)


def test_point_in_time_universe_waits_for_the_quarterly_rebuild(tiny):
    one = variant(tiny, universe={"max_symbols": 1})
    pit = simulate(two_stock_market(), one)
    assert [(t.date.strftime("%m-%d"), t.symbol) for t in fills(pit, "BUY")] == [("05-06", "A")]


def test_break_todays_universe_buys_the_stock_before_it_qualified(tiny):
    one = variant(tiny, universe={"max_symbols": 1})
    pit = simulate(two_stock_market(), one)
    hindsight = simulate(two_stock_market(), one, BacktestOptions(point_in_time_universe=False))
    assert fills(hindsight, "BUY")[0].date == pd.Timestamp("2024-01-08")     # four months earlier
    assert hindsight.equity.iloc[-1] > pit.equity.iloc[-1] * 1.02            # and richer
    assert hindsight.equity.iloc[-1] != pit.equity.iloc[-1]


def test_break_same_bar_execution_trades_at_the_signal_close(tiny):
    good = simulate(hand_market(), tiny)
    bad = simulate(hand_market(), tiny, BacktestOptions(same_bar_execution=True))
    (b,) = bad.trades
    assert b.date == pd.Timestamp("2024-01-05")                       # the signal bar itself
    assert b.price == pytest.approx(108.222409, rel=1e-6)             # C4, not next open
    assert b.shares == pytest.approx(3000.0 / 108.222409, rel=1e-6)
    expected = 12000.0 - BUY_COST + b.shares * A_CLOSE_JAN31
    assert bad.equity.iloc[-1] == pytest.approx(expected, rel=1e-9)
    assert bad.equity.iloc[-1] != pytest.approx(good.equity.iloc[-1], rel=1e-4)


def test_break_no_costs_flatters_the_result(tiny):
    good = simulate(hand_market(), tiny)
    bad = simulate(hand_market(), tiny, BacktestOptions(apply_costs=False))
    assert bad.equity.iloc[-1] > good.equity.iloc[-1]
    assert all(t.cost == 0.0 for t in bad.trades) and all(t.cost > 0 for t in good.trades)


@pytest.mark.parametrize("switch", [
    {"same_bar_execution": True}, {"apply_costs": False}, {"point_in_time_universe": False}])
def test_a_broken_run_cannot_pass_acceptance(cfg, switch):
    """Numbers that would pass on every criterion are still FAIL: invalid run."""
    great = _result_with(cfg, options=BacktestOptions(**switch))
    great.equity_curve = noisy(0.0012, 1000, 0.006)
    verdict = check_acceptance(great, cfg, echo=False)
    assert all(ok for ok, _, _ in verdict.results.values())                # would pass...
    assert not verdict.passed and verdict.reason == "invalid run"
    assert "FAIL (invalid run)" in verdict.report()
    good = _result_with(cfg)
    good.equity_curve = noisy(0.0012, 1000, 0.006)
    assert check_acceptance(good, cfg, echo=False).passed                  # the same numbers, valid


def test_the_report_flags_a_broken_run(cfg):
    result = _result_with(cfg, options=BacktestOptions(apply_costs=False))
    text = format_report(result, check_acceptance(result, cfg, echo=False), cfg)
    assert "INVALID RUN" in text
    clean = _result_with(cfg)
    assert "INVALID RUN" not in format_report(clean, check_acceptance(clean, cfg, echo=False), cfg)


# ============================================================== random market


def random_market(seed: int, n: int = 320, k: int = 12) -> MarketData:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2019-01-02", periods=n, name="date")
    names = [f"S{i:02d}" for i in range(k)] + ["SPY"]
    drift = rng.normal(0.0006, 0.0004, len(names))
    vol = rng.uniform(0.010, 0.022, len(names))
    close = pd.DataFrame(
        50 * np.exp(np.cumsum(rng.normal(drift, vol, (n, len(names))), axis=0)),
        index=idx, columns=names)
    opens = close.shift(1).fillna(close) * (1 + rng.normal(0, 0.004, close.shape))
    high = np.maximum(opens, close) * (1 + rng.uniform(0, 0.01, close.shape))
    low = np.minimum(opens, close) * (1 - rng.uniform(0, 0.01, close.shape))
    volume = pd.DataFrame(rng.uniform(1e6, 5e6, close.shape), index=idx, columns=names)
    sectors = {s: f"Sec{i % 4}" for i, s in enumerate(names)}
    return MarketData(opens, high, low, close, volume, sectors)


@pytest.fixture(scope="module")
def rcfg(cfg) -> Config:
    """Small signal windows so a 320-bar market trades often."""
    return variant(
        cfg,
        signals={"momentum": {"skip_days": 2, "lookback_short": 4, "lookback_long": 6},
                 "volatility": {"window": 5}, "trend_filter": {"ma_period": 4},
                 "atr": {"period": 3}},
        sizing={"covariance_window": 5}, universe={**SMALL_UNIVERSE, "adv_window_days": 3})


@pytest.fixture(scope="module")
def rsim(rcfg):
    return simulate(random_market(1), rcfg)


def test_engine_invariants(rsim, rcfg):
    trades = rsim.trades
    assert len(trades) >= 20, "the market must trade for these checks to mean anything"
    assert rsim.exposure.max() <= 1.0 + 1e-9                           # never levered
    assert (rsim.equity > 0).all() and rsim.equity.notna().all()

    data = random_market(1)
    dates = data.closes.index
    decisions, entry_rows = weekly_schedule(dates)
    entry_fills = {dates[r + 1] for r in entry_rows}
    fill_dates = {dates[d + 1] for d in decisions if d + 1 < len(dates)}
    assert {t.date for t in trades} <= fill_dates                      # only ever at a weekly fill
    assert {t.date for t in trades if t.rule == "ENTRY"} <= entry_fills   # entries: monthly
    for d in {t.date for t in trades if t.rule == "ENTRY"}:
        assert sum(t.rule == "ENTRY" and t.date == d for t in trades) <= rcfg.entry.max_new_per_rebalance

    held: dict[str, float] = {}
    for t in trades:                                                   # replay: at most 6 names, no shorts
        held[t.symbol] = held.get(t.symbol, 0.0) + (t.shares if t.side == "BUY" else -t.shares)
        assert held[t.symbol] > -1e-9
        held = {s: q for s, q in held.items() if q > 1e-9}
        assert len(held) <= rcfg.risk.max_positions
    assert {t.rule for t in trades if t.side == "SELL"} <= {"X1", "X2", "X3", "X4", "DRIFT_TRIM"}
    assert all(t.cost > 0 for t in trades)


# ------------------------------------------------------------- look-ahead


def splice_future(data: MarketData, other: MarketData, cut_row: int) -> MarketData:
    """`data` up to and including `cut_row`, a different world after it."""
    def sp(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
        out = a.copy()
        out.iloc[cut_row + 1:] = b.iloc[cut_row + 1:].to_numpy()
        return out
    return MarketData(sp(data.opens, other.opens), sp(data.highs, other.highs),
                      sp(data.lows, other.lows), sp(data.closes, other.closes),
                      sp(data.volumes, other.volumes), data.sectors, data.allowed)


def lookahead(sim_fn, data, other, cut_row) -> tuple[bool, bool]:
    """(equity up to the cut unchanged, decisions made by the cut unchanged).

    Scramble every price and volume after `cut_row`. Equity up to the cut can
    only depend on bars up to the cut. And what was decided at the cut (which
    fills on the next bar) can only depend on bars up to the cut too, checked on
    the trade's symbol, side and rule, which do not depend on the fill price.
    """
    dates = data.closes.index
    base = sim_fn(data)
    alt = sim_fn(splice_future(data, other, cut_row))
    t = dates[cut_row]
    eq_same = np.array_equal(base.equity.loc[:t].to_numpy(), alt.equity.loc[:t].to_numpy())

    def decided(sim):
        return [(x.date, x.symbol, x.side, x.rule) for x in sim.trades if x.date <= dates[cut_row + 1]]

    return eq_same, decided(base) == decided(alt), base, alt


def cuts_around_trades(sim, data, limit=8) -> list[int]:
    dates = data.closes.index
    rows = sorted({dates.get_loc(t.date) for t in sim.trades})
    picks = rows[:: max(1, len(rows) // limit)][:limit]
    return sorted({r - 1 for r in picks} | {r for r in picks[:3]})


def test_whole_engine_has_no_lookahead(rcfg, rsim):
    data, other = random_market(1), random_market(99)
    cuts = cuts_around_trades(rsim, data)
    assert len(cuts) >= 6
    for cut in cuts:
        eq_same, dec_same, base, alt = lookahead(lambda d: simulate(d, rcfg), data, other, cut)
        assert eq_same, f"equity up to row {cut} changed when later prices were scrambled"
        assert dec_same, f"decisions at row {cut} changed when later prices were scrambled"
        assert base.equity.iloc[-1] != alt.equity.iloc[-1]             # the scramble did bite


def test_break_filling_at_a_future_price_is_caught(rcfg, rsim):
    bad = load_mutant("src.backtest.walkforward", "return arr[i, j]",
                      "return self.close[min(i + NEXT_BAR, self.n - 1), j]")
    data, other = random_market(1), random_market(99)
    caught = [not lookahead(lambda d: bad.simulate(d, rcfg), data, other, c)[0]
              for c in cuts_around_trades(rsim, data)]
    assert any(caught)


def test_break_deciding_on_a_future_signal_is_caught(rcfg, rsim):
    bad = load_mutant("src.backtest.walkforward", "sf = self.panel.at(t)",
                      "sf = self.panel.at(self.dates[min(i + NEXT_BAR, self.n - 1)])")
    data, other = random_market(1), random_market(99)
    caught = [not lookahead(lambda d: bad.simulate(d, rcfg), data, other, c)[1]
              for c in cuts_around_trades(rsim, data)]
    assert any(caught)


def test_break_entries_every_week_changes_the_trading(rcfg, rsim):
    bad = load_mutant("src.backtest.walkforward", "if i in self.entry_decisions:",
                      "if i in self.decision_set:")
    data = random_market(1)
    weekly = bad.simulate(data, rcfg)
    entry_fills = {data.closes.index[r + 1] for r in weekly_schedule(data.closes.index)[1]}
    assert len(weekly.trades) != len(rsim.trades)
    assert {t.date for t in weekly.trades if t.rule == "ENTRY"} - entry_fills   # off-schedule entries


# ========================================================= walk-forward run


@pytest.fixture(scope="module")
def long_result(cfg):
    """Real signal windows over a synthetic 8-year market: 3 complete windows
    plus a partial one."""
    rng = np.random.default_rng(5)
    n, k = 2000, 25
    idx = pd.bdate_range("2012-01-02", periods=n, name="date")
    names = [f"S{i:02d}" for i in range(k)] + ["SPY"]
    drift = rng.normal(0.0004, 0.0004, len(names))
    vol = rng.uniform(0.010, 0.022, len(names))
    close = pd.DataFrame(50 * np.exp(np.cumsum(rng.normal(drift, vol, (n, len(names))), axis=0)),
                         index=idx, columns=names)
    opens = close.shift(1).fillna(close) * (1 + rng.normal(0, 0.003, close.shape))
    high = np.maximum(opens, close) * 1.004
    low = np.minimum(opens, close) * 0.996
    volume = pd.DataFrame(rng.uniform(1e6, 5e6, close.shape), index=idx, columns=names)
    data = MarketData(opens, high, low, close, volume, {s: f"Sec{i % 5}" for i, s in enumerate(names)})
    return data, walk_forward(cfg, data)


def test_out_of_sample_curve_starts_at_capital_and_windows_chain_to_it(cfg, long_result):
    data, res = long_result
    dates = data.closes.index
    first = res.windows[0]
    assert res.equity_curve.iloc[0] == pytest.approx(cfg.account.capital)
    assert res.benchmark_curve.iloc[0] == pytest.approx(cfg.account.capital)
    assert res.equity_curve.index.equals(res.benchmark_curve.index)
    assert res.equity_curve.index[1].date() == first.oos_start          # nothing in-sample is reported
    assert res.equity_curve.index[-1].date() == res.windows[-1].oos_end

    chained = np.prod([1 + w.strategy_return for w in res.windows])
    assert res.equity_curve.iloc[-1] / cfg.account.capital == pytest.approx(chained, rel=1e-9)
    chained_b = np.prod([1 + w.benchmark_return for w in res.windows])
    assert res.benchmark_curve.iloc[-1] / cfg.account.capital == pytest.approx(chained_b, rel=1e-9)
    assert all(t.date >= pd.Timestamp(first.oos_start) for t in res.trades)
    assert [w.partial for w in res.windows][:-1] == [False] * (len(res.windows) - 1)
    assert len(res.windows) >= 3 and res.windows[-1].oos_end == dates[-1].date()


def test_walk_forward_windows_are_a_year_apart_and_contiguous(long_result):
    _, res = long_result
    for prev, nxt in zip(res.windows, res.windows[1:]):
        gap = (pd.Timestamp(nxt.oos_start) - pd.Timestamp(prev.oos_end)).days
        assert 1 <= gap <= 4                                            # a weekend, at most
    starts = [pd.Timestamp(w.oos_start) for w in res.windows]
    assert all(360 <= (b - a).days <= 372 for a, b in zip(starts, starts[1:]))


def test_metrics_are_finite_and_consistent(long_result):
    _, res = long_result
    for value in (res.oos_cagr, res.benchmark_cagr, res.max_drawdown, res.sharpe,
                  res.annual_turnover, res.worst_rolling_12m):
        assert np.isfinite(value)
    assert 0 <= res.max_drawdown < 1
    assert res.exposure.between(0, 1 + 1e-9).all()
    for w in res.windows:
        assert w.excess_return == pytest.approx(w.strategy_return - w.benchmark_return)


def test_not_enough_history_raises(cfg):
    with pytest.raises(BacktestError, match="not enough history"):
        walk_forward(cfg, random_market(2, n=400))


def test_a_missing_benchmark_bar_is_refused(cfg):
    data = random_market(3)
    data.closes.iloc[10, data.closes.columns.get_loc("SPY")] = np.nan
    with pytest.raises(BacktestError, match="missing bars"):
        simulate(data, cfg)


# ============================================================== acceptance


def curve(daily: float, n: int, start="2020-01-01", shock: tuple[int, float] | None = None):
    idx = pd.bdate_range(start, periods=n)
    vals = 15000.0 * (1 + daily) ** np.arange(n)
    if shock:
        at, drop = shock
        vals[at:] *= (1 - drop)
    return pd.Series(vals, index=idx)


def window(excess: float, i: int = 0) -> WindowResult:
    return WindowResult(pd.Timestamp("2015-01-01").date(), pd.Timestamp("2018-01-01").date(),
                        pd.Timestamp("2018-01-02").date(), pd.Timestamp("2019-01-01").date(),
                        0.10 + excess, 0.10, excess, 0.1, 1.0, 1.0, 5)


def _result_with(cfg, strat_daily=0.0006, bench_daily=0.0003, n=1000, excess=(0.05, 0.04, 0.03),
                 shock=None, options=None) -> BacktestResult:
    trades = []
    return BacktestResult(
        windows=[window(e) for e in excess],
        equity_curve=curve(strat_daily, n, shock=shock), benchmark_curve=curve(bench_daily, n),
        trades=trades, options=options or BacktestOptions(),
        periods_per_year=cfg.signals.volatility.annualize,
        exposure=pd.Series(0.5, index=curve(0.0, n).index),
    )


def noisy(daily: float, n: int, sd: float, seed: int = 0) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.Series(15000.0 * np.cumprod(1 + rng.normal(daily, sd, n)), index=idx)


def test_acceptance_all_pass(cfg, capsys):
    res = _result_with(cfg)
    res.equity_curve = noisy(0.0012, 1000, 0.006)
    res.trades = []
    v = check_acceptance(res, cfg)
    out = capsys.readouterr().out
    assert v.passed, out
    assert all(f"  A{i}  PASS" in out for i in range(1, 7)) and "VERDICT: PASS" in out


def test_a1_fails_when_spy_wins_and_says_to_buy_spy(cfg, capsys):
    res = _result_with(cfg, strat_daily=0.0001, bench_daily=0.0004)
    v = check_acceptance(res, cfg)
    out = capsys.readouterr().out
    assert not v.results["A1"][0] and not v.passed
    assert "A1  FAIL" in out and "Buy SPY" in out


def test_a2_max_drawdown_boundary(cfg):
    assert check_acceptance(_result_with(cfg, shock=(500, 0.30)), cfg, echo=False).results["A2"][0]
    hit = check_acceptance(_result_with(cfg, shock=(500, 0.40)), cfg, echo=False)
    assert not hit.results["A2"][0] and hit.results["A2"][1] == pytest.approx(0.40, abs=1e-3)


def test_a3_worst_rolling_year(cfg):
    res = _result_with(cfg, shock=(500, 0.40))
    v = check_acceptance(res, cfg, echo=False)
    assert not v.results["A3"][0]
    assert v.results["A3"][1] < -0.30


def test_a4_sharpe_threshold(cfg):
    res = _result_with(cfg)
    res.equity_curve = noisy(0.0002, 1000, 0.02)           # low return, high vol: Sharpe ~ 0.16
    v = check_acceptance(res, cfg, echo=False)
    assert not v.results["A4"][0] and v.results["A4"][1] < cfg.backtest.acceptance.min_sharpe


def test_a5_turnover(cfg):
    res = _result_with(cfg)
    res.equity_curve = noisy(0.0006, 1000, 0.006)
    mean_equity = res.equity_curve.mean()
    years = (res.equity_curve.index[-1] - res.equity_curve.index[0]).days / 365.25
    day1 = res.equity_curve.index[1]

    def one_trade(value):
        return [wf.Trade(day1, "A", "BUY", 1.0, 1.0, value, 0.0, "ENTRY")]

    # turnover = half of traded value / mean equity / years
    ok = 3.0 * 2 * mean_equity * years          # 300%
    res.trades = one_trade(ok)
    assert res.annual_turnover == pytest.approx(3.0)
    assert check_acceptance(res, cfg, echo=False).results["A5"][0]
    res.trades = one_trade(5.0 * 2 * mean_equity * years)
    assert not check_acceptance(res, cfg, echo=False).results["A5"][0]


@pytest.mark.parametrize("excess,share,passes", [
    ((0.10, 0.05, 0.05), 0.5, True),             # exactly half is allowed
    ((0.20, 0.05, 0.05), 2 / 3, False),
    ((0.10, 0.10, 0.10, 0.10), 0.25, True),
    ((0.30, -0.10, -0.05), 0.30 / 0.15, False),  # one lucky year carrying the rest
    ((-0.05, -0.02), float("nan"), False),       # no edge to apportion: fail closed
    ((), float("nan"), False),
])
def test_a6_single_window_contribution(cfg, excess, share, passes):
    res = _result_with(cfg, excess=excess)
    got = res.max_single_window_contribution
    assert (np.isnan(got) and np.isnan(share)) or got == pytest.approx(share)
    assert check_acceptance(res, cfg, echo=False).results["A6"][0] is passes


def test_a_criterion_that_cannot_be_computed_fails(cfg):
    res = _result_with(cfg, n=100)                          # under a year: no rolling 12m
    v = check_acceptance(res, cfg, echo=False)
    assert np.isnan(v.results["A3"][1]) and not v.results["A3"][0] and not v.passed


def test_beat_benchmark_can_be_switched_off_in_config(cfg):
    off = variant(cfg, backtest={"acceptance": {"beat_benchmark": False}})
    res = _result_with(off, strat_daily=0.0001, bench_daily=0.0004)
    assert check_acceptance(res, off, echo=False).results["A1"][0]


def test_report_states_what_the_backtest_leaves_out(cfg):
    res = _result_with(cfg)
    text = format_report(res, check_acceptance(res, cfg, echo=False), cfg)
    for phrase in ("E5 (AI veto) always passes", "Cash earns 0%", "point-in-time",
                   "today's S&P 500 membership", "sectors and security type",
                   "No same-bar execution", "Out-of-sample only"):
        assert phrase in text, phrase
