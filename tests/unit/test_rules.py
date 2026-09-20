"""Rules E1-E9, X1-X4 and sizing (spec sections 5, 6, 8), and deliberate breaks."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
import yaml

from src.config import DEFAULT_CONFIG_PATH, Config
from src.strategy import rules
from src.strategy.rules import (
    evaluate_entries,
    evaluate_exits,
    size_new_positions,
    size_positions,
)
from src.strategy.signals import SignalFrame
from tests.mutants import load_mutant

NAN = float("nan")


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config(**yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")))


def variant(cfg: Config, **sections) -> Config:
    """Copy of cfg with e.g. entry={"max_new_per_rebalance": 3}."""
    new = cfg.model_copy(deep=True)
    for section, updates in sections.items():
        for key, value in updates.items():
            setattr(getattr(new, section), key, value)
    return new


def frame(rows: dict[str, dict], market_on: bool = True) -> SignalFrame:
    """SignalFrame from {symbol: {rank, mom, vol, close, atr}}; healthy defaults."""
    recs = {}
    for sym, r in rows.items():
        recs[sym] = {
            "close": r.get("close", 100.0), "mom_6m": NAN, "mom_12m": NAN,
            "blended_momentum": r.get("mom", 0.2), "vol_63": r.get("vol", 0.25),
            "score": NAN, "rank": r.get("rank", pd.NA), "atr_20": r.get("atr", 2.0),
        }
    table = pd.DataFrame.from_dict(recs, orient="index")
    table["rank"] = table["rank"].astype("Int64")
    table.index.name = "symbol"
    return SignalFrame(pd.Timestamp("2024-06-07"), market_on, table)


def ranked(n: int, **common) -> dict[str, dict]:
    """n healthy candidates C1..Cn with ranks 1..n."""
    return {f"C{i}": {"rank": i, **common} for i in range(1, n + 1)}


def sectors_of(rows, sector="S") -> dict[str, str]:
    return {s: sector for s in rows}


def distinct_sectors(n) -> dict[str, str]:
    """C1..Cn each in a sector of its own, so E8 never interferes."""
    return {f"C{i}": f"Sec{i}" for i in range(1, n + 1)}


def decisions(sf, cfg, held=(), sector_weights=None, ai=None, sectors=None):
    by = {d.symbol: d for d in evaluate_entries(
        sf, set(held), sector_weights or {}, ai or {}, cfg,
        sectors=sectors if sectors is not None else {s: "S" for s in sf.table.index})}
    return by


# ==================================================================== exits


def exit_of(cfg, sf, sym="H", high=100.0, atr=2.0):
    (d,) = evaluate_exits({sym: 10.0}, sf, {sym: high}, {sym: atr}, cfg)
    return d


def test_x1_exits_beyond_rank_exit_and_holds_at_the_boundary(cfg):
    assert exit_of(cfg, frame({"H": {"rank": 11}})).rule == "X1"
    d = exit_of(cfg, frame({"H": {"rank": 10}}))
    assert not d.should_exit and d.rule is None


def test_six_in_ten_out_buffer(cfg):
    """A held name at rank 8 is kept (X1 needs > 10) but the same name is not
    bought (E2 needs <= 6). The gap between the two is the anti-churn buffer."""
    sf = frame({"H": {"rank": 8}, "N": {"rank": 8}})
    assert not exit_of(cfg, sf).should_exit
    assert decisions(sf, cfg, held=[])["N"].blocked_by == ["E2"]
    for rank in range(cfg.entry.rank_threshold + 1, cfg.exit.rank_exit + 1):
        sf = frame({"H": {"rank": rank}})
        assert not exit_of(cfg, sf).should_exit


def test_x2_market_off_exits_a_healthy_position(cfg):
    d = exit_of(cfg, frame({"H": {"rank": 1}}, market_on=False))
    assert d.should_exit and d.rule == "X2"


def test_x2_switch_in_config(cfg):
    off = variant(cfg, exit={"exit_all_on_market_off": False})
    assert not exit_of(off, frame({"H": {"rank": 1}}, market_on=False)).should_exit


def test_x3_trailing_stop_is_strict_and_uses_three_atr(cfg):
    # high 100, ATR 5 -> stop = 100 - 3*5 = 85
    assert exit_of(cfg, frame({"H": {"rank": 1, "close": 84.9}}), high=100, atr=5).rule == "X3"
    assert not exit_of(cfg, frame({"H": {"rank": 1, "close": 85.0}}), high=100, atr=5).should_exit
    assert not exit_of(cfg, frame({"H": {"rank": 1, "close": 90.0}}), high=100, atr=5).should_exit


def test_x3_follows_the_position_high_not_todays_price(cfg):
    sf = frame({"H": {"rank": 1, "close": 120.0}})
    assert not exit_of(cfg, sf, high=125, atr=2).should_exit      # 125-6 = 119 < 120
    assert exit_of(cfg, sf, high=130, atr=2).rule == "X3"          # 130-6 = 124 > 120


def test_x4_negative_momentum_exits_zero_does_not(cfg):
    assert exit_of(cfg, frame({"H": {"rank": 1, "mom": -0.01}})).rule == "X4"
    assert not exit_of(cfg, frame({"H": {"rank": 1, "mom": 0.0}})).should_exit


def test_x4_switch_in_config(cfg):
    off = variant(cfg, exit={"exit_on_negative_momentum": False})
    assert not exit_of(off, frame({"H": {"rank": 1, "mom": -0.5}})).should_exit


def test_priority_reports_one_rule_and_lists_all_that_fired(cfg):
    sf = frame({"H": {"rank": 30, "mom": -0.1, "close": 50.0}}, market_on=False)
    d = exit_of(cfg, sf, high=100, atr=5)
    assert d.rule == "X2"
    assert all(code in d.detail for code in ("X1", "X2", "X3", "X4"))


def test_exits_fail_closed_when_a_check_cannot_be_evaluated(cfg):
    healthy = {"rank": 1}
    assert exit_of(cfg, frame({"H": healthy}), atr=NAN).rule == "X3"
    assert exit_of(cfg, frame({"H": {**healthy, "close": NAN}})).rule == "X3"
    (no_high,) = evaluate_exits({"H": 1.0}, frame({"H": healthy}), {}, {"H": 2.0}, cfg)
    assert no_high.rule == "X3"
    assert exit_of(cfg, frame({"H": {**healthy, "mom": NAN}})).rule == "X4"
    # no row at all (left the universe / no bar today)
    (gone,) = evaluate_exits({"H": 1.0}, frame({"OTHER": healthy}), {"H": 100.0}, {"H": 2.0}, cfg)
    assert gone.should_exit


def test_exits_return_one_decision_per_position_sorted(cfg):
    sf = frame({"B": {"rank": 1}, "A": {"rank": 20}})
    out = evaluate_exits({"B": 1.0, "A": 1.0}, sf, {"A": 100.0, "B": 100.0},
                         {"A": 2.0, "B": 2.0}, cfg)
    assert [(d.symbol, d.should_exit) for d in out] == [("A", True), ("B", False)]


# ================================================================== entries


def test_a_clean_top_ranked_candidate_is_eligible(cfg):
    d = decisions(frame({"N": {"rank": 1}}), cfg)["N"]
    assert d.eligible and d.blocked_by == [] and d.rank == 1


def test_held_names_are_not_candidates(cfg):
    assert "H" not in decisions(frame({"H": {"rank": 1}, "N": {"rank": 2}}), cfg, held=["H"])


def test_e1_market_off_blocks_everyone(cfg):
    out = decisions(frame(ranked(3), market_on=False), cfg)
    assert all(d.blocked_by == ["E1"] for d in out.values())


def test_e2_rank_boundary(cfg):
    lax = variant(cfg, entry={"max_new_per_rebalance": 10})
    out = decisions(frame(ranked(8)), lax, sectors=distinct_sectors(8))
    assert out["C6"].eligible
    assert "E2" in out["C7"].blocked_by and "E2" in out["C8"].blocked_by   # (E6 too: book is full)


def test_e3_needs_strictly_positive_momentum(cfg):
    for mom, blocked in ((0.01, False), (0.0, True), (-0.2, True), (NAN, True)):
        d = decisions(frame({"N": {"rank": 1, "mom": mom}}), cfg)["N"]
        assert ("E3" in d.blocked_by) is blocked, mom


def test_e4_volatility_ceiling_is_inclusive(cfg):
    for vol, blocked in ((0.80, False), (0.81, True), (NAN, True)):
        d = decisions(frame({"N": {"rank": 1, "vol": vol}}), cfg)["N"]
        assert ("E4" in d.blocked_by) is blocked, vol


def test_e5_veto(cfg):
    sf = frame({"N": {"rank": 1}})
    assert decisions(sf, cfg, ai={"N": True})["N"].blocked_by == ["E5"]
    assert decisions(sf, cfg, ai={"N": False})["N"].eligible
    # unavailable (None) or absent proceeds without the veto (spec section 7)
    assert decisions(sf, cfg, ai={"N": None})["N"].eligible
    assert decisions(sf, cfg, ai={})["N"].eligible


def test_e5_off_when_the_layer_is_disabled(cfg):
    off = variant(cfg, ai={"enabled": False})
    assert decisions(frame({"N": {"rank": 1}}), off, ai={"N": True})["N"].eligible


def test_e6_position_count_after_entry(cfg):
    held = [f"H{i}" for i in range(6)]
    sf = frame({"N": {"rank": 1}, **{h: {"rank": 20} for h in held}})
    assert decisions(sf, cfg, held=held)["N"].blocked_by == ["E6"]
    five = held[:5]
    assert decisions(sf, cfg, held=five)["N"].eligible                   # 6th is allowed


def test_e6_counts_new_entries_accepted_earlier_in_the_same_pass(cfg):
    lax = variant(cfg, entry={"max_new_per_rebalance": 10})
    held = [f"H{i}" for i in range(4)]
    sf = frame({**ranked(3), **{h: {"rank": 20} for h in held}})
    sectors = distinct_sectors(3) | {h: f"H{h}" for h in held}
    out = decisions(sf, lax, held=held, sectors=sectors)
    assert [out[s].eligible for s in ("C1", "C2", "C3")] == [True, True, False]
    assert out["C3"].blocked_by == ["E6"]                                # 4 held + 2 new = 6


def test_e8_sector_cap_boundary_and_unknown_sector(cfg):
    sf = frame({"N": {"rank": 1}})
    # 0.20 (cap-weight of the new name) + current sector weight, against 0.40
    assert decisions(sf, cfg, sector_weights={"S": 0.20})["N"].eligible          # = 0.40 allowed
    assert decisions(sf, cfg, sector_weights={"S": 0.21})["N"].blocked_by == ["E8"]
    assert decisions(sf, cfg, sector_weights={"OTHER": 0.9})["N"].eligible       # other sector
    assert decisions(sf, cfg, sectors={})["N"].blocked_by == ["E8"]              # unknown: fail closed
    assert decisions(sf, cfg, sectors={"N": None})["N"].blocked_by == ["E8"]
    assert decisions(sf, cfg, sectors=None) is not None


def test_e8_without_any_sector_map_blocks_everything(cfg):
    out = evaluate_entries(frame({"N": {"rank": 1}}), set(), {}, {}, cfg)
    assert out[0].blocked_by == ["E8"]


def test_e8_accumulates_within_one_pass_and_a_blocked_name_frees_the_slot(cfg):
    lax = variant(cfg, entry={"max_new_per_rebalance": 3})
    sf = frame(ranked(3))
    sectors = {"C1": "Tech", "C2": "Tech", "C3": "Tech"}
    out = decisions(sf, lax, sectors=sectors)
    assert [out[s].eligible for s in ("C1", "C2", "C3")] == [True, True, False]
    assert out["C3"].blocked_by == ["E8"]                                # 0.2 + 0.2 + 0.2 > 0.4

    sectors = {"C1": "Tech", "C2": "Tech", "C3": "Tech", "C4": "Health"}
    sf = frame(ranked(4))
    lax = variant(cfg, entry={"max_new_per_rebalance": 3, "rank_threshold": 6})
    out = decisions(sf, lax, sectors=sectors)
    # C3 was blocked by E8 and consumed no slot, so C4 still gets the third.
    assert out["C4"].eligible


def test_e9_caps_new_positions_per_rebalance(cfg):
    sectors = {f"C{i}": f"Sec{i}" for i in range(1, 6)}
    out = decisions(frame(ranked(5)), cfg, sectors=sectors)
    assert [out[f"C{i}"].eligible for i in range(1, 6)] == [True, True, False, False, False]
    assert out["C3"].blocked_by == ["E9"]


def test_e9_follows_config(cfg):
    one = variant(cfg, entry={"max_new_per_rebalance": 1})
    sectors = {f"C{i}": f"Sec{i}" for i in range(1, 4)}
    out = decisions(frame(ranked(3)), one, sectors=sectors)
    assert sum(d.eligible for d in out.values()) == 1


def test_a_blocked_candidate_does_not_consume_an_e9_slot(cfg):
    rows = ranked(4)
    rows["C1"]["mom"] = -0.1                                          # fails E3
    sectors = {f"C{i}": f"Sec{i}" for i in range(1, 5)}
    out = decisions(frame(rows), cfg, sectors=sectors)
    assert [out[f"C{i}"].eligible for i in range(1, 5)] == [False, True, True, False]


def test_unranked_symbols_are_never_candidates(cfg):
    out = decisions(frame({"N": {"rank": 1}, "U": {}}), cfg)
    assert list(out) == ["N"]


def test_every_failed_rule_is_listed(cfg):
    sf = frame({"N": {"rank": 9, "mom": -1.0, "vol": 2.0}}, market_on=False)
    d = decisions(sf, cfg, ai={"N": True}, sector_weights={"S": 0.5})["N"]
    assert d.blocked_by == ["E1", "E2", "E3", "E4", "E5", "E8"]


# ================================================================== sizing


@pytest.fixture(scope="module")
def small(cfg) -> Config:
    """covariance_window 4 so returns can be written out by hand."""
    return variant(cfg, sizing={"covariance_window": 4})


def rets(symbols, a, rows=4):
    """Every symbol earns +a, -a, +a, -a...: perfectly correlated, mean 0."""
    pattern = [a if i % 2 == 0 else -a for i in range(rows)]
    return pd.DataFrame({s: pattern for s in symbols}, dtype=float)


def names(n):
    return [f"N{i}" for i in range(n)]


def test_equal_vol_gives_equal_weights_and_stays_within_bounds(small):
    syms = names(6)
    out = size_positions(syms, pd.Series(0.25, index=syms), rets(syms, 0.001), 15000.0, small)
    assert out.to_numpy() == pytest.approx(15000 / 6)            # 16.7% each, fully invested
    assert out.sum() == pytest.approx(15000.0)


def test_lower_vol_gets_the_larger_weight(small):
    syms = ["LO", "HI"] + names(4)
    vol = pd.Series([0.15, 0.30, 0.25, 0.25, 0.25, 0.25], index=syms)
    out = size_positions(syms, vol, rets(syms, 0.001), 15000.0, small)
    assert out["LO"] > out["N0"] > out["HI"]


def test_clipping_holds_the_bounds_and_redistributes(small):
    syms = names(6)
    vol = pd.Series([0.1] * 5 + [0.6], index=syms)
    # raw 10,10,10,10,10,1.667: last is 3.2% -> clipped up to the 8% floor;
    # the other five share the remaining 92%: 18.4% each (under the 20% cap).
    out = size_positions(syms, vol, rets(syms, 0.0005), 10000.0, small) / 10000.0
    assert out["N5"] == pytest.approx(0.08)
    assert out[:5].to_numpy() == pytest.approx(0.184)


def test_cap_is_not_broken_by_renormalizing_with_few_names(small):
    """Spec 8 read literally (clip then renormalize) gives two names 50% each.
    The 20% cap must hold, and the rest of the book is cash."""
    syms = names(2)
    out = size_positions(syms, pd.Series(0.25, index=syms), rets(syms, 0.0005), 10000.0, small)
    assert out.to_numpy() == pytest.approx(2000.0)
    assert out.sum() == pytest.approx(4000.0)
    three = names(3)
    out3 = size_positions(three, pd.Series(0.25, index=three), rets(three, 0.0005), 10000.0, small)
    assert out3.to_numpy() == pytest.approx(2000.0)              # 3 x 20% = 60% invested


def test_vol_target_scales_down_hand_checked(small):
    """Six perfectly correlated names, returns +/-1%: sample stdev of
    [.01,-.01,.01,-.01] is .01*2/sqrt(3); portfolio vol (weights sum to 1) is
    that times sqrt(252) = 0.18330. Scalar = 0.15 / 0.18330 = 0.81830."""
    syms = names(6)
    out = size_positions(syms, pd.Series(0.25, index=syms), rets(syms, 0.01), 12000.0, small)
    port_vol = 0.01 * 2 / math.sqrt(3) * math.sqrt(252)
    scalar = 0.15 / port_vol
    assert port_vol == pytest.approx(0.18330, abs=1e-5)
    assert scalar == pytest.approx(0.81830, abs=1e-4)
    assert out.to_numpy() == pytest.approx(12000.0 / 6 * scalar)
    assert out.sum() < 12000.0                                     # deliberately under-invested


def test_sizing_never_scales_up_to_hit_the_target(small):
    """Two calm names at the 20% cap hold 40% of the book. Portfolio vol is far
    below the 15% target, and the scalar still must not exceed 1."""
    syms = names(2)
    out = size_positions(syms, pd.Series(0.10, index=syms), rets(syms, 0.0001), 10000.0, small)
    assert out.sum() == pytest.approx(4000.0)
    six = names(6)
    out6 = size_positions(six, pd.Series(0.10, index=six), rets(six, 0.0001), 10000.0, small)
    assert out6.sum() == pytest.approx(10000.0)                     # no leverage either


def test_gross_exposure_never_exceeds_the_limit_even_if_scale_up_were_enabled(small):
    syms = names(2)
    up = variant(small, sizing={"scale_up_allowed": True})
    out = size_positions(syms, pd.Series(0.10, index=syms), rets(syms, 0.0001), 10000.0, up)
    assert out.sum() <= 10000.0 * up.risk.max_gross_exposure + 1e-9


def test_sizing_refuses_bad_inputs(small):
    syms = names(3)
    good = rets(syms, 0.001)
    with pytest.raises(ValueError, match="volatility"):
        size_positions(syms, pd.Series([0.2, NAN, 0.2], index=syms), good, 1000.0, small)
    with pytest.raises(ValueError, match="volatility"):
        size_positions(syms, pd.Series([0.2, 0.0, 0.2], index=syms), good, 1000.0, small)
    gappy = good.copy()
    gappy.iloc[-1, 0] = NAN
    with pytest.raises(ValueError, match="covariance window"):
        size_positions(syms, pd.Series(0.2, index=syms), gappy, 1000.0, small)
    with pytest.raises(ValueError, match="covariance window"):
        size_positions(syms, pd.Series(0.2, index=syms), good.iloc[:2], 1000.0, small)
    assert size_positions([], pd.Series(dtype=float), good, 1000.0, small).empty


# ------------------------------------------------------------------- E7


def test_e7_drops_new_positions_under_the_minimum_smallest_first(small):
    """$5,000 across six names is $833 each: too small. Dropping one new name
    leaves five at exactly $1,000, which is enough (the minimum is inclusive)."""
    syms = names(6)
    held, new = syms[:4], syms[4:]
    targets, dropped = size_new_positions(
        syms, new, pd.Series(0.25, index=syms), rets(syms, 0.0005), 5000.0, small)
    assert dropped == ["N4"]                                        # tie -> symbol order
    assert list(targets.index) == held + ["N5"]
    assert targets.to_numpy() == pytest.approx(1000.0)


def test_e7_never_drops_an_existing_position(small):
    syms = names(3)
    targets, dropped = size_new_positions(
        syms, ["N2"], pd.Series(0.25, index=syms), rets(syms, 0.0005), 2000.0, small)
    assert dropped == ["N2"]
    assert list(targets.index) == ["N0", "N1"]                       # held stay, though small
    assert (targets < small.sizing.min_position_value).all()


def test_e7_no_drops_when_everything_clears(small):
    syms = names(6)
    targets, dropped = size_new_positions(
        syms, syms[4:], pd.Series(0.25, index=syms), rets(syms, 0.0005), 15000.0, small)
    assert dropped == [] and len(targets) == 6


# ============================================================ deliberate breaks
#
# Each test loads rules.py with ONE line changed and shows the scenario that
# guards that line gives a different answer. If a break did not change the
# answer, the corresponding test above would be guarding nothing.


def mutant_exit(cfg, sf, mod, high=100.0, atr=2.0):
    (d,) = mod.evaluate_exits({"H": 1.0}, sf, {"H": high}, {"H": atr}, cfg)
    return d


def test_break_x1_off_by_one(cfg):
    bad = load_mutant("src.strategy.rules", "rank > cfg.exit.rank_exit", "rank >= cfg.exit.rank_exit")
    sf = frame({"H": {"rank": 10}})
    assert not exit_of(cfg, sf).should_exit and mutant_exit(cfg, sf, bad).should_exit


def test_break_no_buffer_exit_at_the_entry_rank(cfg):
    """Using the entry threshold for exits removes the 6-in/10-out buffer."""
    bad = load_mutant("src.strategy.rules", "rank > cfg.exit.rank_exit",
                      "rank > cfg.entry.rank_threshold")
    sf = frame({"H": {"rank": 8}})
    assert not exit_of(cfg, sf).should_exit and mutant_exit(cfg, sf, bad).should_exit


def test_break_stop_uses_one_atr_instead_of_three(cfg):
    bad = load_mutant("src.strategy.rules", "high - cfg.exit.trailing_stop_atr * a", "high - a")
    sf = frame({"H": {"rank": 1, "close": 90.0}})
    assert not exit_of(cfg, sf, high=100, atr=5).should_exit
    assert mutant_exit(cfg, sf, bad, high=100, atr=5).should_exit


def test_break_x2_ignored(cfg):
    bad = load_mutant("src.strategy.rules", "and not signals.market_on:\n            fired[\"X2\"]",
                      "and False:\n            fired[\"X2\"]")
    sf = frame({"H": {"rank": 1}}, market_on=False)
    assert exit_of(cfg, sf).should_exit and not mutant_exit(cfg, sf, bad).should_exit


def test_break_x4_fires_at_zero(cfg):
    bad = load_mutant("src.strategy.rules", "elif momentum < ZERO", "elif momentum <= ZERO")
    sf = frame({"H": {"rank": 1, "mom": 0.0}})
    assert not exit_of(cfg, sf).should_exit and mutant_exit(cfg, sf, bad).should_exit


def test_break_exits_no_longer_fail_closed(cfg):
    bad = load_mutant("src.strategy.rules",
                      'if _missing(close) or _missing(high) or _missing(a):',
                      'if False:')
    sf = frame({"H": {"rank": 1}})
    assert exit_of(cfg, sf, atr=NAN).should_exit
    assert not mutant_exit(cfg, sf, bad, atr=NAN).should_exit


def test_break_e9_cap_off_by_one(cfg):
    bad = load_mutant("src.strategy.rules", "new_count >= cfg.entry.max_new_per_rebalance",
                      "new_count > cfg.entry.max_new_per_rebalance")
    sectors = {f"C{i}": f"Sec{i}" for i in range(1, 6)}
    sf = frame(ranked(5))
    good = sum(d.eligible for d in decisions(sf, cfg, sectors=sectors).values())
    out = bad.evaluate_entries(sf, set(), {}, {}, cfg, sectors=sectors)
    assert good == 2 and sum(d.eligible for d in out) == 3


def test_break_e8_uses_a_strict_cap(cfg):
    bad = load_mutant("src.strategy.rules",
                      "sector_now.get(sector, ZERO) + weight > cfg.risk.max_sector_weight",
                      "sector_now.get(sector, ZERO) + weight >= cfg.risk.max_sector_weight")
    sf = frame({"N": {"rank": 1}})
    out = bad.evaluate_entries(sf, set(), {"S": 0.20}, {}, cfg, sectors={"N": "S"})
    assert decisions(sf, cfg, sector_weights={"S": 0.20})["N"].eligible
    assert out[0].blocked_by == ["E8"]


def test_break_e8_treats_unknown_sector_as_fine(cfg):
    bad = load_mutant("src.strategy.rules", "if sector is None or sector_now",
                      "if sector is not None and sector_now")
    sf = frame({"N": {"rank": 1}})
    out = bad.evaluate_entries(sf, set(), {}, {}, cfg, sectors={})
    assert decisions(sf, cfg, sectors={})["N"].blocked_by == ["E8"]
    assert out[0].eligible


def test_break_e3_allows_zero_momentum(cfg):
    bad = load_mutant("src.strategy.rules", 'row["blended_momentum"] > cfg.entry.min_momentum',
                      'row["blended_momentum"] >= cfg.entry.min_momentum')
    sf = frame({"N": {"rank": 1, "mom": 0.0}})
    out = bad.evaluate_entries(sf, set(), {}, {}, cfg, sectors={"N": "S"})
    assert "E3" in decisions(sf, cfg)["N"].blocked_by and out[0].eligible


def test_break_sizing_scales_up(small):
    bad = load_mutant("src.strategy.rules", "if not sz.scale_up_allowed:", "if False:")
    syms = names(2)
    args = (syms, pd.Series(0.10, index=syms), rets(syms, 0.0001), 10000.0, small)
    assert size_positions(*args).sum() == pytest.approx(4000.0)
    assert bad.size_positions(*args).sum() > 4000.0                  # would lever up toward 15% vol


def test_break_sizing_renormalizes_after_clipping(small):
    """Reintroduce the literal spec reading: the 20% cap is silently exceeded."""
    bad = load_mutant(
        "src.strategy.rules",
        "weights = _bounded_weights(FULL_WEIGHT / v, sz.min_position_weight, sz.max_position_weight)",
        "weights = (lambda w: w.clip(sz.min_position_weight, sz.max_position_weight) "
        "/ w.clip(sz.min_position_weight, sz.max_position_weight).sum())(FULL_WEIGHT / v)")
    syms = names(2)
    args = (syms, pd.Series(0.25, index=syms), rets(syms, 0.0005), 10000.0, small)
    assert (size_positions(*args) <= 2000.0 + 1e-6).all()
    assert (bad.size_positions(*args) > 2000.0).any()


def test_break_min_position_value_not_enforced(small):
    bad = load_mutant("src.strategy.rules", "and not targets[s] >= cfg.sizing.min_position_value]",
                      "and False]")
    syms = names(6)
    args = (syms, syms[4:], pd.Series(0.25, index=syms), rets(syms, 0.0005), 5000.0, small)
    assert size_new_positions(*args)[1] == ["N4"]
    assert bad.size_new_positions(*args)[1] == []
