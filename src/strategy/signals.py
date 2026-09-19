"""
Signal computation. Pure functions over a price matrix — no IO, no broker,
no database. That is what makes them testable and identical in backtest and
live paths.

Implements spec section 4. Every parameter comes from config; there are no
literals in this module by design.

STATUS: stub. Signatures and contracts are fixed; bodies are next.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.config import Config


@dataclass(frozen=True)
class SignalFrame:
    """Signals for one date across the universe."""

    signal_date: pd.Timestamp
    market_on: bool
    table: pd.DataFrame  # index=symbol, cols: close, mom_6m, mom_12m,
                         # blended_momentum, vol_63, score, rank, atr_20

    def top(self, n: int) -> list[str]:
        return self.table.nsmallest(n, "rank").index.tolist()

    def rank_of(self, symbol: str) -> int | None:
        if symbol not in self.table.index:
            return None
        return int(self.table.loc[symbol, "rank"])


def blended_momentum(closes: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """P[t-skip]/P[t-lookback] - 1, blended across two lookbacks.

    The 21-day skip avoids short-term reversal, which is documented and
    opposite-signed at the one-month horizon (spec section 4).

    Returns a DataFrame aligned to `closes`, NaN where history is short.
    """
    raise NotImplementedError


def realized_volatility(closes: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Annualized stdev of daily returns over the configured window."""
    raise NotImplementedError


def average_true_range(
    high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, period: int
) -> pd.DataFrame:
    """ATR for the trailing stop. True range uses the prior close, so a gap
    counts as range — which is the whole point of using ATR over high-low."""
    raise NotImplementedError


def market_regime(benchmark_closes: pd.Series, cfg: Config) -> pd.Series:
    """Boolean series: is the benchmark above its 200-day SMA?

    This is the absolute trend filter. When False, the strategy holds cash.
    """
    raise NotImplementedError


def compute(
    closes: pd.DataFrame,
    high: pd.DataFrame,
    low: pd.DataFrame,
    cfg: Config,
    as_of: pd.Timestamp | None = None,
) -> SignalFrame:
    """Full signal computation for one date.

    CRITICAL: uses only data at or before `as_of`. Any use of a future bar is
    look-ahead bias and will make the backtest worthless. The test suite
    should assert this by feeding truncated data and comparing.
    """
    raise NotImplementedError
