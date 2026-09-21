"""
Entry and exit rules (spec sections 5 and 6) and position sizing (section 8).

Rules return a decision plus the RULE CODE that fired (E1..E8, R1, T1, X1, X4,
L1..Ln). The code is written to the proposals table so that months later you
can answer "why did I own this?" without re-deriving anything.

Pure functions: no IO, no broker, no database. The backtest and the live path
call exactly these, which is what makes a backtest evidence about live.

Fail closed, throughout. A check that cannot be evaluated (a NaN signal, a
sector that is unknown, no high-water mark for a position) counts as a
failure: an entry is blocked and an exit fires. The alternative is buying or
holding on the strength of a number that does not exist.

v2.0.0 retired three v1 rules. X2 (sell everything when SPY is below its 200-day
SMA) became the graded filter in `regime.py`. X3 (3 x ATR stop) became the
laddered stop below. E9 (two new names a month) became the refill toward the
invested target in `plan.py`.

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

# Not tunables. FULL_WEIGHT is "100% of equity" and "the whole position"; ZERO is
# the sign change that X4 means by "thesis inverted" (E3's threshold is
# configurable, this is not).
FULL_WEIGHT = 1.0
ZERO = 0.0
NO_PEAK = float("-inf")      # a top-up with no prior peak to beat (a regime restore)


@dataclass
class ExitDecision:
    """What to sell of one held name. `sell_fraction` is a share of the shares
    held: 0 = keep, 1 = the whole position."""

    symbol: str
    sell_fraction: float = ZERO
    rule: str | None = None        # X1 | X4 | L1..Ln
    detail: str | None = None
    levels: int = 0                # ladder levels this sale fires

    @property
    def should_sell(self) -> bool:
        return self.sell_fraction > ZERO

    @property
    def full_exit(self) -> bool:
        return self.sell_fraction >= FULL_WEIGHT


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
    ladder_fired: dict[str, int],
    cfg: Config,
) -> list[ExitDecision]:
    """Check X1, X4 and the laddered stop against every open position.

    X1  rank > rank_exit          (buffer above entry rank prevents churn)
    X4  blended_momentum < 0      (thesis inverted)
    L1..Ln  laddered trailing stop, measured from the highest CLOSE since entry.
            With drawdowns (12%, 20%, 28%): at -12% sell a third, at -20% another
            third, at -28% the rest. Each level fires at most once per position.

    Exits are checked WEEKLY. Risk control responds fast; position-taking does
    not need to.

    Ladder arithmetic. `ladder_fired[s]` is how many levels have already fired.
    If `c` levels are now crossed, `c - fired` are new, and they sell
    `(c - fired) / (n - fired)` of the CURRENT shares. That is a third of the
    original, then half of what is left (another third), then all of it, and it
    stays well-defined after a top-up. A gap that crosses several levels at once
    sells them all together. Crossing the last level sells everything.

    Why a ladder, not one stop. v1's 3 x ATR stop fired at 6-12%, not the 15-25%
    the spec claimed, and was the most common exit (177 of 391 sells). A stop that
    tight ejects momentum names on ordinary noise. The ladder gives up a third
    at 12% but keeps the rest through a normal pullback.

    `held` maps symbol -> quantity (the keys are what matter). One decision is
    returned per held symbol, sorted by symbol. A position whose close or high
    cannot be read, or whose X1 or X4 cannot be evaluated, is sold in full: a name
    with no rank today (it left the universe, or has no bar) cannot be shown to
    still belong.
    """
    table = signals.table
    drawdowns = cfg.exit.ladder.drawdowns
    n = len(drawdowns)
    last = f"L{n}"
    priority = (last, "X4", "X1")          # disaster stop, thesis, then the rank buffer
    out: list[ExitDecision] = []
    for sym in sorted(held):
        row = table.loc[sym] if sym in table.index else None
        full: dict[str, str] = {}
        fired = ladder_fired.get(sym, 0)

        close = None if row is None else row["close"]
        high = position_highs.get(sym)
        crossed = 0
        ladder_detail = ""
        if _missing(close) or _missing(high):
            full[last] = "close or position high unavailable; cannot evaluate"
        else:
            crossed = sum(close <= high * (FULL_WEIGHT - d) for d in drawdowns)
            ladder_detail = (f"close {close:.2f} is {FULL_WEIGHT - close / high:.1%} below "
                             f"high {high:.2f}; levels crossed {crossed} of {n}, fired {fired}")
            if crossed >= n:
                full[last] = ladder_detail

        if cfg.exit.exit_on_negative_momentum:
            momentum = None if row is None else row["blended_momentum"]
            if _missing(momentum):
                full["X4"] = "blended momentum unavailable; cannot evaluate"
            elif momentum < ZERO:
                full["X4"] = f"blended momentum {momentum:.4f} < 0"

        rank = signals.rank_of(sym)
        if rank is None:
            full["X1"] = "no rank today (not in universe or no signal); cannot evaluate"
        elif rank > cfg.exit.rank_exit:
            full["X1"] = f"rank {rank} > {cfg.exit.rank_exit}"

        if full:
            rule = next(code for code in priority if code in full)
            detail = "; ".join(f"{code}: {full[code]}" for code in priority if code in full)
            out.append(ExitDecision(sym, FULL_WEIGHT, rule, detail, levels=n - fired))
        elif crossed > fired:
            new = crossed - fired
            out.append(ExitDecision(sym, new / (n - fired), f"L{crossed}",
                                    f"L{crossed}: {ladder_detail}", levels=new))
        else:
            out.append(ExitDecision(sym))
    return out


# ------------------------------------------------------------------- entries


def evaluate_entries(
    signals: SignalFrame,
    held: set[str],
    sector_weights: dict[str, float],
    ai_flags: dict[str, bool | None],
    cfg: Config,
    *,
    regime_normal: bool,
    sectors: dict[str, str | None] | None = None,
    proposed_weight: float | None = None,
    exit_weeks: dict[str, int] | None = None,
) -> list[EntryDecision]:
    """Check E1-E8 (and the re-entry gate R1) against ranked candidates.

    There is no cap on new names per week. v1's E9 (two a month) left the book 35%
    invested on average; v2 refills toward the invested target instead, and
    `plan.py` decides how many of these eligible names that takes.

    Candidates are every ranked symbol not already held, best rank first, and
    every rule is evaluated for each (no short-circuit) so `blocked_by` is the
    full list. Slots are handed out greedily in rank order: only a candidate
    that passes everything consumes a position slot (E6) and sector room (E8),
    so a name blocked by E8 does not starve the next one.

    E1 is the graded filter: `regime_normal` is False in the DEFENSIVE mode, which
    takes no new names. One reading of SPY below its SMA does not change the mode.

    R1 applies only to a name previously SOLD IN FULL, given as `exit_weeks`
    (symbol -> whole weeks since that exit). It may return only when it ranks
    within `reentry.max_rank`, closes above its own SMA, and at least
    `min_weeks_after_exit` weeks have passed. v1 only barred a rebuy in the week of
    the stop; v2 gates it on recovery, because waiting says nothing about whether
    the name recovered and the SMA does.

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
    reentry = cfg.reentry
    exited = exit_weeks or {}

    new_count = 0
    out: list[EntryDecision] = []
    for sym, row in ranked.iterrows():
        if sym in held:
            continue
        blocked: list[str] = []

        if cfg.entry.require_market_on and not regime_normal:
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
        if sym in exited and not (
            exited[sym] >= reentry.min_weeks_after_exit
            and row["rank"] <= reentry.max_rank
            and row["close"] > row["sma_reentry"]                     # NaN fails
        ):
            blocked.append("R1")

        eligible = not blocked
        if eligible:
            new_count += 1
            sector_now[sector] = sector_now.get(sector, ZERO) + weight
        out.append(EntryDecision(sym, eligible, blocked, int(row["rank"])))
    return out


def evaluate_topups(
    signals: SignalFrame,
    partials: dict[str, float],
    weights: dict[str, float],
    sector_weights: dict[str, float],
    ai_flags: dict[str, bool | None],
    cfg: Config,
    *,
    regime_normal: bool,
    sectors: dict[str, str | None] | None = None,
) -> list[EntryDecision]:
    """Which partly-sold positions may be topped up back toward their target.

    `partials` maps a held name to the prior peak close it must now beat (T1: no
    top-up until it closes ABOVE that peak, at the weekly check). A name that had a
    ladder sale has a real peak; a name that was only trimmed pro rata by the
    defensive filter has `NO_PEAK`, since a pro rata trim says nothing about the
    name and it should simply come back when the filter does. The same E1-E5 conditions as a new entry apply, and E8 is
    checked for the extra weight the top-up could add (up to the position cap).
    A name that is still held does not take an E6 slot.

    Why the peak. A name that fell 12% and has recovered only part of it is still
    a name that just showed it can fall; buying back before it takes out the old
    high is averaging down into weakness. Selling early is what the ladder costs;
    this is how a name is bought back only once it resumes.
    """
    table = signals.table
    veto_on = cfg.ai.enabled and cfg.entry.require_ai_veto_pass
    sector_now = dict(sector_weights)
    ranked = table[table["rank"].notna()].sort_values("rank")
    out: list[EntryDecision] = []
    for sym, row in ranked.iterrows():
        if sym not in partials:
            continue
        blocked: list[str] = []
        if cfg.entry.require_market_on and not regime_normal:
            blocked.append("E1")
        if not row["rank"] <= cfg.entry.rank_threshold:
            blocked.append("E2")
        if not row["blended_momentum"] > cfg.entry.min_momentum:
            blocked.append("E3")
        if not row["vol_63"] <= cfg.entry.max_volatility:
            blocked.append("E4")
        if veto_on and ai_flags.get(sym) is True:
            blocked.append("E5")
        sector = None if sectors is None else sectors.get(sym)
        room = cfg.sizing.max_position_weight - weights.get(sym, ZERO)
        if sector is None or sector_now.get(sector, ZERO) + room > cfg.risk.max_sector_weight:
            blocked.append("E8")
        if not row["close"] > partials[sym]:                          # NaN fails
            blocked.append("T1")
        eligible = not blocked
        if eligible:
            sector_now[sector] = sector_now.get(sector, ZERO) + room
        out.append(EntryDecision(sym, eligible, blocked, int(row["rank"])))
    return out


# -------------------------------------------------------------------- sizing


def _bounded_weights(raw: pd.Series, lo: float, hi: float, budget: float) -> pd.Series:
    """Weights proportional to `raw` that sum to `budget`, each held within [lo, hi].

    Spec section 8 says "clip, then renormalize". Taken literally that breaks
    the cap it just applied: two names at the cap renormalize to 50% each. Here a
    name that hits a bound stays there and the rest share what is left, so the
    bounds hold in the result. When every name is capped the weights sum to less
    than `budget` and the remainder is cash. The two agree whenever nothing is
    clipped. (Unchanged from v1.0.4 except that the budget is the invested target
    rather than 100%.)
    """
    fixed: dict[str, float] = {}
    while True:
        free = [s for s in raw.index if s not in fixed]
        left = budget - sum(fixed.values())
        if not free:
            break
        if left <= ZERO:
            raise ValueError("position bounds are infeasible: minimum weights exceed the target")
        cand = raw[free] / raw[free].sum() * left
        violators = cand[(cand > hi) | (cand < lo)]
        if violators.empty:
            fixed.update(cand.to_dict())
            break
        for s, w in violators.items():
            fixed[s] = min(max(w, lo), hi)
    weights = pd.Series(fixed).reindex(raw.index)
    if weights.sum() > budget + np.finfo(float).eps * len(weights):
        raise ValueError("position bounds are infeasible: minimum weights exceed the target")
    return weights


def size_positions(
    symbols: list[str],
    vol: pd.Series,
    equity: float,
    invested_target: float,
    cfg: Config,
) -> pd.Series:
    """Inverse-volatility weights, bounded, scaled to the invested target.

    Weights are proportional to 1 / vol_63, sum to `invested_target` (85% while
    the market filter is on), and each sits within the position bounds (5%-15%).
    A name at a bound stays there and the remainder is cash. v1 also scaled down
    to a 15% portfolio volatility target; that left the book 35% invested and is
    gone. The invested target is the only thing that sets the total.

    Returns target dollar values indexed by symbol. `vol` is annualized 63-day
    volatility per symbol; a missing or non-positive value raises rather than
    guessing (fail closed). Gross exposure never exceeds `risk.max_gross_exposure`.
    """
    if not symbols:
        return pd.Series(dtype=float)
    v = vol.reindex(symbols)
    if v.isna().any() or not (v > ZERO).all():
        bad = v[v.isna() | ~(v > ZERO)].index.tolist()
        raise ValueError(f"cannot size {bad}: volatility missing or not positive")
    budget = min(invested_target, cfg.risk.max_gross_exposure)
    weights = _bounded_weights(
        FULL_WEIGHT / v, cfg.sizing.min_position_weight, cfg.sizing.max_position_weight, budget)
    return weights * equity


def size_new_positions(
    book: list[str],
    new: list[str],
    topups: list[str],
    held_values: dict[str, float],
    vol: pd.Series,
    equity: float,
    invested_target: float,
    cfg: Config,
) -> tuple[dict[str, float], list[str]]:
    """Dollars to BUY for the new names and the top-ups, with E7 enforced.

    `book` is everything that will be held (kept positions plus `new`).
    `held_values` is the current dollar value of every kept position. Buying is
    limited to the room under the target, `invested_target x equity` less what is
    already held: top-ups first (each up to its target value), then the new names
    share what is left pro rata. So a refill can never push the book past the
    target, whatever the allocation says.

    A new name whose dollars fall under `sizing.min_position_value` is dropped and
    the book resized, smallest first, until every new name clears it (inclusive).
    Existing positions and top-ups are never dropped here; exits are X1/X4/the
    ladder's job. Returns the buys and the E7-blocked symbols in order dropped.
    """
    names, dropped = list(book), []
    cap = min(invested_target, cfg.risk.max_gross_exposure) * equity
    while True:
        alloc = size_positions(names, vol, equity, invested_target, cfg)
        room = max(ZERO, cap - sum(held_values.values()))
        buys: dict[str, float] = {}
        for s in topups:
            buys[s] = min(max(ZERO, alloc[s] - held_values[s]), room)
            room -= buys[s]
        want_new = {s: alloc[s] for s in names if s in new}
        total = sum(want_new.values())
        scale = FULL_WEIGHT if total <= room else room / total
        buys.update({s: d * scale for s, d in want_new.items()})
        small = [s for s in want_new if not buys[s] >= cfg.sizing.min_position_value]
        if not small:
            return buys, dropped
        worst = min(small, key=lambda s: (buys[s], s))
        names.remove(worst)
        dropped.append(worst)
