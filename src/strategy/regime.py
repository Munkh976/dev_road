"""
Graded market filter (spec section 4.1). Pure: no IO, no broker, no database.

v1 sold everything the day SPY closed below its 200-day SMA (X2). v2 keeps the
filter but grades the response: two consecutive weekly readings below move the
book to the DEFENSIVE target, two consecutive readings above move it back to
NORMAL. A single reading in the other direction changes nothing, so one
whipsaw week cannot flip the book.

The mode is a fold over the weekly readings, so it is a pure function of SPY's
history: the backtest steps it one week at a time with `next_mode`, and live can
rebuild the same answer with `regime_modes` instead of storing state that could
drift from the prices (there is no positions table for the same reason).

Fail closed: the fold starts DEFENSIVE. Until two consecutive readings above the
SMA exist, the book is not asked to be fully invested. A reading that cannot be
evaluated is `market_on == False` (signals.market_regime), so it counts as below.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from src.config import Config

NORMAL = "normal"
DEFENSIVE = "defensive"
NO_STREAK = 0


@dataclass(frozen=True)
class RegimeState:
    mode: str = DEFENSIVE
    streak: int = NO_STREAK    # consecutive weekly readings that contradict `mode`


def next_mode(state: RegimeState, market_on: bool, cfg: Config) -> RegimeState:
    """Fold one weekly reading into the state."""
    agrees = market_on == (state.mode == NORMAL)
    if agrees:
        return RegimeState(state.mode, NO_STREAK)
    streak = state.streak + 1
    if streak >= cfg.regime.confirm_weeks:
        return RegimeState(DEFENSIVE if state.mode == NORMAL else NORMAL, NO_STREAK)
    return RegimeState(state.mode, streak)


def regime_modes(readings: Iterable[bool], cfg: Config) -> list[str]:
    """The mode in force after each weekly reading, from the fail-closed start."""
    state, out = RegimeState(), []
    for reading in readings:
        state = next_mode(state, bool(reading), cfg)
        out.append(state.mode)
    return out


def target_invested(mode: str, cfg: Config) -> float:
    """Invested fraction the regime asks for (before the live risk dial)."""
    return cfg.regime.target_normal if mode == NORMAL else cfg.regime.target_defensive
