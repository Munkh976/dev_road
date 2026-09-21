"""Backtest engine: costs, schedule, windows, hand-checked trades, look-ahead,
acceptance, and deliberate breaks. Synthetic markets only; no real data."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import yaml

from src.backtest import walkforward as wf
from src.strategy import plan as plan_module
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
    return variant(cfg, signals=SMALL_SIGNALS, universe=SMALL_UNIVERSE, reentry={"sma_days": 2})


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
    decisions = weekly_schedule(idx)
    dates = [idx[d].strftime("%a %m-%d") for d in decisions]
    assert dates[:4] == ["Fri 01-05", "Fri 01-12", "Fri 01-19", "Thu 01-25"]


def test_every_week_is_a_decision_week_there_is_no_monthly_entry_schedule():
    """v1 entered only in the first fill of each month. v2 refills toward the target
    every week, so there are as many decision rows as trading weeks."""
    idx = pd.bdate_range("2024-01-01", "2024-06-28")
    decisions = weekly_schedule(idx)
    assert len(decisions) == 26 and all(idx[d].dayofweek == 4 for d in decisions)


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
# windows (skip 1, lookbacks 2 and 3, vol window 2, SMA 2, ATR 2, re-entry SMA 2).
#
#   * Signals first exist at row 3 (Thu Jan 4). Decisions are the last bar of each
#     week: rows 4, 9, 14, 19 (Fridays) and 22 (the last bar; nothing to fill on).
#   * The graded filter starts DEFENSIVE (fail closed). SPY is above its 2-day SMA
#     at every check, so the reading at row 4 is the first of two, and at row 9 the
#     second: the mode becomes NORMAL after the row 9 decision, and the first buy
#     fills on the next bar, Mon Jan 15 (row 10). Nothing can be bought before that.
#   * At the Jan 12 close (row 9) A ranks 1, momentum > 0, vol 22.4% (< 80%), sector
#     Tech; B ranks 2 but falls (E3). One name, so sizing pins A at the 15% cap:
#     $2,250 of $15,000 (the 85% target is far away, which is why it keeps refilling
#     and cannot: there is nothing else to buy).
#   * Filled at A's open on Jan 15: 0.99 x C10, C10 = 100 x (1.03 x 1.01)^5.
#   * Cost of that buy: commission max(shares x 0.005, $1) = $1.00, plus 6.5 bps
#     of $2,250 = $1.4625.
#   * A then rises faster than the account, so it drifts above the 15% cap and is
#     trimmed back to it every Friday (CAP_TRIM), filled the Monday after.

C10 = 100 * (1.03 * 1.01) ** 5
C14 = C10 * 1.03 * 1.01 * 1.03 * 1.01
A_OPEN_JAN15 = 0.99 * C10
A_SHARES = 2250.0 / A_OPEN_JAN15
BUY_COST = 1.0 + 2250.0 * 6.5e-4                                # $1 minimum + 6.5 bps = 2.4625


def test_hand_checked_first_entry_waits_for_two_confirming_weeks(tiny):
    sim = simulate(hand_market(), tiny)

    buy = sim.trades[0]
    assert (buy.symbol, buy.side, buy.rule) == ("A", "BUY", "ENTRY")
    assert buy.date == pd.Timestamp("2024-01-15")                    # not Jan 8: one reading is not two
    assert buy.price == pytest.approx(A_OPEN_JAN15, rel=1e-12)
    assert buy.value == pytest.approx(2250.0) and buy.shares == pytest.approx(A_SHARES)
    assert buy.cost == pytest.approx(BUY_COST)

    assert sim.first_signal == pd.Timestamp("2024-01-04")
    for day in ("2024-01-05", "2024-01-08", "2024-01-12"):
        assert sim.equity.loc[day] == pytest.approx(15000.0)         # flat until Jan 15
    assert sim.equity.loc["2024-01-15"] == pytest.approx(
        15000.0 - 2250.0 - BUY_COST + A_SHARES * C10, rel=1e-12)     # marked at the close
    assert sim.exposure.loc["2024-01-15"] == pytest.approx(A_SHARES * C10 / sim.equity.loc["2024-01-15"])


def test_hand_checked_regime_modes_by_bar(tiny):
    """Mode in force on each bar, before that bar's own decision: DEFENSIVE through
    the row 9 decision's bar (Jan 12), NORMAL from Jan 15."""
    sim = simulate(hand_market(), tiny)
    assert sim.regime.loc["2024-01-04":"2024-01-12"].eq("defensive").all()
    assert sim.regime.loc["2024-01-15":].eq("normal").all()


def test_hand_checked_cap_trim(tiny):
    """At the Jan 19 close (row 14) A is worth shares x C14 = $2,459.6 of a
    $15,207 account: 16.2% against the 15% cap. The excess is sold at the next
    open (Mon Jan 22), computed as a share of the shares held at the decision."""
    sim = simulate(hand_market(), tiny)
    trim = sim.trades[1]
    cash = 15000.0 - 2250.0 - BUY_COST
    value = A_SHARES * C14
    equity = cash + value
    fraction = (value - 0.15 * equity) / value
    assert (trim.symbol, trim.side, trim.rule) == ("A", "SELL", "CAP_TRIM")
    assert trim.date == pd.Timestamp("2024-01-22")
    assert trim.shares == pytest.approx(fraction * A_SHARES, rel=1e-9)
    c15 = C14 * 1.03
    assert trim.price == pytest.approx(0.99 * c15, rel=1e-12)
    assert fraction == pytest.approx(0.0726, abs=1e-4)               # ~7% of the position


def test_the_ledger_reconciles_with_the_equity_curve(tiny):
    """Cash rebuilt from the trades alone, plus shares at the last close, is the
    final equity: nothing is created or lost outside the trade list."""
    data = hand_market()
    sim = simulate(data, tiny)
    cash = 15000.0
    shares: dict[str, float] = {}
    for t in sim.trades:
        sign = 1 if t.side == "BUY" else -1
        cash += -sign * t.value - t.cost
        shares[t.symbol] = shares.get(t.symbol, 0.0) + sign * t.shares
    last = data.closes.iloc[-1]
    assert sim.equity.iloc[-1] == pytest.approx(cash + sum(q * last[s] for s, q in shares.items()), rel=1e-12)
    assert sim.exposure.max() <= 1.0 and (cash >= 0)


def test_hand_checked_costs_are_charged_and_switching_them_off_flatters(tiny):
    with_costs = simulate(hand_market(), tiny)
    without = simulate(hand_market(), tiny, BacktestOptions(apply_costs=False))
    assert all(t.cost > 0 for t in with_costs.trades)
    assert without.equity.iloc[-1] > with_costs.equity.iloc[-1]
    assert without.equity.iloc[-1] - with_costs.equity.iloc[-1] == pytest.approx(
        sum(t.cost for t in with_costs.trades), rel=0.02)


def _x4_market():
    """A rises to row 10, then falls 2% a day for four days (rows 11-14), then is
    flat. At the Jan 19 close (row 14): blended momentum -0.0298 (X4). Only 7.8%
    below its high, so no ladder level (12%) is reached. A ranks 1 or 2 of 2 (X1
    needs > 16)."""
    n = 23
    rets = alternating(11, 0.03, 0.01) + [-0.02] * 4 + [0.0] * (n - 1 - 14)
    return hand_market(n, a_rets=rets)


def test_hand_checked_exit_on_negative_momentum(tiny):
    sim = simulate(_x4_market(), tiny)
    buy, sell = sim.trades
    assert sell.side == "SELL" and sell.rule == "X4"
    assert sell.date == pd.Timestamp("2024-01-22")                   # Monday after the Jan 19 signal
    assert sell.shares == pytest.approx(buy.shares)                  # the whole position

    c = path(100, alternating(11, 0.03, 0.01) + [-0.02] * 4 + [0.0] * 8)
    open_sell = 0.99 * c[15]                                         # C15 = C14 (flat day)
    proceeds = A_SHARES * open_sell
    assert sell.price == pytest.approx(open_sell, rel=1e-6)
    assert sell.cost == pytest.approx(1.0 + proceeds * 6.5e-4)       # the $1 minimum again
    final = 15000.0 - 2250.0 - BUY_COST + proceeds - (1.0 + proceeds * 6.5e-4)
    assert sim.equity.iloc[-1] == pytest.approx(final, rel=1e-9)
    assert sim.exposure.iloc[-1] == 0.0                              # flat, and stays flat
    assert len(sim.trades) == 2                                      # momentum is 0 afterwards: no re-entry


def test_market_off_no_longer_sells_a_healthy_position_at_once(tiny):
    """v1's X2 sold at the first reading. v2 needs two consecutive readings below,
    and the response is a pro rata trim to 40%, which a book at 15% does not need."""
    spy = [0.01] * 13 + [-0.02] + [0.01] * 8                         # the Fri Jan 19 reading only
    sim = simulate(hand_market(spy_rets=spy), tiny)
    assert not any(t.side == "SELL" and t.rule in ("X2", "REGIME_TRIM") for t in sim.trades)
    assert sim.regime.loc["2024-01-15":].eq("normal").all()          # a single reading changed nothing


# --------------------------------------------------------------- the ladder


def ladder_market(recovery: list[float]) -> MarketData:
    """A peaks on row 14 (Fri Jan 19, close 152.43), falls four days to row 18
    and 1% more on row 19: 14.2% below its peak at the Jan 26 close but still 6.5%
    ABOVE its entry price (bought at 121.8). Then it recovers as `recovery` says."""
    rets = alternating(10, 0.03, 0.01) + [0.02] + [0.06, 0.05, 0.06, 0.05] \
        + [-0.04, -0.03, -0.04, -0.03] + [-0.01] + [0.01] + recovery
    return hand_market(len(rets) + 1, a_rets=rets)


def no_x4(tiny):
    """Momentum is measured over 2-3 bars in these tiny markets, so a real fall trips X4
    before the ladder can show. Switch X4 off to see the ladder alone."""
    return variant(tiny, exit={"exit_on_negative_momentum": False})


STRONG = [0.05, 0.04, 0.05, 0.04, 0.05, 0.04, 0.05]
WEAK = [0.01, 0.012] * 4


def test_the_ladder_sells_a_third_at_12_percent_below_the_peak_not_below_entry(tiny):
    data = ladder_market(STRONG)
    c = data.closes["A"]
    sim = simulate(data, no_x4(tiny))
    rules = [(t.date.strftime("%m-%d"), t.side, t.rule) for t in sim.trades]
    assert rules[:3] == [("01-15", "BUY", "ENTRY"), ("01-22", "SELL", "CAP_TRIM"), ("01-29", "SELL", "L1")]

    peak, close19, entry = c.iloc[14], c.iloc[19], sim.trades[0].price
    assert 1 - close19 / peak >= 0.12 and 1 - close19 / peak < 0.20      # level 1 only
    assert close19 > entry                                                # up on entry, down from the peak
    held = sim.trades[0].shares - sim.trades[1].shares                    # after the cap trim
    l1 = sim.trades[2]
    assert l1.shares == pytest.approx(held / 3, rel=1e-9)                 # a third of the position
    assert l1.price == pytest.approx(0.99 * c.iloc[20], rel=1e-12)        # at the next open


def test_a_partly_sold_name_is_topped_up_only_after_it_beats_its_prior_peak(tiny):
    data = ladder_market(STRONG)
    c = data.closes["A"]
    peak = c.iloc[14]
    strong = simulate(data, no_x4(tiny))
    topups = [t for t in strong.trades if t.rule == "TOPUP"]
    assert len(topups) == 1 and topups[0].side == "BUY"
    decision = data.closes.index.get_loc(topups[0].date) - 1              # the Friday before the fill
    assert c.iloc[decision] > peak                                         # closed above its old peak
    assert c.iloc[decision - 5] < peak                                     # and had not the week before

    weak_data = ladder_market(WEAK)
    assert weak_data.closes["A"].iloc[15:].max() < peak                    # never gets back to it
    weak = simulate(weak_data, no_x4(tiny))
    assert [t.rule for t in weak.trades if t.side == "BUY"] == ["ENTRY"]   # so never bought back


RETRACE = [-0.04, -0.03, -0.04, -0.03, -0.04]      # rows 30-34: ~16.8% off the new high, level 1 only


def test_a_topup_rearms_the_ladder_and_restarts_the_high(tiny):
    """After the top-up (Mon Feb 5, a full recovery above the old peak) the name is a
    fresh position: a later 12% fall from the new high fires L1 again. Without the
    reset its first protection would be L2 at -20%, which this fall does not reach."""
    data = ladder_market(STRONG + [0.01, 0.012] + RETRACE + [0.005])
    sim = simulate(data, no_x4(tiny))
    assert [t.rule for t in sim.trades if t.rule.startswith("L") or t.rule == "TOPUP"] == [
        "L1", "TOPUP", "L1"]
    c = data.closes["A"]
    topup = next(t for t in sim.trades if t.rule == "TOPUP")
    high = c.loc[topup.date:].iloc[:10].max()
    second = [t for t in sim.trades if t.rule == "L1"][1]
    decision = c.index.get_loc(second.date) - 1
    assert 0.12 <= 1 - c.iloc[decision] / high < 0.20              # level 1 only, from the NEW high
    assert c.iloc[decision] > c.iloc[14] * 0.88                    # and not 12% below the OLD peak
    assert not any(t.rule == "L2" for t in sim.trades)


def test_break_topup_does_not_rearm_the_ladder(tiny):
    bad = load_mutant(
        "src.backtest.walkforward",
        '            self.ladder_fired[sym] = 0\n            self.highs[sym] = float("nan")\n',
        "            pass\n")
    data = ladder_market(STRONG + [0.01, 0.012] + RETRACE + [0.005])
    good = simulate(data, no_x4(tiny))
    broken = bad.simulate(data, no_x4(tiny))
    assert [t.rule for t in good.trades if t.rule.startswith("L")] == ["L1", "L1"]
    assert [t.rule for t in broken.trades if t.rule.startswith("L")] == ["L1"]


def test_break_the_stop_is_measured_from_entry_not_the_peak(tiny):
    """The high never rises above the entry-day close: a 14% fall from the peak,
    while still up on entry, no longer reaches level 1."""
    bad = load_mutant(
        "src.backtest.walkforward",
        "self.highs[sym] = float(np.fmax(self.highs[sym], c))",
        "self.highs[sym] = c if np.isnan(self.highs[sym]) else self.highs[sym]")
    data = ladder_market(STRONG)
    good = simulate(data, no_x4(tiny))
    broken = bad.simulate(data, no_x4(tiny))
    assert any(t.rule == "L1" for t in good.trades)
    assert not any(t.rule.startswith("L") for t in broken.trades)


# ------------------------------------------------------ full exit and re-entry


def reentry_market() -> MarketData:
    """A is bought Jan 15, then crashes 25% then 10% (rows 15-16) and is 30.5% below
    its peak by the Jan 26 close: L3 sells everything on Mon Jan 29 (row 20).
    It then rises 2% a day. At the Fri Jan 26 (row 24) decision it has a positive
    momentum and rank 1 but has just dipped, so it closes BELOW its own 2-day SMA:
    re-entry is refused. The next Friday (row 29) it is above: re-entry Mon Feb 12."""
    rets = (alternating(10, 0.03, 0.01) + [0.02] + [0.02, 0.015, 0.02, 0.015]
            + [-0.25, -0.10, 0.01, 0.012, 0.008] + [0.02] * 4 + [-0.01] + [0.02] * 4
            + [0.01] + [0.015, 0.01, 0.015, 0.01, 0.015, 0.01])
    return hand_market(len(rets) + 1, a_rets=rets)


def sequence(sim, sym="A"):
    return [(t.date.strftime("%m-%d"), t.side, t.rule) for t in sim.trades if t.symbol == sym]


def test_full_exit_then_reentry_needs_the_50_day_sma_condition(tiny):
    data = reentry_market()
    sim = simulate(data, tiny)
    seq = sequence(sim)
    assert ("01-29", "SELL", "L3") in seq                                # the disaster level: all of it
    reentry = [x for x in seq if x[2] == "REENTRY"]
    assert reentry == [("02-12", "BUY", "REENTRY")]                     # not Feb 5
    c = data.closes["A"]
    assert c.iloc[24] < (c.iloc[24] + c.iloc[23]) / 2                    # the dip: below its 2-day SMA
    assert c.iloc[29] > (c.iloc[29] + c.iloc[28]) / 2                    # above it a week later
    l3 = next(t for t in sim.trades if t.rule == "L3")
    assert l3.shares == pytest.approx(
        sum(t.shares for t in sim.trades if t.side == "BUY" and t.date < l3.date)
        - sum(t.shares for t in sim.trades if t.side == "SELL" and t.date < l3.date), rel=1e-9)


def test_break_reentry_without_the_50_day_condition_returns_a_week_early(tiny, monkeypatch):
    bad = load_mutant("src.strategy.rules", 'and row["close"] > row["sma_reentry"]', "and True")
    data = reentry_market()
    good = [x for x in sequence(simulate(data, tiny)) if x[2] == "REENTRY"]
    monkeypatch.setattr(plan_module, "evaluate_entries", bad.evaluate_entries)
    broken = [x for x in sequence(simulate(data, tiny)) if x[2] == "REENTRY"]
    assert good == [("02-12", "BUY", "REENTRY")] and broken == [("02-05", "BUY", "REENTRY")]


# --------------------------------------------------------- the graded filter


def trend_market(n: int = 62, k: int = 8) -> MarketData:
    """k steadily rising names in a sector each, and an SPY that rises for 24 bars,
    falls 2% a day for 15 (rows 25-39), then rises 1.5% a day. Fridays are rows 4, 9,
    14... so the readings below its 2-day SMA are rows 29 and 34 (DEFENSIVE after
    row 34, the trim filling Mon Feb 19), and above are rows 44 and 49 (NORMAL after
    row 49, filling Mon Mar 11)."""
    idx = pd.bdate_range("2024-01-01", periods=n, name="date")
    names = [f"S{i}" for i in range(k)]
    cols = {}
    for i, s in enumerate(names):
        base = 0.004 + 0.0008 * i
        cols[s] = path(100, [base + (0.003 if t % 2 else -0.003) for t in range(1, n)])
    cols["SPY"] = path(100, [0.01] * 24 + [-0.02] * 15 + [0.015] * (n - 1 - 39))
    close = pd.DataFrame(cols, index=idx)
    vol = pd.DataFrame(1e6, index=idx, columns=close.columns)
    sectors = {s: f"Sec{i}" for i, s in enumerate(names)} | {"SPY": "ETF"}
    return MarketData(close * 0.999, close * 1.004, close * 0.996, close, vol, sectors)


@pytest.fixture(scope="module")
def trend(tiny):
    return trend_market(), simulate(trend_market(), tiny)


def test_the_first_refill_buys_six_names_in_one_week_at_the_cap_free_weights(trend):
    """No two-a-month limit: six names on Jan 15, each $12,750 / 6 = $2,125 (14.2%,
    under the 15% cap; five would cap at 75% invested, short of 85%)."""
    _, sim = trend
    firsts = [t for t in sim.trades if t.date == pd.Timestamp("2024-01-15")]
    assert [t.symbol for t in firsts] == ["S7", "S6", "S5", "S4", "S3", "S2"]   # best ranks first
    assert all(t.rule == "ENTRY" and t.value == pytest.approx(2125.0) for t in firsts)


def test_defensive_trims_to_40_percent_then_the_book_comes_back(trend):
    data, sim = trend
    assert sim.exposure.loc["2024-02-16"] > 0.80                              # the 85% book, drifted up
    assert sim.regime.loc["2024-02-16"] == "normal"
    assert sim.regime.loc["2024-02-19"] == "defensive"                        # after the row 34 decision
    inside = sim.exposure.loc["2024-02-19":"2024-03-08"]
    assert inside.between(0.38, 0.46).all()                                   # 40% + at most the 5% band
    assert sim.regime.loc["2024-03-11"] == "normal"
    assert sim.exposure.loc["2024-03-15":].between(0.80, 0.95).all()          # back to the 85% target


def test_defensive_trims_are_pro_rata_and_nothing_is_bought_while_it_lasts(trend):
    _, sim = trend
    trims = [t for t in sim.trades if t.rule == "REGIME_TRIM"]
    assert len(trims) == 6 and {t.date for t in trims} == {pd.Timestamp("2024-02-19")}
    held = {}
    for t in sim.trades:                        # shares held after that day's cap trim, if any
        if t.date <= pd.Timestamp("2024-02-19") and t.rule != "REGIME_TRIM":
            held[t.symbol] = held.get(t.symbol, 0.0) + (t.shares if t.side == "BUY" else -t.shares)
    shares = [t.shares / held[t.symbol] for t in trims]
    assert max(shares) - min(shares) < 1e-9                                   # the same share of each
    assert 0.40 < shares[0] < 0.60                                            # 1 - 40%/85%
    inside = [t for t in sim.trades if pd.Timestamp("2024-02-19") < t.date < pd.Timestamp("2024-03-11")]
    assert not [t for t in inside if t.side == "BUY"]                         # no new names, no top-ups


def test_trimmed_names_are_restored_when_the_filter_is_back(trend):
    _, sim = trend
    restores = [t for t in sim.trades if t.rule == "RESTORE"]
    assert len(restores) == 6 and {t.date for t in restores} == {pd.Timestamp("2024-03-11")}
    assert {t.symbol for t in restores} == {t.symbol for t in sim.trades if t.rule == "REGIME_TRIM"}


def test_break_trimmed_names_are_never_flagged_for_restore(tiny, trend):
    data, good = trend
    bad = load_mutant("src.backtest.walkforward", "if o.rule == REGIME_TRIM:    # comes back when the filter does",
                      "if False:")
    broken = bad.simulate(data, tiny)
    assert any(t.rule == "RESTORE" for t in good.trades)
    assert not any(t.rule == "RESTORE" for t in broken.trades)
    assert broken.exposure.iloc[-1] < good.exposure.iloc[-1] - 0.10           # and it stays under-invested


def test_break_the_graded_filter_switches_on_one_reading(tiny, trend, monkeypatch):
    data, good = trend
    bad = load_mutant("src.strategy.regime", "if streak >= cfg.regime.confirm_weeks:", "if streak >= 1:")
    monkeypatch.setattr(wf, "next_mode", bad.next_mode)
    broken = simulate(data, tiny)
    assert not broken.regime.equals(good.regime)


def test_break_the_refill_ignores_the_position_cap(tiny, monkeypatch):
    """Sized without the 15% cap the first refill puts the whole 85% into one name."""
    bad = load_mutant(
        "src.strategy.rules",
        "cfg.sizing.min_position_weight, cfg.sizing.max_position_weight, budget)",
        "cfg.sizing.min_position_weight, FULL_WEIGHT, budget)")
    data = trend_market()
    good = simulate(data, tiny)
    monkeypatch.setattr(plan_module, "size_positions", bad.size_positions)
    monkeypatch.setattr(plan_module, "size_new_positions", bad.size_new_positions)
    broken = simulate(data, tiny)
    biggest = lambda sim: max(t.value for t in sim.trades if t.side == "BUY")     # noqa: E731
    assert biggest(good) <= 0.15 * 15000.0 + 1e-6
    assert biggest(broken) > 0.15 * 15000.0 * 2


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
    volume. The Apr 1 rebuild picks A; the first decision after that is Fri Apr 5,
    filled Mon Apr 8 (the Mar 29 decision was made before the rebuild). Hindsight
    membership is A throughout, so it buys on Jan 15, once the filter is confirmed."""
    n = 100                                    # Mon 2024-01-01 .. Fri 2024-05-17
    b_vol = [5e4] * 12 + [1e3] * (n - 12)
    return hand_market(n, b_vol=b_vol)


def test_point_in_time_universe_waits_for_the_quarterly_rebuild(tiny):
    one = variant(tiny, universe={"max_symbols": 1})
    pit = simulate(two_stock_market(), one)
    assert [(t.date.strftime("%m-%d"), t.symbol) for t in fills(pit, "BUY")] == [("04-08", "A")]


def test_break_todays_universe_buys_the_stock_before_it_qualified(tiny):
    one = variant(tiny, universe={"max_symbols": 1})
    pit = simulate(two_stock_market(), one)
    hindsight = simulate(two_stock_market(), one, BacktestOptions(point_in_time_universe=False))
    assert fills(hindsight, "BUY")[0].date == pd.Timestamp("2024-01-15")     # nearly three months earlier
    assert hindsight.equity.iloc[-1] > pit.equity.iloc[-1] * 1.02            # and richer
    assert hindsight.equity.iloc[-1] != pit.equity.iloc[-1]


def test_break_same_bar_execution_trades_at_the_signal_close(tiny):
    good = simulate(hand_market(), tiny)
    bad = simulate(hand_market(), tiny, BacktestOptions(same_bar_execution=True))
    b = bad.trades[0]
    c9 = C10 / (1.03 * 1.01) * 1.03                                   # the Jan 12 close
    assert b.date == pd.Timestamp("2024-01-12")                       # the signal bar itself
    assert b.price == pytest.approx(c9, rel=1e-12)                    # C9, not the next open
    assert b.shares == pytest.approx(2250.0 / c9, rel=1e-12)
    assert b.date != good.trades[0].date and b.price != good.trades[0].price
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
        reentry={"sma_days": 3}, universe={**SMALL_UNIVERSE, "adv_window_days": 3})


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
    fill_dates = {dates[d + 1] for d in weekly_schedule(dates) if d + 1 < len(dates)}
    assert {t.date for t in trades} <= fill_dates                      # only ever at a weekly fill

    held: dict[str, float] = {}
    for t in trades:                                                   # replay: at most 10 names, no shorts
        held[t.symbol] = held.get(t.symbol, 0.0) + (t.shares if t.side == "BUY" else -t.shares)
        assert held[t.symbol] > -1e-9
        held = {s: q for s, q in held.items() if q > 1e-9}
        assert len(held) <= rcfg.risk.max_positions
    sells = {t.rule for t in trades if t.side == "SELL"}
    buys = {t.rule for t in trades if t.side == "BUY"}
    assert sells <= {"X1", "X4", "L1", "L2", "L3", "CAP_TRIM", "REGIME_TRIM"}
    assert buys <= {"ENTRY", "REENTRY", "TOPUP", "RESTORE"}
    assert not sells & {"X2", "X3", "DRIFT_TRIM"}                       # retired in v2
    assert all(t.cost > 0 for t in trades)
    assert set(rsim.regime) <= {"normal", "defensive"}


def test_the_ledger_reconciles_on_a_random_market(rsim):
    data = random_market(1)
    cash, shares = 15000.0, {}
    for t in rsim.trades:
        sign = 1 if t.side == "BUY" else -1
        cash += -sign * t.value - t.cost
        shares[t.symbol] = shares.get(t.symbol, 0.0) + sign * t.shares
    last = data.closes.iloc[-1]
    assert cash >= 0                                                    # never spent what it did not have
    assert rsim.equity.iloc[-1] == pytest.approx(
        cash + sum(q * last[s] for s, q in shares.items()), rel=1e-9)


def test_the_risk_dial_has_no_effect_on_the_backtest(rcfg, rsim):
    """LIVE ONLY (spec 8.2): not simulated. Any dial setting gives the same run."""
    other = variant(rcfg, risk_dial={"levels": {"normal": 0.85, "caution": 0.45}})
    again = simulate(random_market(1), other)
    assert again.equity.equals(rsim.equity)


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


def test_the_graded_filter_and_restores_have_no_lookahead(tiny, trend):
    """Cut the trend market at the decisions where the mode changes and around the
    first refill, scramble everything after (here: the same market run backwards),
    and equity and decisions up to the cut must not move."""
    data, _ = trend
    flipped = MarketData(*(f.iloc[::-1].set_axis(f.index) for f in
                           (data.opens, data.highs, data.lows, data.closes, data.volumes)),
                         data.sectors, data.allowed)
    for cut in (9, 29, 34, 44, 49, 54):
        eq_same, dec_same, base, alt = lookahead(lambda d: simulate(d, tiny), data, flipped, cut)
        assert eq_same, f"equity up to row {cut} changed when later prices were scrambled"
        assert dec_same, f"decisions at row {cut} changed when later prices were scrambled"
        assert not base.equity.equals(alt.equity)


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


def with_cagr(target: float, n: int = 1000) -> pd.Series:
    """A curve whose CAGR (calendar days / 365.25) is exactly `target`."""
    idx = pd.bdate_range("2020-01-01", periods=n)
    years = (idx - idx[0]).days / 365.25
    return pd.Series(15000.0 * (1 + target) ** years, index=idx)


def test_a1_needs_spy_plus_two_points_not_merely_spy(cfg):
    """v2: SPY + 2.0 percentage points, because a survivorship-biased universe
    favors a fully invested momentum book. Beating SPY by 1.9 is a FAIL."""
    assert cfg.backtest.acceptance.beat_benchmark_margin == 0.02
    bench = with_cagr(0.10)
    for margin, passes in ((0.019, False), (0.021, True), (0.0, False), (-0.01, False)):
        res = _result_with(cfg)
        res.benchmark_curve, res.equity_curve = bench, with_cagr(0.10 + margin)
        v = check_acceptance(res, cfg, echo=False)
        assert v.results["A1"][0] is passes, margin
        assert v.results["A1"][2] == pytest.approx(0.12)               # the threshold shown is SPY + 2 pts


def test_a1_margin_comes_from_config_and_zero_restores_the_v1_test(cfg):
    zero = variant(cfg, backtest={"acceptance": {"beat_benchmark_margin": 0.0}})
    res = _result_with(zero)
    res.benchmark_curve, res.equity_curve = with_cagr(0.10), with_cagr(0.101)
    assert check_acceptance(res, zero, echo=False).results["A1"][0]


def test_a6_prints_fail_no_positive_excess_return_not_nan(cfg):
    res = _result_with(cfg, excess=(-0.05, -0.02))
    verdict = check_acceptance(res, cfg, echo=False)
    text = verdict.report()
    assert "A6  FAIL  no positive excess return" in text
    assert "nan" not in text.lower()
    empty = _result_with(cfg, excess=())
    assert "A6  FAIL  no positive excess return" in check_acceptance(empty, cfg, echo=False).report()
    fine = _result_with(cfg, excess=(0.1, 0.1, 0.1, 0.1))
    assert "A6  PASS" in check_acceptance(fine, cfg, echo=False).report() and "no positive" not in         check_acceptance(fine, cfg, echo=False).report()


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
                   "No same-bar execution", "Out-of-sample only", "live risk dial is not simulated"):
        assert phrase in text, phrase


def test_the_report_shows_what_the_v2_rules_did(cfg):
    res = _result_with(cfg)
    day = res.equity_curve.index
    def trade(k, side, rule):
        return wf.Trade(day[k], "A", side, 1.0, 1.0, 100.0, 1.0, rule)
    res.trades = ([trade(1, "BUY", "ENTRY")] * 3 + [trade(2, "BUY", "REENTRY")] * 2
                  + [trade(3, "BUY", "TOPUP")] + [trade(3, "BUY", "RESTORE")] * 4
                  + [trade(4, "SELL", "L1")] * 5 + [trade(4, "SELL", "L2")] * 3 + [trade(4, "SELL", "L3")]
                  + [trade(5, "SELL", "CAP_TRIM")] * 7 + [trade(5, "SELL", "X1")] * 2
                  + [trade(5, "SELL", "X4")] + [trade(6, "SELL", "REGIME_TRIM")] * 6)
    res.exposure = pd.Series([0.50, 0.60, 0.70, 0.80] * 250, index=day)
    res.regime = pd.Series(["normal"] * 750 + ["defensive"] * 250, index=day)
    text = format_report(res, check_acceptance(res, cfg, echo=False), cfg)
    for line in (
        "Average invested fraction 65.0%",
        "Time at the 85% target (filter on) 75.0%, at the 40% target (filter off) 25.0%",
        "Sells per ladder level: L1 (-12%) 5, L2 (-20%) 3, L3 (-28%) 1",
        "cap-drift trims 7",
        "X1 (rank) 2, X4 (momentum) 1",
        "Top-ups after partial sales: 1",
        "Restores after defensive trims: 4",
        "Re-entries after full exits: 2",
        "New entries: 3",
    ):
        assert line in text, line
