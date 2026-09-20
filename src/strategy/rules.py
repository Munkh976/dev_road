"""
Entry and exit rules (spec sections 5 and 6) and position sizing (section 8).

Rules return a decision plus the RULE CODE that fired (E1..E9, X1..X4). The
code is written to the proposals table so that months later you can answer
"why did I own this?" without re-deriving anything.

Pure functions: no IO, no broker, no database. The backtest and the live path
call exactly these, which is what makes a backtest evidence about live.

Fail closed, throughout. A check that cannot be evaluated (a NaN signal, a
sector that is unknown, no high-water mark for a position) counts as a
failure: an entry is blocked and an exit fires. The alternative is buying or
holding on the strength of a number that does not exist.

E7 (minimum position value) is not in `evaluate_entries`: it needs the sized
dollar value, and sizing needs to know who was selected. It is enforced by
`size_new_positions`, which drops the smallest new position and resizes until
every new position clears the minimum.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.config import Config
from src.strategy.signals import SignalFrame

# Not tunables. FULL_WEIGHT is "100% of equity"; ZERO is the sign change that
# X4 means by "thesis inverted" (E3's threshold is configurable, this is not).
FULL_WEIGHT = 1.0
ZERO = 0.0

# Which rule is reported when several fire on the same position. Risk first:
# the regime, then the disaster stop, then the thesis, then the rank buffer.
# Every rule that fired is listed in `detail` regardless.
EXIT_PRIORITY = ("X2", "X3", "X4", "X1")


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


def _missing(value: float | None) -> bool:
    return value is None or bool(pd.isna(value))


# --------------------------------------------------------------------- exits


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

    `held` maps symbol -> quantity (the keys are what matter). One decision is
    returned per held symbol, sorted by symbol. A position whose X1, X3 or X4
    cannot be evaluated exits under that rule: a name with no rank today (it
    left the universe, or has no bar) cannot be shown to still belong.
    """
    table = signals.table
    out: list[ExitDecision] = []
    for sym in sorted(held):
        row = table.loc[sym] if sym in table.index else None
        fired: dict[str, str] = {}

        if cfg.exit.exit_all_on_market_off and not signals.market_on:
            fired["X2"] = "market_on is False"

        close = None if row is None else row["close"]
        high, a = position_highs.get(sym), atr.get(sym)
        if _missing(close) or _missing(high) or _missing(a):
            fired["X3"] = "close, position high or ATR unavailable; cannot evaluate"
        else:
            stop = high - cfg.exit.trailing_stop_atr * a
            if close < stop:
                fired["X3"] = (
                    f"close {close:.2f} < stop {stop:.2f} "
                    f"(high {high:.2f} - {cfg.exit.trailing_stop_atr} x ATR {a:.2f})"
                )

        if cfg.exit.exit_on_negative_momentum:
            momentum = None if row is None else row["blended_momentum"]
            if _missing(momentum):
                fired["X4"] = "blended momentum unavailable; cannot evaluate"
            elif momentum < ZERO:
                fired["X4"] = f"blended momentum {momentum:.4f} < 0"

        rank = signals.rank_of(sym)
        if rank is None:
            fired["X1"] = "no rank today (not in universe or no signal); cannot evaluate"
        elif rank > cfg.exit.rank_exit:
            fired["X1"] = f"rank {rank} > {cfg.exit.rank_exit}"

        if fired:
            rule = next(code for code in EXIT_PRIORITY if code in fired)
            detail = "; ".join(f"{code}: {fired[code]}" for code in EXIT_PRIORITY if code in fired)
            out.append(ExitDecision(sym, True, rule, detail))
        else:
            out.append(ExitDecision(sym, False))
    return out


# ------------------------------------------------------------------- entries


def evaluate_entries(
    signals: SignalFrame,
    held: set[str],
    sector_weights: dict[str, float],
    ai_flags: dict[str, bool | None],
    cfg: Config,
    *,
    sectors: dict[str, str | None] | None = None,
    proposed_weight: float | None = None,
) -> list[EntryDecision]:
    """Check E1-E9 against ranked candidates.

    E9 (max 2 new per rebalance) is what spreads the initial $15,000 entry
    across several months without requiring any market timing.

    Candidates are every ranked symbol not already held, best rank first, and
    every rule is evaluated for each (no short-circuit) so `blocked_by` is the
    full list. Slots are handed out greedily in rank order: only a candidate
    that passes everything consumes an E6 position slot, an E9 new-entry slot
    and sector room, so a name blocked by E8 does not starve the next one.

    `sector_weights` is the current weight of each sector (fraction of equity).
    `sectors` maps a candidate to its sector; without one E8 cannot be
    evaluated and blocks (fail closed). E8 is checked against `proposed_weight`
    (default: max_position_weight, the largest weight sizing can give a name),
    so a passing entry cannot breach the cap once it is sized.

    E5: `True` blocks. `False` passes. `None` or absent means the AI layer was
    unavailable, which proceeds without the veto and is logged (spec section
    7). Backtests pass no flags: the veto cannot be reproduced historically.
    E7 is enforced after sizing; see `size_new_positions`.
    """
    table = signals.table
    weight = cfg.sizing.max_position_weight if proposed_weight is None else proposed_weight
    ranked = table[table["rank"].notna()].sort_values("rank")
    sector_now = dict(sector_weights)
    veto_on = cfg.ai.enabled and cfg.entry.require_ai_veto_pass

    new_count = 0
    out: list[EntryDecision] = []
    for sym, row in ranked.iterrows():
        if sym in held:
            continue
        blocked: list[str] = []

        if cfg.entry.require_market_on and not signals.market_on:
            blocked.append("E1")
        if not row["rank"] <= cfg.entry.rank_threshold:
            blocked.append("E2")
        if not row["blended_momentum"] > cfg.entry.min_momentum:      # NaN fails
            blocked.append("E3")
        if not row["vol_63"] <= cfg.entry.max_volatility:             # NaN fails
            blocked.append("E4")
        if veto_on and ai_flags.get(sym) is True:
            blocked.append("E5")
        if len(held) + new_count + 1 > cfg.risk.max_positions:
            blocked.append("E6")
        sector = None if sectors is None else sectors.get(sym)
        if sector is None or sector_now.get(sector, ZERO) + weight > cfg.risk.max_sector_weight:
            blocked.append("E8")
        if new_count >= cfg.entry.max_new_per_rebalance:
            blocked.append("E9")

        eligible = not blocked
        if eligible:
            new_count += 1
            sector_now[sector] = sector_now.get(sector, ZERO) + weight
        out.append(EntryDecision(sym, eligible, blocked, int(row["rank"])))
    return out


# -------------------------------------------------------------------- sizing


def _bounded_weights(raw: pd.Series, lo: float, hi: float) -> pd.Series:
    """Weights proportional to `raw`, each held within [lo, hi].

    Spec section 8 says "clip, then renormalize". Taken literally that breaks
    the cap it just applied: two names at the 20% cap renormalize to 50% each.
    Here a name that hits a bound stays there and the rest share what is left,
    so the bounds hold in the result. When every name is capped the weights sum
    to less than 1 and the remainder is cash. The two agree whenever nothing is
    clipped, and for the usual six-name book.
    """
    fixed: dict[str, float] = {}
    while True:
        free = [s for s in raw.index if s not in fixed]
        left = FULL_WEIGHT - sum(fixed.values())
        if not free:
            break
        if left <= ZERO:
            raise ValueError("position bounds are infeasible: minimum weights exceed 100%")
        cand = raw[free] / raw[free].sum() * left
        violators = cand[(cand > hi) | (cand < lo)]
        if violators.empty:
            fixed.update(cand.to_dict())
            break
        for s, w in violators.items():
            fixed[s] = min(max(w, lo), hi)
    weights = pd.Series(fixed).reindex(raw.index)
    if weights.sum() > FULL_WEIGHT + np.finfo(float).eps * len(weights):
        raise ValueError("position bounds are infeasible: minimum weights exceed 100%")
    return weights


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

    `vol` is annualized 63-day volatility per symbol; `returns` are daily
    returns (rows up to the decision date, NaN where a bar was missing). The
    covariance uses the last `covariance_window` rows and must be complete: an
    incomplete window means the volatility inputs are not trustworthy, so it
    raises instead of guessing. Gross exposure never exceeds
    `risk.max_gross_exposure`, whatever `scale_up_allowed` says.
    """
    sz = cfg.sizing
    if not symbols:
        return pd.Series(dtype=float)
    v = vol.reindex(symbols)
    if v.isna().any() or not (v > ZERO).all():
        bad = v[v.isna() | ~(v > ZERO)].index.tolist()
        raise ValueError(f"cannot size {bad}: volatility missing or not positive")

    weights = _bounded_weights(FULL_WEIGHT / v, sz.min_position_weight, sz.max_position_weight)

    window = returns[symbols].tail(sz.covariance_window)
    if len(window) < sz.covariance_window or window.isna().any().any():
        raise ValueError(
            f"covariance window incomplete: need {sz.covariance_window} full rows of returns"
        )
    w = weights.to_numpy()
    port_vol = float(np.sqrt(w @ window.cov().to_numpy() @ w)
                     * np.sqrt(cfg.signals.volatility.annualize))

    scalar = FULL_WEIGHT if port_vol <= ZERO else sz.target_portfolio_vol / port_vol
    if not sz.scale_up_allowed:
        scalar = min(FULL_WEIGHT, scalar)
    scalar = min(scalar, cfg.risk.max_gross_exposure / weights.sum())
    return weights * scalar * equity


def size_new_positions(
    symbols: list[str],
    new: list[str],
    vol: pd.Series,
    returns: pd.DataFrame,
    equity: float,
    cfg: Config,
) -> tuple[pd.Series, list[str]]:
    """Size the whole book and enforce E7 on the new names.

    `symbols` is everything that will be held (existing plus `new`). A new name
    whose target value is under `sizing.min_position_value` is dropped and the
    book resized, smallest first, until every new name clears it. Existing
    positions are never dropped here; exits are X1-X4's job. Returns the
    target values and the E7-blocked symbols in the order dropped.
    """
    names, dropped = list(symbols), []
    while True:
        targets = size_positions(names, vol, returns, equity, cfg)
        small = [s for s in new if s in targets.index
                 and not targets[s] >= cfg.sizing.min_position_value]
        if not small:
            return targets, dropped
        worst = min(small, key=lambda s: (targets[s], s))
        names.remove(worst)
        dropped.append(worst)
