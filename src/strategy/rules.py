"""
Entry and exit rules (spec sections 5 and 6) and position sizing (section 8).

Rules return a decision plus the RULE CODE that fired (E1..E9, X1..X4). The
code is written to the proposals table so that months later you can answer
"why did I own this?" without re-deriving anything.

STATUS: stub. Signatures and contracts are fixed; bodies are next.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from src.config import Config
from src.strategy.signals import SignalFrame


@dataclass
class ExitDecision:
    symbol: str
    should_exit: bool
    rule: str | None = None        # X1|X2|X3|X4
    detail: str | None = None


@dataclass
class EntryDecision:
    symbol: str
    eligible: bool
    blocked_by: list[str] = field(default_factory=list)   # failed rule codes
    rank: int | None = None


def evaluate_exits(
    held: dict[str, float],
    signals: SignalFrame,
    position_highs: dict[str, float],
    atr: dict[str, float],
    cfg: Config,
) -> list[ExitDecision]:
    """Check X1-X4 against every open position.

    X1  rank > rank_exit          (buffer above entry rank prevents churn)
    X2  market_on is False        (exit everything to cash)
    X3  close < high - 3*ATR      (wide trailing disaster stop)
    X4  blended_momentum < 0      (thesis inverted)

    Exits are checked WEEKLY while entries are monthly. That asymmetry is
    deliberate: risk control responds fast, position-taking does not.
    """
    raise NotImplementedError


def evaluate_entries(
    signals: SignalFrame,
    held: set[str],
    sector_weights: dict[str, float],
    ai_flags: dict[str, bool | None],
    cfg: Config,
) -> list[EntryDecision]:
    """Check E1-E9 against ranked candidates.

    E9 (max 2 new per rebalance) is what spreads the initial $15,000 entry
    across several months without requiring any market timing.
    """
    raise NotImplementedError


def size_positions(
    symbols: list[str],
    vol: pd.Series,
    returns: pd.DataFrame,
    equity: float,
    cfg: Config,
) -> pd.Series:
    """Inverse-volatility weights, clipped, then scaled to a portfolio vol
    target. Scaling is DOWN ONLY — never lever up to hit the target.

    A scalar below 1.0 leaves the book deliberately under-invested because
    its constituents are volatile. That is the mechanism working.

    Returns target dollar values indexed by symbol. The remainder is cash.
    """
    raise NotImplementedError
