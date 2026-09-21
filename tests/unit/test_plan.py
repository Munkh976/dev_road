"""The weekly plan (spec sections 6, 8, 9): exits, cap trims, the graded filter,
top-ups and the refill toward the invested target. Every case is worked by hand
at equity $15,000, the 85% target (= $12,750) and a 15% cap (= $2,250)."""

from __future__ import annotations

import pytest
import yaml

from src.config import DEFAULT_CONFIG_PATH, Config
from src.strategy.plan import (
    BUY,
    CAP_TRIM,
    ENTRY,
    REENTRY,
    REGIME_TRIM,
    RESTORE,
    SELL,
    TOPUP,
    Holding,
)
from src.strategy.regime import DEFENSIVE, NORMAL
from tests.mutants import load_mutant
from tests.unit.test_rules import frame, variant

@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config(**yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")))


EQUITY = 15000.0
TARGET = 0.85 * EQUITY          # 12,750
CAP = 0.15 * EQUITY             # 2,250


def held(**values: float) -> dict[str, Holding]:
    """Positions by dollar value; every name trades at 100 and peaked at 100."""
    return {s: Holding(v / 100.0, v, 100.0) for s, v in values.items()}


def run(rows, holdings, cfg, mode=NORMAL, equity=EQUITY, exit_weeks=None, sectors=None, mod=None):
    sf = frame(rows)
    sectors = sectors if sectors is not None else {s: f"Sec-{s}" for s in rows}
    return (mod or __import__("src.strategy.plan", fromlist=["plan_week"])).plan_week(
        sf, holdings, equity, mode, exit_weeks or {}, sectors, {}, cfg)


def only(plan, side=None, rule=None):
    return [o for o in plan.orders
            if (side is None or o.side == side) and (rule is None or o.rule == rule)]


def healthy(n, start=1, **kw):
    return {f"C{i}": {"rank": i, **kw} for i in range(start, start + n)}


# ============================================================== the refill


def test_an_empty_book_is_filled_toward_the_target_in_one_week(cfg):
    """No monthly cap and no two-a-month: six names at once. Five names would cap
    at 15% (75% invested, short of 85%), so the sixth is added: 6 x 14.17%."""
    plan = run(healthy(12), {}, cfg)
    buys = only(plan, BUY)
    assert [o.symbol for o in buys] == [f"C{i}" for i in range(1, 7)]      # best ranks first
    assert all(o.rule == ENTRY for o in buys)
    assert [o.dollars for o in buys] == pytest.approx([TARGET / 6] * 6)
    assert sum(o.dollars for o in buys) == pytest.approx(TARGET)
    assert not only(plan, SELL)


def test_the_book_never_ends_above_the_target(cfg):
    holdings = held(H1=2000, H2=2000, H3=2000, H4=2000, H5=2000)          # 10,000 = 66.7%
    rows = {**{s: {"rank": i} for i, s in enumerate(holdings, 1)}, **healthy(5, start=6)}
    plan = run(rows, holdings, cfg)
    invested = sum(h.value for h in holdings.values()) + sum(o.dollars for o in only(plan, BUY))
    assert invested <= TARGET + 1e-6
    assert invested == pytest.approx(TARGET)                              # and reaches it


def test_no_refill_at_or_above_80_percent_and_a_refill_below(cfg):
    names = {f"H{i}": 2016.7 for i in range(1, 7)}                        # 12,100.2 = 80.7%
    rows = {**{s: {"rank": i} for i, s in enumerate(names, 1)}, **healthy(3, start=7)}
    assert only(run(rows, held(**names), cfg), BUY) == []
    low = {s: 1900.0 for s in names}                                      # 11,400 = 76%
    (o,) = only(run(rows, held(**low), cfg), BUY)
    assert o.symbol == "C7" and o.dollars == pytest.approx(TARGET - 11400.0)   # only $1,350 of room


def test_a_capped_name_stays_capped_and_the_rest_is_cash(cfg):
    plan = run(healthy(3), {}, cfg)                                       # only 3 eligible names
    assert [o.dollars for o in only(plan, BUY)] == pytest.approx([CAP] * 3)
    assert sum(o.dollars for o in only(plan, BUY)) == pytest.approx(0.45 * EQUITY)   # 55% is cash


def test_the_refill_takes_names_in_rank_order_and_skips_ineligible_ones(cfg):
    rows = healthy(8)
    rows["C2"]["mom"] = -0.1                                              # fails E3
    buys = [o.symbol for o in only(run(rows, {}, cfg), BUY)]
    assert "C2" not in buys and buys[:2] == ["C1", "C3"]


def test_the_sector_cap_still_binds_the_refill(cfg):
    holdings = held(H1=2250, H2=2250)                                     # 30% of equity in Tech
    rows = {"H1": {"rank": 1}, "H2": {"rank": 2}, "C3": {"rank": 3}, "C4": {"rank": 4}}
    sectors = {"H1": "Tech", "H2": "Tech", "C3": "Tech", "C4": "Health"}
    buys = [o.symbol for o in only(run(rows, holdings, cfg, sectors=sectors), BUY)]
    assert buys == ["C4"]                                                 # .30 + .15 > .40 blocks C3


def test_ten_positions_at_most(cfg):
    holdings = held(**{f"H{i}": 1000.0 for i in range(1, 10)})           # nine held, 60% cash
    rows = {**{s: {"rank": i} for i, s in enumerate(holdings, 1)}, **healthy(4, start=10)}
    buys = only(run(rows, holdings, cfg), BUY)
    assert len(buys) == 1                                                 # the tenth slot only


def test_e7_drops_a_new_name_that_would_be_too_small(cfg):
    plan = run(healthy(8), {}, cfg, equity=7000.0)
    assert plan.e7_drops == 1                                             # 6 x $992 -> 5 x $1,050
    assert [o.dollars for o in only(plan, BUY)] == pytest.approx([1050.0] * 5)


# ------------------------------------------------------------- redeployment


def test_invested_is_measured_after_the_weeks_sells(cfg):
    """Six names at 84% invested: no refill. One is sold on the rank buffer, which
    leaves 70% - below 80% - so the proceeds are redeployed the same week."""
    holdings = held(H1=2100, H2=2100, H3=2100, H4=2100, H5=2100, H6=2100)
    rows = {"H1": {"rank": 30}, **{f"H{i}": {"rank": i} for i in range(2, 7)}, **healthy(3, start=7)}
    plan = run(rows, holdings, cfg)
    assert [(o.symbol, o.rule, o.fraction) for o in only(plan, SELL)] == [("H1", "X1", 1.0)]
    assert only(plan, BUY)


def test_a_name_sold_this_week_is_not_bought_back_this_week(cfg):
    """C1 falls 40% from its peak (L3) yet is still rank 1 with positive momentum.
    Without the guard it would be sold and immediately rebought."""
    holdings = {"C1": Holding(20.0, 2000.0, 100.0)}
    rows = {"C1": {"rank": 1, "close": 60.0}, **healthy(6, start=2)}
    plan = run(rows, holdings, cfg)
    assert [o.rule for o in only(plan, SELL)] == ["L3"]
    assert "C1" not in [o.symbol for o in only(plan, BUY)]


def test_break_a_stopped_out_name_is_bought_back_in_the_same_week(cfg):
    bad = load_mutant("src.strategy.plan",
                      "candidates = replace(signals, table=signals.table.drop(\n            index=list(fully_out), errors=\"ignore\"))",
                      "candidates = signals")
    holdings = {"C1": Holding(20.0, 2000.0, 100.0)}
    rows = {"C1": {"rank": 1, "close": 60.0}, **healthy(6, start=2)}
    assert "C1" not in [o.symbol for o in only(run(rows, holdings, cfg), BUY)]
    assert "C1" in [o.symbol for o in only(run(rows, holdings, cfg, mod=bad), BUY)]


# ------------------------------------------------------------------ the ladder


def test_ladder_sales_are_planned_as_fractions_with_the_levels_and_peak(cfg):
    holdings = {"L": Holding(30.0, 2640.0, 100.0)}                       # close 88: -12%
    rows = {"L": {"rank": 1, "close": 88.0}}
    (o,) = only(run(rows, holdings, cfg), SELL)
    assert (o.rule, o.levels, o.full_exit, o.peak) == ("L1", 1, False, 100.0)
    assert o.fraction == pytest.approx(1 / 3)


# --------------------------------------------------------------- cap-drift trim


def test_a_name_above_the_cap_is_trimmed_back_to_the_cap(cfg):
    holdings = held(BIG=3000.0)                                           # 20% of equity
    (o,) = only(run({"BIG": {"rank": 1}}, holdings, cfg), SELL)
    assert o.rule == CAP_TRIM and o.fraction == pytest.approx(0.25)       # sells $750
    assert only(run({"BIG": {"rank": 1}}, held(BIG=CAP), cfg), SELL) == []   # exactly at the cap: keep


def test_a_ladder_sale_and_a_cap_trim_on_the_same_name_do_not_oversell(cfg):
    holdings = {"X": Holding(40.0, 3520.0, 100.0)}                        # 23.5% of equity, close 88
    rows = {"X": {"rank": 1, "close": 88.0}}
    sells = only(run(rows, holdings, cfg), SELL)
    assert [o.rule for o in sells] == ["L1", CAP_TRIM]
    assert sum(o.fraction for o in sells) <= 1.0
    assert 3520.0 * (1 - sum(o.fraction for o in sells)) == pytest.approx(CAP)   # ends at the cap


def test_break_the_cap_trim_is_skipped(cfg):
    bad = load_mutant("src.strategy.plan", "if sym not in fully_out and excess > eps:", "if False:")
    holdings = held(BIG=3000.0)
    assert only(run({"BIG": {"rank": 1}}, holdings, cfg), SELL)
    assert only(run({"BIG": {"rank": 1}}, holdings, cfg, mod=bad), SELL) == []


# ------------------------------------------------------------------- defensive


def test_defensive_trims_every_position_pro_rata_to_the_defensive_target(cfg):
    holdings = held(**{f"H{i}": 1275.0 for i in range(1, 11)})            # 10 x 8.5% = 85%
    rows = {s: {"rank": i} for i, s in enumerate(holdings, 1)}
    plan = run(rows, holdings, cfg, mode=DEFENSIVE)
    sells = only(plan, SELL)
    assert len(sells) == 10 and {o.rule for o in sells} == {REGIME_TRIM}
    assert {round(o.fraction, 9) for o in sells} == {round(1 - 6000 / 12750, 9)}    # the same share of each
    assert sum(1275.0 * (1 - o.fraction) for o in sells) == pytest.approx(0.40 * EQUITY)
    assert only(plan, BUY) == []


def test_defensive_trim_only_when_invested_is_above_target_plus_the_band(cfg):
    rows = {f"H{i}": {"rank": i} for i in range(1, 5)}
    inside = held(H1=1675.0, H2=1675.0, H3=1675.0, H4=1675.0)             # 6,700 = 44.7% <= 45%
    assert only(run(rows, inside, cfg, mode=DEFENSIVE), SELL) == []
    outside = held(H1=1700.0, H2=1700.0, H3=1700.0, H4=1700.0)            # 6,800 = 45.3%
    assert only(run(rows, outside, cfg, mode=DEFENSIVE), SELL)


def test_defensive_never_buys_a_new_name_even_below_target(cfg):
    holdings = held(H1=1000.0)
    rows = {"H1": {"rank": 1}, **healthy(6, start=2)}
    plan = run(rows, holdings, cfg, mode=DEFENSIVE)
    assert only(plan, BUY) == []


def test_defensive_still_sells_on_the_ladder_and_rank_rules(cfg):
    holdings = {"H": Holding(20.0, 2000.0, 100.0)}
    plan = run({"H": {"rank": 30}}, holdings, cfg, mode=DEFENSIVE)
    assert [o.rule for o in only(plan, SELL)] == ["X1"]


def test_break_defensive_mode_buys_like_normal(cfg):
    bad = load_mutant(
        "src.strategy.plan",
        "        return plan                          # DEFENSIVE never buys: no new names, no top-ups"
        "\n\n    # 4. NORMAL: top-ups, then the refill\n    assert mode == NORMAL, mode",
        "        pass")
    holdings = held(H1=1000.0)
    rows = {"H1": {"rank": 1}, **healthy(6, start=2)}
    assert only(run(rows, holdings, cfg, mode=DEFENSIVE), BUY) == []
    assert only(run(rows, holdings, cfg, mode=DEFENSIVE, mod=bad), BUY)


# --------------------------------------------------------------------- top-ups


def topup_case(cfg, close, others=2200.0, **holding_kw):
    """P was partly sold (fired 1, prior peak 100) and now trades at `close`. Five
    other names hold `others` each; with 2,200 the book is 80% invested, so there
    is no refill and only the top-up could buy."""
    holdings = held(**{f"O{i}": others for i in range(1, 6)})
    holdings["P"] = Holding(10.0, 1000.0, 100.0, ladder_fired=1, topup_above=100.0, **holding_kw)
    rows = {"P": {"rank": 1, "close": close}, **{f"O{i}": {"rank": i + 1} for i in range(1, 6)}}
    return run(rows, holdings, cfg)


def test_a_partly_sold_name_is_topped_up_only_after_it_beats_its_prior_peak(cfg):
    assert only(topup_case(cfg, close=99.0), BUY) == []
    assert only(topup_case(cfg, close=100.0), BUY) == []                 # equal is not above
    (o,) = only(topup_case(cfg, close=101.0), BUY)
    assert (o.symbol, o.rule) == ("P", TOPUP)


def test_a_topup_is_limited_to_the_room_under_the_target(cfg):
    (o,) = only(topup_case(cfg, close=101.0), BUY)
    # 12,000 held, target 12,750: $750 of room. Its allocation (14.17%) would want $1,125.
    assert o.dollars == pytest.approx(750.0)


def test_a_topup_fills_to_its_allocation_when_there_is_room(cfg):
    holdings = held(O1=2000.0, O2=2000.0)
    holdings["P"] = Holding(10.0, 1000.0, 100.0, ladder_fired=1, topup_above=100.0)
    rows = {"P": {"rank": 1, "close": 101.0}, "O1": {"rank": 2}, "O2": {"rank": 3}, **healthy(0)}
    plan = run(rows, holdings, cfg)
    (o,) = only(plan, BUY, TOPUP)
    assert o.dollars == pytest.approx(CAP - 1000.0)                      # back to the 15% cap


def test_no_topup_in_defensive_mode(cfg):
    holdings = {"P": Holding(10.0, 1000.0, 100.0, ladder_fired=1, topup_above=100.0)}
    plan = run({"P": {"rank": 1, "close": 110.0}}, holdings, cfg, mode=DEFENSIVE)
    assert only(plan, BUY) == []


def test_a_name_sold_on_the_ladder_this_week_is_not_topped_up(cfg):
    """Prior peak 50, and it has since run to 120; today it is at 95 (-20.8%), so
    the second level sells. It is above the old peak, but it is being sold."""
    holdings = {"P": Holding(10.0, 950.0, 120.0, ladder_fired=1, topup_above=50.0)}
    plan = run({"P": {"rank": 1, "close": 95.0}}, holdings, cfg)
    assert [o.rule for o in only(plan, SELL)] == ["L2"]
    assert only(plan, BUY, TOPUP) == []


def test_break_a_name_is_topped_up_in_the_week_it_is_sold(cfg):
    bad = load_mutant("src.strategy.plan", "and sold[s] == ZERO}", "}")
    holdings = {"P": Holding(10.0, 950.0, 120.0, ladder_fired=1, topup_above=50.0)}
    rows = {"P": {"rank": 1, "close": 95.0}}
    assert only(run(rows, holdings, cfg), BUY, TOPUP) == []
    assert only(run(rows, holdings, cfg, mod=bad), BUY, TOPUP)


# --------------------------------------------------------------------- restores


def trimmed_book(cfg, mode=NORMAL, **kw):
    """Ten names the defensive filter trimmed to $600 (4%) each: 40% invested."""
    holdings = {f"H{i}": Holding(6.0, 600.0, 100.0, restore=True) for i in range(1, 11)}
    rows = {s: {"rank": i} for i, s in enumerate(holdings, 1)}
    return run(rows, holdings, cfg, mode=mode, **kw)


def test_names_trimmed_by_the_defensive_filter_come_back_when_it_does(cfg):
    """A full book has no free slot and the refill only buys names not held, so
    without this the trimmed names would stay at 4% for good."""
    plan = trimmed_book(cfg)
    buys = only(plan, BUY)
    assert len(buys) == 10 and {o.rule for o in buys} == {RESTORE}
    assert [o.dollars for o in buys] == pytest.approx([675.0] * 10)         # back to 8.5% each
    assert 6000.0 + sum(o.dollars for o in buys) == pytest.approx(TARGET)


def test_nothing_is_restored_while_the_filter_is_still_defensive(cfg):
    assert only(trimmed_book(cfg, mode=DEFENSIVE), BUY) == []


def test_a_restore_still_faces_the_entry_conditions(cfg):
    holdings = {"H1": Holding(6.0, 600.0, 100.0, restore=True)}
    plan = run({"H1": {"rank": 1, "vol": 0.9}}, holdings, cfg)             # E4: too volatile now
    assert only(plan, BUY) == []


# -------------------------------------------------------------------- re-entry


def test_a_name_sold_in_full_returns_only_above_its_50_day_sma_after_a_week(cfg):
    rows = {"C1": {"rank": 1, "close": 95.0, "sma": 90.0}, **healthy(6, start=2)}
    got = [(o.symbol, o.rule) for o in only(run(rows, {}, cfg, exit_weeks={"C1": 1}), BUY)]
    assert ("C1", REENTRY) in got
    below = {"C1": {"rank": 1, "close": 85.0, "sma": 90.0}, **healthy(6, start=2)}
    assert "C1" not in [o.symbol for o in only(run(below, {}, cfg, exit_weeks={"C1": 1}), BUY)]
    same_week = [o.symbol for o in only(run(rows, {}, cfg, exit_weeks={"C1": 0}), BUY)]
    assert "C1" not in same_week


def test_a_first_time_entry_is_labelled_entry_not_reentry(cfg):
    rows = {"C1": {"rank": 1, "close": 85.0, "sma": 90.0}, **healthy(6, start=2)}
    got = {o.symbol: o.rule for o in only(run(rows, {}, cfg), BUY)}
    assert got["C1"] == ENTRY                                            # never held: no SMA condition


# ---------------------------------------------------------------- fail closed


def test_a_sizing_failure_plans_no_buys_but_keeps_the_sells(cfg):
    """Ten names at the 9% floor would need 90% > the 85% target: infeasible."""
    tight = variant(cfg, sizing={"min_position_weight": 0.09})
    holdings = held(**{f"H{i}": 500.0 for i in range(1, 11)})
    rows = {**{f"H{i}": {"rank": i + 1} for i in range(1, 11)}, "H1": {"rank": 30},
            "C1": {"rank": 1}}
    plan = run(rows, holdings, tight)
    assert plan.sizing_failed and only(plan, BUY) == []
    assert [o.symbol for o in only(plan, SELL)] == ["H1"]
