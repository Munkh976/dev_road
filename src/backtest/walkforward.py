"""
Walk-forward backtest (spec section 12).

The rules this module exists to enforce, because they are what separate a
backtest from a fiction:

  1. Report OUT-OF-SAMPLE results only. In-sample numbers are not evidence.
  2. Costs always applied: commission, slippage, spread.
  3. No look-ahead. Signals at date t use only bars <= t.
  4. Benchmark on the same costs, same dates.
  5. Acceptance criteria are checked automatically and the verdict is
     recorded, so "it looked good enough" is not a judgment call made at the
     end while attached to three weeks of work.

How it works
------------
Signals are computed at the close of the last trading day of each week (a
Friday). Orders decided then fill at the NEXT bar's open. Exits (X1-X4) are
checked every week; entries (E1-E9) only in the first weekly execution of each
month (spec section 9), together with the drift rebalance. The universe for
each date is chosen point-in-time (spec 2.3), rebuilt quarterly like live, not
from today's snapshot.

Nothing is fitted, so the "in-sample" years cannot be tuned on; every parameter
comes from config.yaml and is the same in every window. The strategy is
therefore simulated CONTINUOUSLY from the first date signals exist, and the
out-of-sample windows (one per year, from the first date that is
`in_sample_years` after that) are scored from that one path. Starting every
window flat would charge each year a ramp-up (entries are capped at two a
month, so a book takes about three months to build, and cash earns 0%) that the
strategy running live would pay once. In-sample years are simulated only so the
first scored window starts from a realistic book; they are never reported.

`BacktestOptions` has three switches that make the backtest WRONG on purpose
(same-bar execution, no costs, today's universe). They exist so the tests can
show each one changes the answer. The report prints a banner if any is set.

Known omissions, all repeated in the report: the AI veto (E5) always passes,
cash earns 0%, the risk engine's order-level checks are not applied, and
sectors, security type and index membership are today's (spec 2.3).
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field, replace
from datetime import date

import numpy as np
import pandas as pd

from src.config import REPO_ROOT, Config, load_config
from src.data.cache import DAYS_PER_YEAR, PriceCache
from src.data.universe import (
    UniverseError,
    load_constituents,
    load_contract_info,
    open_db,
    point_in_time_universe,
    quarterly_universe,
    todays_universe,
)
from src.runlog import finish_run, start_run
from src.strategy.rules import evaluate_entries, evaluate_exits, size_new_positions
from src.strategy.signals import PRIOR_BAR, compute_panel

log = logging.getLogger(__name__)

# Unit conversions and identities, not tunables.
BPS_PER_UNIT = 10_000.0
HALF_SPREAD = 0.5          # the quoted spread is crossed one way per trade
MONTHS_PER_YEAR = 12
NEXT_BAR = 1
ZERO = 0.0
REPORTS_DIR = REPO_ROOT / "reports"


class BacktestError(RuntimeError):
    """The backtest cannot be run as specified. Never swallowed."""


# ---------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class MarketData:
    """Aligned dates x symbols price panels, benchmark column included.

    Gaps are NaN and stay NaN (never forward-filled). The benchmark's bars
    define the trading calendar, so it must have none missing.
    """

    opens: pd.DataFrame
    highs: pd.DataFrame
    lows: pd.DataFrame
    closes: pd.DataFrame
    volumes: pd.DataFrame
    sectors: dict[str, str | None] = field(default_factory=dict)   # today's labels
    allowed: frozenset[str] | None = None    # security type passes; None = no filter

    def validate(self, benchmark: str) -> None:
        idx = self.closes.index
        if not (idx.is_monotonic_increasing and idx.is_unique):
            raise BacktestError("price panel index must be strictly increasing dates")
        for name in ("opens", "highs", "lows", "volumes"):
            frame = getattr(self, name)
            if not (frame.index.equals(idx) and frame.columns.equals(self.closes.columns)):
                raise BacktestError(f"{name} is not aligned with closes")
        if benchmark not in self.closes.columns:
            raise BacktestError(f"benchmark {benchmark} is missing from the price panel")
        if self.closes[benchmark].isna().any():
            raise BacktestError(f"{benchmark} has missing bars; it defines the calendar")


@dataclass(frozen=True)
class BacktestOptions:
    """Switches that make the backtest wrong, for the tests only. See module doc."""

    same_bar_execution: bool = False        # trade at the close the signal used
    apply_costs: bool = True
    point_in_time_universe: bool = True     # False: today's universe, in hindsight

    @property
    def is_valid_run(self) -> bool:
        return self == BacktestOptions()


# --------------------------------------------------------------------- results


@dataclass
class WindowResult:
    """One out-of-sample window."""

    in_sample_start: date
    in_sample_end: date
    oos_start: date
    oos_end: date
    strategy_return: float
    benchmark_return: float
    excess_return: float
    max_drawdown: float
    sharpe: float
    turnover: float
    n_trades: int
    partial: bool = False       # the last window can be shorter than a year


@dataclass(frozen=True)
class Trade:
    date: pd.Timestamp
    symbol: str
    side: str            # BUY | SELL
    shares: float
    price: float
    value: float
    cost: float
    rule: str            # X1..X4 for exits; ENTRY, DRIFT_ADD, DRIFT_TRIM otherwise


@dataclass
class Simulation:
    equity: pd.Series                   # close-of-day equity from the first signal date
    exposure: pd.Series                 # invested fraction of equity
    trades: list[Trade]
    first_signal: pd.Timestamp
    stats: dict[str, int]


@dataclass
class BacktestResult:
    windows: list[WindowResult] = field(default_factory=list)
    equity_curve: pd.Series | None = None      # out-of-sample only, from starting capital
    benchmark_curve: pd.Series | None = None   # SPY buy-and-hold, same dates and costs
    trades: list[Trade] = field(default_factory=list)     # out-of-sample trades
    options: BacktestOptions = field(default_factory=BacktestOptions)
    periods_per_year: int = 0
    notes: list[str] = field(default_factory=list)
    exposure: pd.Series | None = None

    @property
    def oos_cagr(self) -> float:
        return _cagr(self.equity_curve)

    @property
    def benchmark_cagr(self) -> float:
        return _cagr(self.benchmark_curve)

    @property
    def max_drawdown(self) -> float:
        return _max_drawdown(self.equity_curve)

    @property
    def sharpe(self) -> float:
        return _sharpe(self.equity_curve, self.periods_per_year)

    @property
    def annual_turnover(self) -> float:
        return _turnover(self.equity_curve, self.trades)

    @property
    def worst_rolling_12m(self) -> float:
        curve = self.equity_curve
        if curve is None or len(curve) <= self.periods_per_year:
            return float("nan")
        return float((curve / curve.shift(self.periods_per_year) - 1).min())

    @property
    def total_excess(self) -> float:
        return float(sum(w.excess_return for w in self.windows))

    @property
    def max_single_window_contribution(self) -> float:
        """Share of total excess return from the single best window.

        Acceptance criterion A6, and the one most people skip. A strategy
        whose entire edge came from one lucky year is not a strategy.

        NaN when total excess return is not positive: there is no edge to
        apportion, and NaN fails A6 (fail closed).
        """
        total = self.total_excess
        if not self.windows or not total > ZERO:
            return float("nan")
        return max(w.excess_return for w in self.windows) / total


@dataclass
class AcceptanceVerdict:
    passed: bool
    results: dict[str, tuple[bool, float, float]]  # name -> (pass, actual, threshold)
    reason: str | None = None    # why the verdict is FAIL regardless of the numbers

    LABELS = {
        "A1": "CAGR vs SPY, after costs (actual: strategy; threshold: SPY)",
        "A2": "Max drawdown (must be below)",
        "A3": "Worst rolling 12 months (must be above)",
        "A4": "Sharpe ratio (must be above)",
        "A5": "Annual turnover (must be below)",
        "A6": "Largest window share of excess return (must not exceed)",
    }

    def report(self) -> str:
        lines = []
        for code, (ok, actual, threshold) in self.results.items():
            lines.append(f"  {code}  {'PASS' if ok else 'FAIL'}  {self.LABELS[code]}: "
                         f"{actual:.4f} vs {threshold:.4f}")
        lines.append(f"  VERDICT: {'PASS' if self.passed else 'FAIL'}"
                     + (f" ({self.reason})" if self.reason else ""))
        if not self.results["A1"][0]:
            lines.append("  A1 failed: stop. Buy SPY and keep the hour (spec section 13).")
        return "\n".join(lines)


# ------------------------------------------------------------------ statistics


def _cagr(curve: pd.Series | None) -> float:
    if curve is None or len(curve) < 2:
        return float("nan")
    years = (curve.index[-1] - curve.index[0]).days / DAYS_PER_YEAR
    if not years > ZERO:
        return float("nan")
    return float((curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1)


def _max_drawdown(curve: pd.Series | None) -> float:
    """Largest peak-to-trough fall, as a positive fraction."""
    if curve is None or curve.empty:
        return float("nan")
    return float(-(curve / curve.cummax() - 1).min())


def _sharpe(curve: pd.Series | None, periods_per_year: int) -> float:
    """Annualized Sharpe of daily returns with a zero risk-free rate. Cash earns
    0% in this backtest, so the risk-free leg is zero for both sides."""
    if curve is None or len(curve) < 3:
        return float("nan")
    r = (curve / curve.shift(PRIOR_BAR) - 1).dropna()
    sd = r.std()
    if not sd > ZERO:
        return float("nan")
    return float(r.mean() / sd * np.sqrt(periods_per_year))


def _turnover(curve: pd.Series | None, trades: list[Trade]) -> float:
    """Annualized turnover: half of the traded value (buys plus sells), over
    average equity. Replacing the whole book once is 100%."""
    if curve is None or len(curve) < 2:
        return float("nan")
    years = (curve.index[-1] - curve.index[0]).days / DAYS_PER_YEAR
    if not years > ZERO:
        return float("nan")
    traded = sum(abs(t.value) for t in trades if t.date > curve.index[0])
    return float(traded * HALF_SPREAD / curve.mean() / years)


# ----------------------------------------------------------------------- costs


def apply_costs(
    trade_value: float, shares: float, cfg: Config, is_entry: bool
) -> float:
    """Commission + slippage + half-spread on one side of a trade.

    Be pessimistic here. An optimistic cost model is the most common way a
    backtest that "works" fails in live trading.

    Dollars, to be deducted from cash. `is_entry` is accepted for the caller's
    symmetry: a buy and a sell pay the same. The spread is charged at half the
    configured quote per side (a round trip crosses it once).
    """
    c = cfg.backtest.costs
    commission = max(abs(shares) * c.commission_per_share, c.commission_min)
    market = abs(trade_value) * (c.slippage_bps + c.spread_bps * HALF_SPREAD) / BPS_PER_UNIT
    return commission + market


def max_affordable_shares(cash: float, price: float, cfg: Config, costs_on: bool = True) -> float:
    """Most shares whose value plus costs fits in `cash`. Never spends more than
    it has: this is what keeps the backtest unlevered."""
    if not (cash > ZERO and price > ZERO):
        return ZERO
    if not costs_on:
        return cash / price
    c = cfg.backtest.costs
    per_share = price * (1 + (c.slippage_bps + c.spread_bps * HALF_SPREAD) / BPS_PER_UNIT)
    minimum_case = (cash - c.commission_min) / per_share            # commission = the minimum
    if minimum_case <= ZERO:
        return ZERO                     # cannot even pay the minimum commission
    if minimum_case * c.commission_per_share <= c.commission_min:
        return minimum_case
    return cash / (per_share + c.commission_per_share)              # commission per share


# -------------------------------------------------------------------- schedule


def weekly_schedule(dates: pd.DatetimeIndex) -> tuple[list[int], set[int]]:
    """(decision rows, entry-decision rows).

    A decision row is the last trading day of each ISO week; its orders fill on
    the next row. It is an entry decision when that fill is the first weekly
    fill of its calendar month, i.e. "the first Monday" (or the first trading
    day of the first trading week). Uses the calendar only, never prices.
    """
    n = len(dates)
    iso = dates.isocalendar()
    rows = pd.Series(np.arange(n), index=dates)
    decisions = sorted(rows.groupby([iso["year"].to_numpy(), iso["week"].to_numpy()]).max())
    fills = [d + NEXT_BAR for d in decisions if d + NEXT_BAR < n]
    fill_dates = dates[fills]
    first_of_month = pd.Series(fills, index=fill_dates).groupby(
        [fill_dates.year, fill_dates.month]).min()
    return decisions, {f - NEXT_BAR for f in first_of_month}


# ------------------------------------------------------------------- simulation


@dataclass
class _Order:
    symbol: str
    kind: str            # EXIT | BUY | ADJUST
    rule: str
    dollars: float = ZERO      # BUY: target value. ADJUST: signed change.


class _Engine:
    """One simulation. State is cash, shares, position highs and last marks."""

    def __init__(self, data: MarketData, cfg: Config, options: BacktestOptions) -> None:
        self.cfg, self.opt, self.data = cfg, options, data
        bench = cfg.universe.benchmark
        data.validate(bench)
        self.dates = data.closes.index
        self.n = len(self.dates)
        self.col = {c: j for j, c in enumerate(data.closes.columns)}
        self.open, self.close = data.opens.to_numpy(), data.closes.to_numpy()

        if options.point_in_time_universe:
            universe = quarterly_universe(
                point_in_time_universe(data.closes, data.volumes, cfg, data.allowed))
        else:
            universe = todays_universe(data.closes, data.volumes, cfg, data.allowed)
        self.panel = compute_panel(data.closes, data.highs, data.lows, cfg, universe)
        self.returns = self.panel.closes / self.panel.closes.shift(PRIOR_BAR) - 1

        self.decisions, self.entry_decisions = weekly_schedule(self.dates)
        self.decision_set = set(self.decisions)

        self.cash = float(cfg.account.capital)
        self.shares: dict[str, float] = {}
        self.highs: dict[str, float] = {}
        self.mark: dict[str, float] = {}
        self.trades: list[Trade] = []
        self.stats = {"unfilled_orders": 0, "sizing_failures": 0, "e7_drops": 0}

        valid = self.panel.rank.notna().any(axis=1) & self.panel.benchmark_sma.notna()
        if not valid.any():
            raise BacktestError("no date has valid signals; not enough history")
        self.first_signal = valid.idxmax()

    # ------------------------------------------------------------- state

    def _fill_price(self, j: int, i: int) -> float:
        arr = self.close if self.opt.same_bar_execution else self.open
        return arr[i, j]

    def _cost(self, value: float, shares: float) -> float:
        return apply_costs(value, shares, self.cfg, is_entry=False) if self.opt.apply_costs else ZERO

    def _update_marks(self, i: int) -> None:
        for sym in self.shares:
            c = self.close[i, self.col[sym]]
            if not np.isnan(c):
                self.mark[sym] = c
            # fmax ignores NaN: the high is the highest CLOSE since entry.
            self.highs[sym] = float(np.fmax(self.highs[sym], c))

    def _equity(self) -> float:
        return self.cash + sum(q * self.mark[s] for s, q in self.shares.items())

    # -------------------------------------------------------- decisions

    def _decide(self, i: int) -> list[_Order]:
        t = self.dates[i]
        sf = self.panel.at(t)
        atr = {s: self.panel.atr_20.at[t, s] for s in self.shares if s in self.panel.atr_20.columns}
        exits = evaluate_exits(dict(self.shares), sf, self.highs, atr, self.cfg)
        sold = {d.symbol: d.rule for d in exits if d.should_exit}
        orders = [_Order(s, "EXIT", rule) for s, rule in sold.items()]
        remaining = [s for s in self.shares if s not in sold]
        if i in self.entry_decisions:
            orders += self._entries(i, sf, remaining, sold)
        return orders

    def _entries(self, i, sf, remaining: list[str], sold: dict[str, str]) -> list[_Order]:
        cfg, t = self.cfg, self.dates[i]
        equity = self._equity()
        sector_w: dict[str | None, float] = {}
        for s in remaining:
            sec = self.data.sectors.get(s)
            sector_w[sec] = sector_w.get(sec, ZERO) + self.shares[s] * self.mark[s] / equity

        # A name stopped out this week is not a candidate this week: buying it
        # back at the same open would be a stop that does nothing.
        candidates = replace(sf, table=sf.table.drop(index=list(sold), errors="ignore"))
        entries = evaluate_entries(
            candidates, set(remaining), sector_w, {}, cfg, sectors=self.data.sectors)
        new = [d.symbol for d in entries if d.eligible]
        book = remaining + new
        if not book:
            return []
        try:
            targets, dropped = size_new_positions(
                book, new, sf.table["vol_63"], self.returns.loc[:t], equity, cfg)
        except ValueError as exc:
            # Fail closed: an unsizable book gets no orders this week.
            log.warning("%s: sizing failed, no entries or rebalance (%s)", t.date(), exc)
            self.stats["sizing_failures"] += 1
            return []
        self.stats["e7_drops"] += len(dropped)

        orders = [_Order(s, "BUY", "ENTRY", float(targets[s])) for s in new if s in targets.index]
        for s in remaining:
            current, target = self.shares[s] * self.mark[s], float(targets[s])
            if target > ZERO and abs(current / target - 1) > cfg.schedule.drift_tolerance:
                orders.append(_Order(s, "ADJUST", "DRIFT", target - current))
        return orders

    # -------------------------------------------------------- execution

    def _execute(self, orders: list[_Order], i: int) -> None:
        """Sells first (they fund the buys), then buys in the order given."""
        for o in orders:
            if o.kind == "EXIT":
                self._sell(o.symbol, None, i, o.rule)
        for o in orders:
            if o.kind == "ADJUST" and o.dollars < ZERO and o.symbol in self.shares:
                price = self._fill_price(self.col[o.symbol], i)
                if not np.isnan(price):
                    self._sell(o.symbol, min(self.shares[o.symbol], -o.dollars / price),
                               i, "DRIFT_TRIM")
                else:
                    self.stats["unfilled_orders"] += 1
        for o in orders:
            if o.kind == "BUY":
                self._buy(o.symbol, o.dollars, i, "ENTRY")
            elif o.kind == "ADJUST" and o.dollars > ZERO and o.symbol in self.shares:
                self._buy(o.symbol, o.dollars, i, "DRIFT_ADD")

    def _sell(self, sym: str, qty: float | None, i: int, rule: str) -> None:
        price = self._fill_price(self.col[sym], i)
        if np.isnan(price):            # no bar to trade on (halted): retry next week
            self.stats["unfilled_orders"] += 1
            return
        qty = self.shares[sym] if qty is None else qty
        value = qty * price
        cost = self._cost(value, qty)
        self.cash += value - cost
        left = self.shares[sym] - qty
        if left > ZERO:
            self.shares[sym] = left
        else:
            for book in (self.shares, self.highs, self.mark):
                book.pop(sym, None)
        self.trades.append(Trade(self.dates[i], sym, "SELL", qty, price, value, cost, rule))

    def _buy(self, sym: str, dollars: float, i: int, rule: str) -> None:
        price = self._fill_price(self.col[sym], i)
        if np.isnan(price):
            self.stats["unfilled_orders"] += 1
            return
        qty = min(dollars / price, max_affordable_shares(self.cash, price, self.cfg, self.opt.apply_costs))
        if not qty > ZERO:
            return
        value = qty * price
        cost = self._cost(value, qty)
        self.cash -= value + cost
        if sym not in self.shares:
            self.shares[sym], self.highs[sym], self.mark[sym] = ZERO, float("nan"), price
        self.shares[sym] += qty
        self.trades.append(Trade(self.dates[i], sym, "BUY", qty, price, value, cost, rule))

    # -------------------------------------------------------------- run

    def run(self, start: pd.Timestamp | None = None) -> Simulation:
        first = self.dates.get_loc(self.first_signal if start is None else start)
        pending: list[_Order] | None = None
        equity: dict[pd.Timestamp, float] = {}
        exposure: dict[pd.Timestamp, float] = {}
        for i in range(first, self.n):
            if pending:
                self._execute(pending, i)         # fills at this bar's open
            pending = None
            self._update_marks(i)
            if i in self.decision_set:
                orders = self._decide(i)          # sees rows <= i only
                if self.opt.same_bar_execution:
                    self._execute(orders, i)      # the wrong way: at the signal bar's close
                    self._update_marks(i)
                elif i + NEXT_BAR < self.n:
                    pending = orders
            total = self._equity()
            equity[self.dates[i]] = total
            exposure[self.dates[i]] = (total - self.cash) / total
        return Simulation(
            pd.Series(equity), pd.Series(exposure), self.trades, self.first_signal, self.stats)


def simulate(
    data: MarketData, cfg: Config, options: BacktestOptions | None = None,
    start: pd.Timestamp | None = None,
) -> Simulation:
    """One continuous run from the first date signals exist (or `start`)."""
    return _Engine(data, cfg, options or BacktestOptions()).run(start)


def buy_and_hold(
    data: MarketData, cfg: Config, options: BacktestOptions, first_row: int
) -> pd.Series:
    """SPY bought with all starting capital at the first out-of-sample fill and
    held, paying the same costs. The point before the purchase is the starting
    capital, so returns are measured from the same base as the strategy's."""
    bench = data.closes.columns.get_loc(cfg.universe.benchmark)
    if options.same_bar_execution:
        price = data.closes.iloc[first_row - NEXT_BAR, bench]
    else:
        price = data.opens.iloc[first_row, bench]
    capital = float(cfg.account.capital)
    qty = max_affordable_shares(capital, price, cfg, options.apply_costs)
    cash = capital - qty * price - (
        apply_costs(qty * price, qty, cfg, True) if options.apply_costs else ZERO)
    closes = data.closes.iloc[first_row - NEXT_BAR:, bench]
    curve = cash + qty * closes
    curve.iloc[0] = capital
    curve.name = None
    return curve


# -------------------------------------------------------------- walk-forward


@dataclass(frozen=True)
class OosWindow:
    start_row: int
    end_row: int          # inclusive
    partial: bool


def oos_windows(
    dates: pd.DatetimeIndex, first_signal: pd.Timestamp, cfg: Config
) -> list[OosWindow]:
    """One out-of-sample window per year, from `in_sample_years` after the
    first date signals exist.

    Anchored to the START of the data, not the end: trimming the front to
    make the last window complete would drop 2008. A last window shorter than
    a year is kept and flagged. `backtest.start` is the earliest an
    out-of-sample window may begin; it is a check on the protocol (spec 12),
    not a way to move the period.
    """
    wf = cfg.backtest.walk_forward
    length = wf.out_of_sample_years * MONTHS_PER_YEAR
    if wf.step_months != length:
        raise BacktestError(
            f"step_months ({wf.step_months}) must equal out_of_sample_years x 12 "
            f"({length}): overlapping or gapped windows would double count or hide returns")
    earliest = pd.Timestamp(cfg.backtest.start)
    first_target = first_signal + pd.DateOffset(years=wf.in_sample_years)
    out: list[OosWindow] = []
    k = 0
    while True:
        start_t = first_target + pd.DateOffset(months=k * wf.step_months)
        end_t = start_t + pd.DateOffset(months=length)
        k += 1
        if start_t > dates[-1]:
            break
        a = int(dates.searchsorted(start_t))
        if dates[a] < earliest:
            continue
        complete = end_t <= dates[-1]
        b = int(dates.searchsorted(end_t)) - 1 if complete else len(dates) - 1
        out.append(OosWindow(a, b, not complete))
    return out


def _window_result(
    w: OosWindow, sim: Simulation, bench: pd.Series, dates: pd.DatetimeIndex, cfg: Config,
    periods_per_year: int,
) -> WindowResult:
    base, end = dates[w.start_row - NEXT_BAR], dates[w.end_row]
    strat, ref = sim.equity.loc[base:end], bench.loc[base:end]
    s_ret, b_ret = strat.iloc[-1] / strat.iloc[0] - 1, ref.iloc[-1] / ref.iloc[0] - 1
    in_trades = [t for t in sim.trades if base < t.date <= end]
    return WindowResult(
        in_sample_start=(dates[w.start_row]
                         - pd.DateOffset(years=cfg.backtest.walk_forward.in_sample_years)).date(),
        in_sample_end=base.date(),
        oos_start=dates[w.start_row].date(), oos_end=end.date(),
        strategy_return=float(s_ret), benchmark_return=float(b_ret),
        excess_return=float(s_ret - b_ret),
        max_drawdown=_max_drawdown(strat), sharpe=_sharpe(strat, periods_per_year),
        turnover=_turnover(strat, in_trades), n_trades=len(in_trades), partial=w.partial,
    )


def run_walk_forward(
    data: MarketData, cfg: Config, options: BacktestOptions | None = None
) -> BacktestResult:
    options = options or BacktestOptions()
    engine = _Engine(data, cfg, options)
    sim = engine.run()
    dates = data.closes.index
    windows = oos_windows(dates, sim.first_signal, cfg)
    if not windows:
        raise BacktestError(
            f"not enough history for one out-of-sample window: signals start "
            f"{sim.first_signal.date()}, need {cfg.backtest.walk_forward.in_sample_years} "
            f"years more, data ends {dates[-1].date()}")

    ppy = cfg.signals.volatility.annualize
    first_row, last_row = windows[0].start_row, windows[-1].end_row
    bench = buy_and_hold(data, cfg, options, first_row)
    base = dates[first_row - NEXT_BAR]
    span = dates[first_row - NEXT_BAR: last_row + NEXT_BAR]
    curve = sim.equity.loc[span] / sim.equity.loc[base] * cfg.account.capital

    result = BacktestResult(
        windows=[_window_result(w, sim, bench, dates, cfg, ppy)
                 for w in windows],
        equity_curve=curve, benchmark_curve=bench.loc[span],
        trades=[t for t in sim.trades if t.date > base],
        options=options, periods_per_year=ppy, exposure=sim.exposure.loc[span],
    )
    unknown = [c for c in data.closes.columns
               if c != cfg.universe.benchmark and data.sectors.get(c) is None]
    result.notes = [
        f"signals start {sim.first_signal.date()}; first out-of-sample window "
        f"{dates[windows[0].start_row].date()}",
        f"{sim.stats['unfilled_orders']} orders had no bar to fill on; "
        f"{sim.stats['sizing_failures']} weeks had an unsizable book; "
        f"{sim.stats['e7_drops']} entries dropped by E7",
        f"{len(unknown)} symbols have no sector and can never pass E8",
    ]
    return result


def load_market_data(cfg: Config) -> MarketData:
    """Every cached constituent plus the benchmark, aligned to the benchmark's
    calendar. Sector and security type are today's contract details (spec 2.3)."""
    cache = PriceCache(cfg.cache_path)
    bench = cfg.universe.benchmark
    symbols = [bench] + [c.symbol for c in load_constituents(cfg) if c.symbol != bench]
    frames: dict[str, dict[str, pd.Series]] = {f: {} for f in
                                               ("open", "high", "low", "close", "volume")}
    for sym in symbols:
        df = cache.read(sym)
        if df is None or df.empty:
            continue
        for f in frames:
            frames[f][sym] = df[f]
    if bench not in frames["close"]:
        raise UniverseError(f"{bench} is not cached; run refresh first")
    calendar = frames["close"][bench].dropna().index
    panels = {f: pd.DataFrame(s).reindex(calendar).sort_index() for f, s in frames.items()}

    conn = open_db(cfg)
    try:
        infos = load_contract_info(conn)
    finally:
        conn.close()
    types = {t.upper() for t in cfg.universe.security_types}
    allowed = frozenset(
        s for s, i in infos.items() if (i.stock_type or "").upper() in types)
    return MarketData(
        panels["open"], panels["high"], panels["low"], panels["close"], panels["volume"],
        sectors={s: i.sector for s, i in infos.items()}, allowed=allowed)


def walk_forward(
    cfg: Config, data: MarketData | None = None, options: BacktestOptions | None = None
) -> BacktestResult:
    """Roll in-sample/out-of-sample windows forward through the full period.

    Period must include 2008, 2020 and 2022. A backtest starting in 2010 has
    never seen a real bear market.

    Reads the parquet cache unless `data` is given (tests pass synthetic
    markets). Parameters are read from `cfg` and are the same in every window.
    """
    return run_walk_forward(data if data is not None else load_market_data(cfg), cfg, options)


# ------------------------------------------------------------------ acceptance


def check_acceptance(result: BacktestResult, cfg: Config, echo: bool = True) -> AcceptanceVerdict:
    """A1-A6 from spec section 13.

    If A1 fails, the honest move is to stop and buy the index. That decision
    is pre-committed here rather than made later while attached to the build.

    A run made with any `BacktestOptions` switch set is FAIL, "invalid run",
    whatever its numbers say: those switches make the backtest wrong on purpose.

    Every comparison is written so that NaN fails: a criterion that cannot be
    computed is not passed. Prints PASS/FAIL per criterion unless `echo` is False.
    """
    a = cfg.backtest.acceptance
    cagr, bench = result.oos_cagr, result.benchmark_cagr
    checks = {
        "A1": (cagr > bench if a.beat_benchmark else True, cagr, bench),
        "A2": (result.max_drawdown < a.max_drawdown, result.max_drawdown, a.max_drawdown),
        "A3": (result.worst_rolling_12m > a.worst_rolling_12m,
               result.worst_rolling_12m, a.worst_rolling_12m),
        "A4": (result.sharpe > a.min_sharpe, result.sharpe, a.min_sharpe),
        "A5": (result.annual_turnover < a.max_annual_turnover,
               result.annual_turnover, a.max_annual_turnover),
        "A6": (result.max_single_window_contribution <= a.max_single_window_contribution,
               result.max_single_window_contribution, a.max_single_window_contribution),
    }
    results = {k: (bool(ok), float(actual), float(limit)) for k, (ok, actual, limit) in checks.items()}
    invalid = not result.options.is_valid_run
    verdict = AcceptanceVerdict(
        all(ok for ok, _, _ in results.values()) and not invalid, results,
        "invalid run" if invalid else None)
    if echo:
        print(verdict.report())
    return verdict


# ---------------------------------------------------------------------- report


def format_report(result: BacktestResult, verdict: AcceptanceVerdict, cfg: Config) -> str:
    out = [f"# Backtest: {cfg.strategy.name} v{cfg.strategy.version}", ""]
    if not result.options.is_valid_run:
        out += [f"**INVALID RUN: {result.options}. These switches make the backtest wrong "
                "on purpose. Do not read the numbers.**", ""]
    c = result.equity_curve
    out += [
        f"Out-of-sample only: {c.index[0].date()} to {c.index[-1].date()}, "
        f"{len(result.windows)} windows. Starting capital ${cfg.account.capital:,.0f}.",
        "", "## Acceptance (spec section 13)", "", "```", verdict.report(), "```", "",
        "## Results", "",
        f"- Strategy CAGR {result.oos_cagr:.2%}, SPY buy-and-hold {result.benchmark_cagr:.2%}",
        f"- Max drawdown {result.max_drawdown:.2%}, worst rolling 12m {result.worst_rolling_12m:.2%}",
        f"- Sharpe {result.sharpe:.2f}, annual turnover {result.annual_turnover:.0%}",
        f"- Average invested fraction {result.exposure.mean():.0%}",
        f"- Final equity ${c.iloc[-1]:,.0f} vs SPY ${result.benchmark_curve.iloc[-1]:,.0f}",
        "", "## Windows", "",
        "| OOS start | OOS end | Strategy | SPY | Excess | Max DD | Sharpe | Turnover | Trades |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for w in result.windows:
        tag = " (partial)" if w.partial else ""
        out.append(f"| {w.oos_start} | {w.oos_end}{tag} | {w.strategy_return:.1%} | "
                   f"{w.benchmark_return:.1%} | {w.excess_return:+.1%} | {w.max_drawdown:.1%} | "
                   f"{w.sharpe:.2f} | {w.turnover:.0%} | {w.n_trades} |")
    exits: dict[str, int] = {}
    for t in result.trades:
        if t.side == "SELL":
            exits[t.rule] = exits.get(t.rule, 0) + 1
    out += ["", f"Sells by rule: {exits or 'none'}", "", "## What this backtest does not tell you", "",
            "- **E5 (AI veto) always passes.** News as of a past date cannot be reproduced.",
            "- **Cash earns 0%.** This slightly understates the strategy in risk-off periods.",
            "- **Costs** per trade: $%.3f/share (min $%.2f), %g bps slippage, %g bps spread "
            "(half crossed per side)." % (
                cfg.backtest.costs.commission_per_share, cfg.backtest.costs.commission_min,
                cfg.backtest.costs.slippage_bps, cfg.backtest.costs.spread_bps),
            "- **Signals at the close, fills at the next open.** No same-bar execution.",
            "- **Universe is point-in-time** (top %d by 20-day dollar volume among current "
            "constituents, spec 2.3). Still biased: today's S&P 500 membership list "
            "(survivorship), and sectors and security type taken from today's labels."
            % cfg.universe.max_symbols,
            "- **Risk engine order-level checks are not applied** (order caps, drawdown halt).",
            "- **In-sample years are warm-up only.** Nothing is fitted; parameters are "
            "identical in every window.",
            ""] + [f"- {n}" for n in result.notes]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Walk-forward backtest, out-of-sample only.").parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    conn = open_db(cfg)
    run_id = start_run(conn, cfg, "backtest", "walk-forward, out-of-sample only")
    try:
        result = walk_forward(cfg)
        verdict = check_acceptance(result, cfg, echo=False)
        if verdict.reason:
            # Not a result. No report file, and the run is recorded as failed.
            log.error("%s: not recording this as a result", verdict.reason)
            finish_run(conn, run_id, "failed", error=verdict.reason)
            return 3
        text = format_report(result, verdict, cfg)
        print(text)
        REPORTS_DIR.mkdir(exist_ok=True)
        path = REPORTS_DIR / f"backtest_{date.today().isoformat()}.md"
        path.write_text(text, encoding="utf-8")
        log.info("report written to %s", path)
        finish_run(conn, run_id, "ok",
                   notes=f"acceptance {'PASS' if verdict.passed else 'FAIL'}: "
                         + ", ".join(f"{k}={'PASS' if v[0] else 'FAIL'}"
                                     for k, v in verdict.results.items()))
        return 0 if verdict.passed else 1
    except (BacktestError, UniverseError) as exc:
        log.error("%s", exc)
        finish_run(conn, run_id, "failed", error=str(exc))
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
