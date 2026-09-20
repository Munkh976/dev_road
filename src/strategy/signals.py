"""
Signal computation. Pure functions over a price matrix — no IO, no broker,
no database. That is what makes them testable and identical in backtest and
live paths.

Implements spec section 4. Every parameter comes from config; there are no
literals in this module by design.

Two layers:

* Panel functions (`blended_momentum`, `realized_volatility`,
  `average_true_range`, `market_regime`, `compute_panel`) work on the whole
  dates x symbols matrix at once. The backtest needs a signal for every date,
  and computing them in one vectorised pass is both faster and the reason
  look-ahead can be tested by truncation.
* `compute()` is one date: it cuts the inputs off at `as_of` *before*
  computing, so it cannot see a later bar even by mistake.

Every signal at row t is a function of rows <= t. Rolling windows are
trailing, shifts are positive, and no window is centered. NaN propagates:
a missing bar is never forward-filled (a filled bar for a halted name is a
price that never traded), it makes every signal whose window touches it NaN,
and a NaN score is left out of the ranking.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.config import Config

# One row back: the prior close for true range and for daily returns.
PRIOR_BAR = 1


@dataclass(frozen=True)
class SignalFrame:
    """Signals for one date across the universe."""

    signal_date: pd.Timestamp
    market_on: bool
    table: pd.DataFrame  # index=symbol, cols: close, mom_6m, mom_12m,
                         # blended_momentum, vol_63, score, rank, atr_20
    benchmark_close: float | None = None   # what market_on was decided from,
    benchmark_sma: float | None = None     # kept so the report can show it

    def top(self, n: int) -> list[str]:
        """The n best-ranked symbols. Unranked symbols (rank NaN) are never
        included, even when fewer than n are ranked: `nsmallest` would pad the
        list with them."""
        return self.table["rank"].dropna().nsmallest(n).index.tolist()

    def rank_of(self, symbol: str) -> int | None:
        """None if the symbol is not in the table or was not ranked (too little
        history, or a missing bar on this date)."""
        if symbol not in self.table.index:
            return None
        rank = self.table.loc[symbol, "rank"]
        return None if pd.isna(rank) else int(rank)


@dataclass(frozen=True)
class SignalPanel:
    """Every signal for every date. Row t only ever depends on rows <= t."""

    closes: pd.DataFrame
    mom_6m: pd.DataFrame
    mom_12m: pd.DataFrame
    blended_momentum: pd.DataFrame
    vol_63: pd.DataFrame
    score: pd.DataFrame
    rank: pd.DataFrame           # float with NaN; 1 = best, unranked = NaN
    atr_20: pd.DataFrame
    in_universe: pd.DataFrame    # bool: was the symbol in force on that date
    market_on: pd.Series         # bool, False wherever it cannot be evaluated
    benchmark_close: pd.Series
    benchmark_sma: pd.Series

    def at(self, when: pd.Timestamp) -> SignalFrame:
        ts = pd.Timestamp(when)
        if ts not in self.closes.index:
            raise KeyError(f"{ts.date()} is not a date in the price panel")
        close = self.closes.loc[ts]
        table = pd.DataFrame({
            "close": close,
            "mom_6m": self.mom_6m.loc[ts],
            "mom_12m": self.mom_12m.loc[ts],
            "blended_momentum": self.blended_momentum.loc[ts],
            "vol_63": self.vol_63.loc[ts],
            "score": self.score.loc[ts],
            "rank": self.rank.loc[ts].astype("Int64"),
            "atr_20": self.atr_20.loc[ts],
        })
        table.index.name = "symbol"
        # A symbol with no bar today has no price to trade at, so no row.
        table = table[self.in_universe.loc[ts] & close.notna()]
        bench_close = self.benchmark_close.loc[ts]
        bench_sma = self.benchmark_sma.loc[ts]
        return SignalFrame(
            signal_date=ts,
            market_on=bool(self.market_on.loc[ts]),
            table=table,
            benchmark_close=None if pd.isna(bench_close) else float(bench_close),
            benchmark_sma=None if pd.isna(bench_sma) else float(bench_sma),
        )


# ------------------------------------------------------------------ signals


def momentum_components(
    closes: pd.DataFrame, cfg: Config
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(mom_6m, mom_12m): P[t-skip] / P[t-lookback] - 1.

    Shifts count rows of the panel, i.e. trading days of the market calendar.
    A symbol with a missing bar exactly at either end of the window gets NaN.
    """
    m = cfg.signals.momentum
    recent = closes.shift(m.skip_days)
    return (
        recent / closes.shift(m.lookback_short) - 1,
        recent / closes.shift(m.lookback_long) - 1,
    )


def blended_momentum(closes: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """P[t-skip]/P[t-lookback] - 1, blended across two lookbacks.

    The 21-day skip avoids short-term reversal, which is documented and
    opposite-signed at the one-month horizon (spec section 4).

    Returns a DataFrame aligned to `closes`, NaN where history is short.
    """
    m = cfg.signals.momentum
    short, long_ = momentum_components(closes, cfg)
    return m.weight_short * short + m.weight_long * long_


def realized_volatility(closes: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Annualized stdev of daily returns over the configured window.

    Sample standard deviation (ddof=1, the pandas default). Returns are
    computed explicitly rather than with `pct_change`, whose default
    forward-fills gaps before differencing. The window must be completely
    populated: one missing bar inside it makes the result NaN.
    """
    v = cfg.signals.volatility
    returns = closes / closes.shift(PRIOR_BAR) - 1
    return returns.rolling(v.window, min_periods=v.window).std() * np.sqrt(v.annualize)


def average_true_range(
    high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, period: int
) -> pd.DataFrame:
    """ATR for the trailing stop. True range uses the prior close, so a gap
    counts as range — which is the whole point of using ATR over high-low.

    The first bar has no prior close and so no true range; it is NaN, not
    high-low. `np.maximum` propagates NaN (unlike `np.fmax`), so a missing
    bar poisons every ATR whose window contains it.
    """
    prev_close = close.shift(PRIOR_BAR)
    true_range = np.maximum(
        high - low,
        np.maximum((high - prev_close).abs(), (low - prev_close).abs()),
    )
    return true_range.rolling(period, min_periods=period).mean()


def _benchmark_sma(benchmark_closes: pd.Series, cfg: Config) -> pd.Series:
    t = cfg.signals.trend_filter
    if t.ma_type.upper() != "SMA":
        raise ValueError(f"trend_filter.ma_type {t.ma_type!r} is not implemented; spec says SMA")
    return benchmark_closes.rolling(t.ma_period, min_periods=t.ma_period).mean()


def market_regime(benchmark_closes: pd.Series, cfg: Config) -> pd.Series:
    """Boolean series: is the benchmark above its 200-day SMA?

    This is the absolute trend filter. When False, the strategy holds cash.
    Fail closed: until a full SMA window exists, or on a date the benchmark
    has no bar, the comparison against NaN is False, i.e. risk-off.
    """
    return (benchmark_closes > _benchmark_sma(benchmark_closes, cfg)).astype(bool)


# -------------------------------------------------------------------- panel


def _check_aligned(closes: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame) -> None:
    if not (closes.index.is_monotonic_increasing and closes.index.is_unique):
        raise ValueError("price panel index must be strictly increasing dates")
    for name, frame in (("high", high), ("low", low)):
        if not (frame.index.equals(closes.index) and frame.columns.equals(closes.columns)):
            raise ValueError(f"{name} is not aligned with closes (same dates and symbols)")


def _universe_mask(
    universe: pd.DataFrame | None, index: pd.Index, symbols: list[str]
) -> pd.DataFrame:
    if universe is None:
        return pd.DataFrame(True, index=index, columns=symbols)
    # Anything the caller did not mark as a member is not a member.
    return universe.reindex(index=index, columns=symbols, fill_value=False).astype(bool)


def compute_panel(
    closes: pd.DataFrame,
    high: pd.DataFrame,
    low: pd.DataFrame,
    cfg: Config,
    universe: pd.DataFrame | None = None,
) -> SignalPanel:
    """Every signal for every date.

    `closes` must include the benchmark column (trend_filter.symbol). It feeds
    the regime and is never itself ranked. `universe` is an optional bool
    dates x symbols mask of who was in force on each date; rank is computed
    only among members with a valid score, so a name that joined the universe
    last month does not displace anyone before it belonged. None means every
    non-benchmark column is a member on every date.
    """
    bench = cfg.signals.trend_filter.symbol
    if bench not in closes.columns:
        raise ValueError(f"benchmark {bench} is missing from the price panel")
    _check_aligned(closes, high, low)

    symbols = sorted(c for c in closes.columns if c != bench)
    px, hi, lo = closes[symbols], high[symbols], low[symbols]
    mask = _universe_mask(universe, closes.index, symbols)

    mom_6m, mom_12m = momentum_components(px, cfg)
    blended = cfg.signals.momentum.weight_short * mom_6m + cfg.signals.momentum.weight_long * mom_12m
    vol = realized_volatility(px, cfg)
    # The floor stops a near-zero-vol name from getting an unbounded score.
    # clip leaves NaN alone.
    score = blended / vol.clip(lower=cfg.signals.volatility.floor)
    # method="first": a tie (only possible with degenerate data) resolves by
    # the sorted symbol order, so the same input always ranks the same way.
    rank = score.where(mask).rank(axis=1, ascending=False, method="first")

    bench_close = closes[bench]
    bench_sma = _benchmark_sma(bench_close, cfg)
    return SignalPanel(
        closes=px, mom_6m=mom_6m, mom_12m=mom_12m, blended_momentum=blended,
        vol_63=vol, score=score, rank=rank,
        atr_20=average_true_range(hi, lo, px, cfg.signals.atr.period),
        in_universe=mask,
        market_on=market_regime(bench_close, cfg),
        benchmark_close=bench_close, benchmark_sma=bench_sma,
    )


def compute(
    closes: pd.DataFrame,
    high: pd.DataFrame,
    low: pd.DataFrame,
    cfg: Config,
    as_of: pd.Timestamp | None = None,
    universe: pd.DataFrame | Iterable[str] | None = None,
) -> SignalFrame:
    """Full signal computation for one date.

    CRITICAL: uses only data at or before `as_of`. Any use of a future bar is
    look-ahead bias and will make the backtest worthless. The test suite
    should assert this by feeding truncated data and comparing.

    The inputs are cut off at `as_of` before anything is computed, so this is
    true by construction, not just by the arithmetic being careful. `as_of`
    defaults to the last row; a date that is not a trading day resolves to the
    last trading day before it. `universe` is a bool mask (as in
    `compute_panel`) or simply the symbols in force on that date.
    """
    if closes.empty:
        raise ValueError("empty price panel")
    when = closes.index[-1] if as_of is None else pd.Timestamp(as_of)
    eligible = closes.index[closes.index <= when]
    if eligible.empty:
        raise ValueError(f"no price data on or before {when.date()}")
    ts = eligible[-1]

    closes, high, low = closes.loc[:ts], high.loc[:ts], low.loc[:ts]
    if universe is not None and not isinstance(universe, pd.DataFrame):
        members = set(universe)
        universe = pd.DataFrame(
            {c: c in members for c in closes.columns}, index=closes.index
        )
    return compute_panel(closes, high, low, cfg, universe).at(ts)
