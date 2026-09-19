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

STATUS: stub. Signatures and contracts are fixed; bodies are next.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from src.config import Config


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


@dataclass
class BacktestResult:
    windows: list[WindowResult] = field(default_factory=list)
    equity_curve: pd.Series | None = None
    benchmark_curve: pd.Series | None = None

    @property
    def oos_cagr(self) -> float:
        raise NotImplementedError

    @property
    def worst_rolling_12m(self) -> float:
        raise NotImplementedError

    @property
    def max_single_window_contribution(self) -> float:
        """Share of total excess return from the single best window.

        Acceptance criterion A6, and the one most people skip. A strategy
        whose entire edge came from one lucky year is not a strategy.
        """
        raise NotImplementedError


@dataclass
class AcceptanceVerdict:
    passed: bool
    results: dict[str, tuple[bool, float, float]]  # name -> (pass, actual, threshold)

    def report(self) -> str:
        raise NotImplementedError


def apply_costs(
    trade_value: float, shares: float, cfg: Config, is_entry: bool
) -> float:
    """Commission + slippage + half-spread on one side of a trade.

    Be pessimistic here. An optimistic cost model is the most common way a
    backtest that "works" fails in live trading.
    """
    raise NotImplementedError


def run_window(
    closes: pd.DataFrame,
    high: pd.DataFrame,
    low: pd.DataFrame,
    benchmark: pd.Series,
    start: date,
    end: date,
    cfg: Config,
) -> WindowResult:
    """Simulate one out-of-sample window."""
    raise NotImplementedError


def walk_forward(cfg: Config) -> BacktestResult:
    """Roll in-sample/out-of-sample windows forward through the full period.

    Period must include 2008, 2020 and 2022. A backtest starting in 2010 has
    never seen a real bear market.
    """
    raise NotImplementedError


def check_acceptance(result: BacktestResult, cfg: Config) -> AcceptanceVerdict:
    """A1-A6 from spec section 13.

    If A1 fails, the honest move is to stop and buy the index. That decision
    is pre-committed here rather than made later while attached to the build.
    """
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(
        "Backtest is not implemented yet.\n"
        "Prerequisite: a populated price cache (make refresh) and\n"
        "src/strategy/signals.py implemented."
    )
