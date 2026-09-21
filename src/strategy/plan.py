"""
The weekly order plan (spec sections 6, 8, 9). Pure: no IO, no broker, no database.

Given one date's signals, what is held, and the regime mode, decide what to sell
and what to buy. The backtest fills these at the next open; live turns them
into proposals. Neither adds rules of its own, which is what makes a backtest
evidence about live.

Order of the week, and why it is this order:

  1. Sells that end a position or ladder it down: X1, X4, L1..Ln.
  2. Cap-drift trims: a name above the position cap is cut back to the cap.
  3. DEFENSIVE only: trim every position pro rata down to the defensive target.
  4. NORMAL only, and from what is left after 1-3: top-ups, then a refill. A top-up
     restores a name partly sold on the ladder (once it beats its old peak) or
     trimmed pro rata by the defensive filter (as soon as the filter is back).
     Without the second, positions trimmed to 40% would never come back: the refill
     only buys names not held, and a full book has no free slot.

Invested fraction is measured AFTER the week's planned sells, so proceeds from a
stopped-out name are redeployed the same week rather than waiting for the next
one. No sale is made to fund a buy of a different name (nothing is sold because
another name looks better): every sale here is one of the rules above.

There is deliberately no take-profit rule. A fixed take-profit cuts momentum's
largest winners, and selling winners early (the disposition effect) is one of the
standard explanations for why momentum exists at all. Profits are protected by
the peak-based ladder and by trimming names that drift above the cap. Adding a
take-profit rule later must spend parameter budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

from src.config import Config
from src.strategy.regime import DEFENSIVE, NORMAL, target_invested
from src.strategy.rules import (
    FULL_WEIGHT,
    NO_PEAK,
    ZERO,
    evaluate_entries,
    evaluate_exits,
    evaluate_topups,
    size_new_positions,
    size_positions,
)
from src.strategy.signals import SignalFrame

SELL = "SELL"
BUY = "BUY"
CAP_TRIM = "CAP_TRIM"
REGIME_TRIM = "REGIME_TRIM"
ENTRY = "ENTRY"
REENTRY = "REENTRY"
TOPUP = "TOPUP"
RESTORE = "RESTORE"


@dataclass(frozen=True)
class Holding:
    """One open position as the planner sees it."""

    shares: float
    value: float                       # shares x last close
    high: float                        # highest close since entry
    ladder_fired: int = 0              # ladder levels already used on this position
    topup_above: float | None = None   # after a ladder sale: no top-up until close > this
    restore: bool = False              # trimmed pro rata by the defensive filter: comes back with it


@dataclass(frozen=True)
class PlannedOrder:
    symbol: str
    side: str                          # SELL | BUY
    rule: str                          # X1, X4, L1.., CAP_TRIM, REGIME_TRIM, ENTRY, REENTRY, TOPUP, RESTORE
    fraction: float = ZERO             # SELL: share of the shares held at decision time
    dollars: float = ZERO              # BUY: value to buy
    levels: int = 0                    # ladder levels this sale uses up
    peak: float = float("nan")         # position high when a ladder sale fired
    full_exit: bool = False


@dataclass
class Plan:
    orders: list[PlannedOrder] = field(default_factory=list)
    e7_drops: int = 0
    sizing_failed: bool = False


def plan_week(
    signals: SignalFrame,
    holdings: dict[str, Holding],
    equity: float,
    mode: str,
    exit_weeks: dict[str, int],
    sectors: dict[str, str | None],
    ai_flags: dict[str, bool | None],
    cfg: Config,
) -> Plan:
    """Everything to sell and buy from this date's close.

    `mode` is the confirmed regime (`regime.NORMAL` or `DEFENSIVE`), `exit_weeks`
    maps a name sold in full to whole weeks since. `equity` is cash plus every
    position at its last close. A sizing failure (a missing volatility, bounds
    that cannot hold) plans no buys and keeps the sells: fail closed on entries,
    never on getting out.
    """
    plan = Plan()
    sold = {s: ZERO for s in holdings}         # cumulative share of decision-time shares
    value = {s: h.value for s, h in holdings.items()}
    fully_out: set[str] = set()
    eps = np.finfo(float).eps * equity

    def sell(sym: str, frac: float, rule: str, **kw) -> None:
        plan.orders.append(PlannedOrder(sym, SELL, rule, fraction=frac, **kw))
        sold[sym] += frac
        value[sym] = holdings[sym].value * (FULL_WEIGHT - sold[sym])

    # 1. exits and the ladder
    decisions = evaluate_exits(
        {s: h.shares for s, h in holdings.items()}, signals,
        {s: h.high for s, h in holdings.items()},
        {s: h.ladder_fired for s, h in holdings.items()}, cfg)
    for d in decisions:
        if d.should_sell:
            sell(d.symbol, d.sell_fraction, d.rule, levels=d.levels,
                 peak=holdings[d.symbol].high, full_exit=d.full_exit)
            if d.full_exit:
                fully_out.add(d.symbol)

    # 2. cap-drift trims
    cap_value = cfg.sizing.max_position_weight * equity
    for sym in sorted(value):
        excess = value[sym] - cap_value
        if sym not in fully_out and excess > eps:
            sell(sym, excess / holdings[sym].value, CAP_TRIM)

    kept = {s: v for s, v in value.items() if s not in fully_out and v > ZERO}

    # 3. defensive: pro rata down to the target
    if mode == DEFENSIVE:
        target = target_invested(mode, cfg)
        invested = sum(kept.values())
        if invested > (target + cfg.regime.defensive_trim_band) * equity:
            keep_share = target * equity / invested
            for sym in sorted(kept):
                sell(sym, kept[sym] * (FULL_WEIGHT - keep_share) / holdings[sym].value,
                     REGIME_TRIM)
                kept[sym] = value[sym]
        return plan                          # DEFENSIVE never buys: no new names, no top-ups

    # 4. NORMAL: top-ups, then the refill
    assert mode == NORMAL, mode
    target = target_invested(mode, cfg)
    invested = sum(kept.values())
    sector_w: dict[str | None, float] = {}
    for s, v in kept.items():
        sector_w[sectors.get(s)] = sector_w.get(sectors.get(s), ZERO) + v / equity
    weights = {s: v / equity for s, v in kept.items()}

    partials = {s: NO_PEAK if h.topup_above is None else h.topup_above
                for s, h in holdings.items()
                if s in kept and (h.topup_above is not None or h.restore) and sold[s] == ZERO}
    topups = [d.symbol for d in evaluate_topups(
        signals, partials, weights, sector_w, ai_flags, cfg,
        regime_normal=True, sectors=sectors) if d.eligible]
    for s in topups:                          # reserve the room a top-up may use (E8)
        sector_w[sectors.get(s)] = (sector_w.get(sectors.get(s), ZERO)
                                    + cfg.sizing.max_position_weight - weights[s])

    eligible: list[str] = []
    if invested < cfg.regime.refill_below * equity:
        candidates = replace(signals, table=signals.table.drop(
            index=list(fully_out), errors="ignore"))
        eligible = [d.symbol for d in evaluate_entries(
            candidates, set(kept), sector_w, ai_flags, cfg, regime_normal=True,
            sectors=sectors, exit_weeks=exit_weeks) if d.eligible]
    if not (eligible or topups):
        return plan

    vol = signals.table["vol_63"]
    headroom = target * equity - invested
    try:
        chosen: list[str] = []
        for k in range(1, len(eligible) + 1):        # add names until the target is reachable
            chosen = eligible[:k]
            alloc = size_positions(list(kept) + chosen, vol, equity, target, cfg)
            need = sum(max(ZERO, alloc[s] - kept[s]) for s in topups) + sum(alloc[s] for s in chosen)
            if need >= headroom - eps * len(alloc):      # float noise is not a shortfall
                break
        buys, dropped = size_new_positions(
            list(kept) + chosen, chosen, topups, kept, vol, equity, target, cfg)
    except ValueError:
        plan.sizing_failed = True
        return plan

    plan.e7_drops = len(dropped)
    for sym, dollars in buys.items():
        if dollars > ZERO:
            rule = ((TOPUP if holdings[sym].topup_above is not None else RESTORE)
                    if sym in topups else REENTRY if sym in exit_weeks else ENTRY)
            plan.orders.append(PlannedOrder(sym, BUY, rule, dollars=dollars))
    return plan
