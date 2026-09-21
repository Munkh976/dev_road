"""Rules X1, X4, the laddered stop, E1-E8, R1, T1 and sizing (spec sections 5, 6, 8),
and deliberate breaks."""

from __future__ import annotations

import pandas as pd
import pytest
import yaml

from src.config import DEFAULT_CONFIG_PATH, Config
from src.strategy.rules import (
    evaluate_entries,
    evaluate_exits,
    evaluate_topups,
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
    """Copy of cfg with e.g. entry={"rank_threshold": 3}."""
    new = cfg.model_copy(deep=True)
    for section, updates in sections.items():
        for key, value in updates.items():
            setattr(getattr(new, section), key, value)
    return new


def frame(rows: dict[str, dict], market_on: bool = True) -> SignalFrame:
    """SignalFrame from {symbol: {rank, mom, vol, close, atr, sma}}; healthy defaults."""
    recs = {}
    for sym, r in rows.items():
        recs[sym] = {
            "close": r.get("close", 100.0), "mom_6m": NAN, "mom_12m": NAN,
            "blended_momentum": r.get("mom", 0.2), "vol_63": r.get("vol", 0.25),
            "score": NAN, "rank": r.get("rank", pd.NA), "atr_20": r.get("atr", 2.0),
            "sma_reentry": r.get("sma", 90.0),
        }
    table = pd.DataFrame.from_dict(recs, orient="index")
    table["rank"] = table["rank"].astype("Int64")
    table.index.name = "symbol"
    return SignalFrame(pd.Timestamp("2024-06-07"), market_on, table)


def ranked(n: int, **common) -> dict[str, dict]:
    """n healthy candidates C1..Cn with ranks 1..n."""
    return {f"C{i}": {"rank": i, **common} for i in range(1, n + 1)}


def distinct_sectors(n) -> dict[str, str]:
    """C1..Cn each in a sector of its own, so E8 never interferes."""
    return {f"C{i}": f"Sec{i}" for i in range(1, n + 1)}


def decisions(sf, cfg, held=(), sector_weights=None, ai=None, sectors=None,
              regime_normal=True, exit_weeks=None):
    return {d.symbol: d for d in evaluate_entries(
        sf, set(held), sector_weights or {}, ai or {}, cfg, regime_normal=regime_normal,
        sectors=sectors if sectors is not None else {s: "S" for s in sf.table.index},
        exit_weeks=exit_weeks)}


# ==================================================================== exits


def exit_of(cfg, sf, sym="H", high=100.0, fired=0):
    (d,) = evaluate_exits({sym: 10.0}, sf, {sym: high}, {sym: fired}, cfg)
    return d


def test_x1_exits_beyond_rank_exit_and_holds_at_the_boundary(cfg):
    d = exit_of(cfg, frame({"H": {"rank": 17}}))
    assert d.full_exit and d.rule == "X1" and d.sell_fraction == 1.0
    d = exit_of(cfg, frame({"H": {"rank": 16}}))
    assert not d.should_sell and d.rule is None


def test_ten_in_sixteen_out_buffer(cfg):
    """A held name at rank 12 is kept (X1 needs > 16) but the same name is not
    bought (E2 needs <= 10). The gap between the two is the anti-churn buffer."""
    sf = frame({"H": {"rank": 12}, "N": {"rank": 12}})
    assert not exit_of(cfg, sf).should_sell
    assert decisions(sf, cfg)["N"].blocked_by == ["E2"]
    for rank in range(cfg.entry.rank_threshold + 1, cfg.exit.rank_exit + 1):
        assert not exit_of(cfg, frame({"H": {"rank": rank}})).should_sell


def test_the_market_filter_no_longer_sells_a_position(cfg):
    """v1's X2 sold everything when SPY was below its SMA. v2 grades that in
    regime.py and plan.py; the rules never see market_on for a held name."""
    d = exit_of(cfg, frame({"H": {"rank": 1}}, market_on=False))
    assert not d.should_sell


def test_x4_negative_momentum_exits_zero_does_not(cfg):
    assert exit_of(cfg, frame({"H": {"rank": 1, "mom": -0.01}})).rule == "X4"
    assert not exit_of(cfg, frame({"H": {"rank": 1, "mom": 0.0}})).should_sell


def test_x4_switch_in_config(cfg):
    off = variant(cfg, exit={"exit_on_negative_momentum": False})
    assert not exit_of(off, frame({"H": {"rank": 1, "mom": -0.5}})).should_sell


# ------------------------------------------------------------------- ladder


def ladder(cfg, close, high=100.0, fired=0):
    return exit_of(cfg, frame({"H": {"rank": 1, "close": close}}), high=high, fired=fired)


def test_ladder_levels_sell_thirds_at_12_20_28_percent_below_the_peak(cfg):
    assert cfg.exit.ladder.drawdowns == [0.12, 0.20, 0.28]
    assert not ladder(cfg, 88.5).should_sell                   # 11.5% down
    l1 = ladder(cfg, 88.0)                                     # exactly -12%: the level is inclusive
    assert (l1.rule, l1.levels, l1.full_exit) == ("L1", 1, False)
    assert l1.sell_fraction == pytest.approx(1 / 3)
    assert ladder(cfg, 80.5).rule == "L1"                      # still only level 1
    l2 = ladder(cfg, 80.0, fired=1)                            # -20%, level 1 already used
    assert (l2.rule, l2.levels) == ("L2", 1)
    assert l2.sell_fraction == pytest.approx(1 / 2)            # half of what is left = a third of the original
    l3 = ladder(cfg, 72.0, fired=2)                            # -28%: the rest
    assert (l3.rule, l3.full_exit, l3.sell_fraction) == ("L3", True, 1.0)


def test_the_three_thirds_add_up_to_the_original_position(cfg):
    shares = 90.0
    for close, fired in ((88.0, 0), (80.0, 1), (72.0, 2)):
        d = ladder(cfg, close, fired=fired)
        sold = d.sell_fraction * shares
        assert sold == pytest.approx(30.0)
        shares -= sold
    assert shares == pytest.approx(0.0)


def test_each_level_fires_at_most_once_per_position(cfg):
    assert not ladder(cfg, 87.0, fired=1).should_sell          # -13%: level 1 is spent
    assert not ladder(cfg, 79.9, fired=2).should_sell          # -20.1%: levels 1 and 2 are spent


def test_a_gap_through_several_levels_sells_them_together(cfg):
    d = ladder(cfg, 79.0)                                      # -21%: L1 and L2 in one week
    assert (d.rule, d.levels) == ("L2", 2) and d.sell_fraction == pytest.approx(2 / 3)
    d = ladder(cfg, 79.0, fired=1)
    assert (d.rule, d.levels) == ("L2", 1) and d.sell_fraction == pytest.approx(1 / 2)
    d = ladder(cfg, 50.0)                                      # -50%: all three
    assert d.full_exit and d.rule == "L3"


def test_the_stop_is_measured_from_the_peak_not_from_entry(cfg):
    """A name bought at 100 that ran to 150 and is now 130 is up 30% on entry but
    13.3% below its peak: level 1 fires. Anchored on the entry price it would not."""
    d = exit_of(cfg, frame({"H": {"rank": 1, "close": 130.0}}), high=150.0)
    assert d.rule == "L1"
    assert not exit_of(cfg, frame({"H": {"rank": 1, "close": 130.0}}), high=130.0).should_sell


def test_ladder_priority_full_exit_rules_beat_a_partial(cfg):
    d = exit_of(cfg, frame({"H": {"rank": 1, "close": 87.0, "mom": -0.1}}))
    assert d.rule == "X4" and d.full_exit                      # momentum inverted: sell it all
    d = exit_of(cfg, frame({"H": {"rank": 30, "close": 50.0, "mom": -0.1}}))
    assert d.rule == "L3"                                      # disaster stop is reported first
    assert all(code in d.detail for code in ("L3", "X4", "X1"))


def test_exits_fail_closed_when_a_check_cannot_be_evaluated(cfg):
    healthy = {"rank": 1}
    assert exit_of(cfg, frame({"H": {**healthy, "close": NAN}})).rule == "L3"
    (no_high,) = evaluate_exits({"H": 1.0}, frame({"H": healthy}), {}, {}, cfg)
    assert no_high.rule == "L3" and no_high.full_exit
    assert exit_of(cfg, frame({"H": {**healthy, "mom": NAN}})).rule == "X4"
    (gone,) = evaluate_exits({"H": 1.0}, frame({"OTHER": healthy}), {"H": 100.0}, {}, cfg)
    assert gone.full_exit                                       # no row: left the universe


def test_exits_return_one_decision_per_position_sorted(cfg):
    sf = frame({"B": {"rank": 1}, "A": {"rank": 20}})
    out = evaluate_exits({"B": 1.0, "A": 1.0}, sf, {"A": 100.0, "B": 100.0}, {}, cfg)
    assert [(d.symbol, d.should_sell) for d in out] == [("A", True), ("B", False)]


# ================================================================== entries


def test_a_clean_top_ranked_candidate_is_eligible(cfg):
    d = decisions(frame({"N": {"rank": 1}}), cfg)["N"]
    assert d.eligible and d.blocked_by == [] and d.rank == 1


def test_held_names_are_not_candidates(cfg):
    assert "H" not in decisions(frame({"H": {"rank": 1}, "N": {"rank": 2}}), cfg, held=["H"])


def test_e1_defensive_mode_blocks_everyone_and_market_on_alone_does_not(cfg):
    out = decisions(frame(ranked(3)), cfg, regime_normal=False, sectors=distinct_sectors(3))
    assert all(d.blocked_by == ["E1"] for d in out.values())
    # a raw SPY reading below its SMA is not the regime: only the confirmed mode is
    out = decisions(frame(ranked(3), market_on=False), cfg, regime_normal=True,
                    sectors=distinct_sectors(3))
    assert all(d.eligible for d in out.values())


def test_e2_rank_boundary_is_ten(cfg):
    out = decisions(frame(ranked(12)), cfg, sectors=distinct_sectors(12))
    assert out["C10"].eligible
    assert "E2" in out["C11"].blocked_by and "E2" in out["C12"].blocked_by


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
    assert decisions(sf, cfg, ai={"N": None})["N"].eligible        # unavailable: proceed (spec 7)
    assert decisions(sf, cfg, ai={})["N"].eligible


def test_e5_off_when_the_layer_is_disabled(cfg):
    off = variant(cfg, ai={"enabled": False})
    assert decisions(frame({"N": {"rank": 1}}), off, ai={"N": True})["N"].eligible


def test_e6_ten_positions(cfg):
    assert cfg.risk.max_positions == 10
    held = [f"H{i}" for i in range(10)]
    sf = frame({"N": {"rank": 1}, **{h: {"rank": 20} for h in held}})
    assert decisions(sf, cfg, held=held)["N"].blocked_by == ["E6"]
    assert decisions(sf, cfg, held=held[:9])["N"].eligible              # the 10th is allowed


def test_e6_counts_new_entries_accepted_earlier_in_the_same_pass(cfg):
    held = [f"H{i}" for i in range(8)]
    sf = frame({**ranked(3), **{h: {"rank": 20} for h in held}})
    sectors = distinct_sectors(3) | {h: f"H{h}" for h in held}
    out = decisions(sf, cfg, held=held, sectors=sectors)
    assert [out[s].eligible for s in ("C1", "C2", "C3")] == [True, True, False]
    assert out["C3"].blocked_by == ["E6"]                               # 8 held + 2 new = 10


def test_e8_sector_cap_boundary_and_unknown_sector(cfg):
    sf = frame({"N": {"rank": 1}})
    # 0.15 (cap-weight of the new name) + current sector weight, against 0.40
    assert decisions(sf, cfg, sector_weights={"S": 0.25})["N"].eligible          # = 0.40 allowed
    assert decisions(sf, cfg, sector_weights={"S": 0.26})["N"].blocked_by == ["E8"]
    assert decisions(sf, cfg, sector_weights={"OTHER": 0.9})["N"].eligible       # other sector
    assert decisions(sf, cfg, sectors={})["N"].blocked_by == ["E8"]              # unknown: fail closed
    assert decisions(sf, cfg, sectors={"N": None})["N"].blocked_by == ["E8"]


def test_e8_without_any_sector_map_blocks_everything(cfg):
    out = evaluate_entries(frame({"N": {"rank": 1}}), set(), {}, {}, cfg, regime_normal=True)
    assert out[0].blocked_by == ["E8"]


def test_e8_accumulates_within_one_pass_and_a_blocked_name_frees_the_slot(cfg):
    sf = frame(ranked(4))
    sectors = {"C1": "Tech", "C2": "Tech", "C3": "Tech", "C4": "Health"}
    out = decisions(sf, cfg, sectors=sectors)
    assert [out[s].eligible for s in ("C1", "C2", "C3", "C4")] == [True, True, False, True]
    assert out["C3"].blocked_by == ["E8"]                                # .15 + .15 + .15 > .40


def test_there_is_no_cap_on_new_names_per_week(cfg):
    """v1's E9 allowed two. Ten eligible names in one pass are all eligible."""
    out = decisions(frame(ranked(10)), cfg, sectors=distinct_sectors(10))
    assert all(d.eligible for d in out.values())
    assert not hasattr(cfg.entry, "max_new_per_rebalance")


def test_a_blocked_candidate_does_not_consume_a_slot(cfg):
    rows = ranked(4)
    rows["C1"]["mom"] = -0.1                                              # fails E3
    out = decisions(frame(rows), cfg, sectors=distinct_sectors(4))
    assert [out[f"C{i}"].eligible for i in range(1, 5)] == [False, True, True, True]


def test_unranked_symbols_are_never_candidates(cfg):
    out = decisions(frame({"N": {"rank": 1}, "U": {}}), cfg)
    assert list(out) == ["N"]


def test_every_failed_rule_is_listed(cfg):
    sf = frame({"N": {"rank": 12, "mom": -1.0, "vol": 2.0}})
    d = decisions(sf, cfg, ai={"N": True}, sector_weights={"S": 0.5}, regime_normal=False)["N"]
    assert d.blocked_by == ["E1", "E2", "E3", "E4", "E5", "E8"]


# -------------------------------------------------------------------- R1


def test_r1_only_applies_to_a_name_that_was_sold_in_full(cfg):
    sf = frame({"N": {"rank": 1, "close": 80.0, "sma": 90.0}})           # below its 50-day SMA
    assert decisions(sf, cfg)["N"].eligible                              # never held: no R1
    assert decisions(sf, cfg, exit_weeks={"N": 5})["N"].blocked_by == ["R1"]


def test_r1_needs_the_close_above_the_50_day_sma(cfg):
    ok = frame({"N": {"rank": 1, "close": 91.0, "sma": 90.0}})
    assert decisions(ok, cfg, exit_weeks={"N": 3})["N"].eligible
    for close in (90.0, 89.0, NAN):
        sf = frame({"N": {"rank": 1, "close": close, "sma": 90.0}})
        assert "R1" in decisions(sf, cfg, exit_weeks={"N": 3})["N"].blocked_by, close
    assert "R1" in decisions(frame({"N": {"rank": 1, "sma": NAN}}), cfg,
                             exit_weeks={"N": 3})["N"].blocked_by         # no SMA: fail closed


def test_r1_needs_at_least_one_week_since_the_exit(cfg):
    sf = frame({"N": {"rank": 1, "close": 95.0, "sma": 90.0}})
    assert "R1" in decisions(sf, cfg, exit_weeks={"N": 0})["N"].blocked_by
    assert decisions(sf, cfg, exit_weeks={"N": 1})["N"].eligible


def test_r1_needs_rank_within_the_reentry_limit(cfg):
    tight = variant(cfg, reentry={"max_rank": 3})
    sf = frame({"N": {"rank": 4, "close": 95.0, "sma": 90.0}})
    assert decisions(sf, tight, exit_weeks={"N": 4})["N"].blocked_by == ["R1"]
    assert decisions(sf, cfg, exit_weeks={"N": 4})["N"].eligible


# ------------------------------------------------------------------ top-ups


def topups(cfg, rows, partials, weights=None, sector_weights=None, regime_normal=True, sectors=None):
    sf = frame(rows)
    return {d.symbol: d for d in evaluate_topups(
        sf, partials, weights or {s: 0.05 for s in partials}, sector_weights or {}, {}, cfg,
        regime_normal=regime_normal,
        sectors=sectors if sectors is not None else {s: "S" for s in rows})}


def test_t1_no_topup_until_the_close_beats_the_prior_peak(cfg):
    below = topups(cfg, {"P": {"rank": 1, "close": 100.0}}, {"P": 100.0})["P"]
    assert below.blocked_by == ["T1"]                                    # equal is not "above"
    above = topups(cfg, {"P": {"rank": 1, "close": 100.5}}, {"P": 100.0})["P"]
    assert above.eligible
    assert topups(cfg, {"P": {"rank": 1, "close": NAN}}, {"P": 100.0})["P"].blocked_by == ["T1"]


def test_topups_face_the_entry_conditions_too(cfg):
    rows = {"P": {"rank": 12, "close": 110.0, "mom": -0.1, "vol": 0.9}}
    d = topups(cfg, rows, {"P": 100.0}, regime_normal=False)["P"]
    assert d.blocked_by == ["E1", "E2", "E3", "E4"]


def test_topups_respect_the_sector_cap_and_skip_e6(cfg):
    rows = {"P": {"rank": 1, "close": 110.0}}
    # room = 0.15 - 0.05 = 0.10; 0.30 + 0.10 = 0.40 is allowed, 0.31 + 0.10 is not
    assert topups(cfg, rows, {"P": 100.0}, sector_weights={"S": 0.30})["P"].eligible
    assert topups(cfg, rows, {"P": 100.0}, sector_weights={"S": 0.31})["P"].blocked_by == ["E8"]
    # already held, so a full book does not block a top-up (there is no E6 here)
    assert "E6" not in topups(cfg, rows, {"P": 100.0})["P"].blocked_by


# ================================================================== sizing


def names(n):
    return [f"N{i}" for i in range(n)]


def vols(syms, v=0.25):
    return pd.Series(v, index=syms)


def test_equal_vol_gives_equal_weights_summing_to_the_target(cfg):
    syms = names(10)
    out = size_positions(syms, vols(syms), 15000.0, 0.85, cfg)
    assert out.to_numpy() == pytest.approx(15000 * 0.085)              # 8.5% each
    assert out.sum() == pytest.approx(0.85 * 15000)


def test_lower_vol_gets_the_larger_weight(cfg):
    syms = ["LO", "HI"] + names(6)
    vol = pd.Series([0.15, 0.30] + [0.25] * 6, index=syms)
    out = size_positions(syms, vol, 15000.0, 0.85, cfg)
    assert out["LO"] > out["N0"] > out["HI"]
    assert out.sum() == pytest.approx(0.85 * 15000)                    # nothing clipped: fills the target


def test_a_capped_name_stays_capped_and_the_rest_share_the_remainder(cfg):
    syms = names(6)
    vol = pd.Series([0.1] * 5 + [0.6], index=syms)
    # raw shares put five names near 16.5% (over the 15% cap) and the last near
    # 2.7% (under the 5% floor). Pinned at the bounds: 5 x 15% + 5% = 80%, and the
    # 5% left over is cash, NOT redistributed over the cap.
    out = size_positions(syms, vol, 10000.0, 0.85, cfg) / 10000.0
    assert out[:5].to_numpy() == pytest.approx(0.15)
    assert out["N5"] == pytest.approx(0.05)
    assert out.sum() == pytest.approx(0.80)


def test_cap_holds_with_few_names_and_the_rest_is_cash(cfg):
    syms = names(2)
    out = size_positions(syms, vols(syms), 10000.0, 0.85, cfg)
    assert out.to_numpy() == pytest.approx(1500.0)                     # 15% each, not 42.5%
    assert out.sum() == pytest.approx(3000.0)
    three = names(3)
    assert size_positions(three, vols(three), 10000.0, 0.85, cfg).sum() == pytest.approx(4500.0)


def test_sizing_never_exceeds_the_target_or_gross_exposure(cfg):
    syms = names(10)
    assert size_positions(syms, vols(syms), 10000.0, 0.85, cfg).sum() <= 8500.0 + 1e-6
    # asking for more than the no-leverage limit is capped at it
    six = names(6)
    out = size_positions(six, vols(six), 10000.0, 1.5, cfg)
    assert out.sum() <= 10000.0 * cfg.risk.max_gross_exposure + 1e-6


def test_the_floor_cannot_be_met_when_the_target_is_too_small(cfg):
    syms = names(10)                                                    # 10 x 5% = 50% > 40%
    with pytest.raises(ValueError, match="infeasible"):
        size_positions(syms, vols(syms), 10000.0, 0.40, cfg)


def test_sizing_refuses_bad_inputs(cfg):
    syms = names(3)
    with pytest.raises(ValueError, match="volatility"):
        size_positions(syms, pd.Series([0.2, NAN, 0.2], index=syms), 1000.0, 0.85, cfg)
    with pytest.raises(ValueError, match="volatility"):
        size_positions(syms, pd.Series([0.2, 0.0, 0.2], index=syms), 1000.0, 0.85, cfg)
    assert size_positions([], pd.Series(dtype=float), 1000.0, 0.85, cfg).empty


# ------------------------------------------------------------- refill sizing


def test_new_names_are_sized_from_the_allocation_within_the_room(cfg):
    book = ["H1", "H2", "N1", "N2", "N3"]
    buys, dropped = size_new_positions(
        book, ["N1", "N2", "N3"], [], {"H1": 1500.0, "H2": 1500.0}, vols(book), 15000.0, 0.85, cfg)
    assert dropped == [] and set(buys) == {"N1", "N2", "N3"}
    assert list(buys.values()) == pytest.approx([2250.0] * 3)          # 15% cap each


def test_buys_are_scaled_so_the_book_never_passes_the_target(cfg):
    book = ["H", "N1", "N2"]
    buys, _ = size_new_positions(
        book, ["N1", "N2"], [], {"H": 10000.0}, vols(book), 15000.0, 0.85, cfg)
    assert sum(buys.values()) == pytest.approx(0.85 * 15000 - 10000)    # only $2,750 of room
    assert buys["N1"] == pytest.approx(buys["N2"])


def test_no_room_means_no_buys(cfg):
    book = ["H", "N1"]
    buys, dropped = size_new_positions(book, ["N1"], [], {"H": 13000.0}, vols(book), 15000.0, 0.85, cfg)
    assert buys == {} and dropped == ["N1"]                              # nothing left to buy it with


def test_topups_are_filled_before_new_names(cfg):
    book = ["T", "A", "N"]
    buys, _ = size_new_positions(book, ["N"], ["T"], {"T": 1000.0, "A": 1500.0}, vols(book),
                                 15000.0, 0.85, cfg)
    assert buys["T"] == pytest.approx(2250.0 - 1000.0)                   # back up to its 15%
    tight, dropped = size_new_positions(
        book, ["N"], ["T"], {"T": 1000.0, "A": 11000.0}, vols(book), 15000.0, 0.85, cfg)
    assert tight == {"T": pytest.approx(750.0)}                           # all the room there is
    assert dropped == ["N"]                                               # none left, so E7 drops it


def test_e7_drops_new_positions_under_the_minimum_smallest_first(cfg):
    """$7,000 across six new names is $992 each at 14.2%: too small. Dropping one
    leaves five at the 15% cap, $1,050 each, which is enough."""
    syms = names(6)
    buys, dropped = size_new_positions(syms, syms, [], {}, vols(syms), 7000.0, 0.85, cfg)
    assert dropped == ["N0"]                                             # tie -> symbol order
    assert sorted(buys) == syms[1:]
    assert list(buys.values()) == pytest.approx([1050.0] * 5)


def test_e7_minimum_is_inclusive(cfg):
    syms = names(5)
    exactly = float(size_positions(syms, vols(syms), 7000.0, 0.85, cfg).iloc[0])
    at = variant(cfg, sizing={"min_position_value": exactly})
    assert size_new_positions(syms, syms, [], {}, vols(syms), 7000.0, 0.85, at)[1] == []
    over = variant(cfg, sizing={"min_position_value": exactly + 1e-6})
    assert size_new_positions(syms, syms, [], {}, vols(syms), 7000.0, 0.85, over)[1] != []


def test_e7_never_drops_a_held_name(cfg):
    book = ["H", "N"]
    buys, dropped = size_new_positions(book, ["N"], [], {"H": 500.0}, vols(book), 2000.0, 0.85, cfg)
    assert "H" not in dropped and "H" not in buys


# ============================================================ deliberate breaks
#
# Each test loads rules.py with ONE line changed and shows the scenario that
# guards that line gives a different answer. If a break did not change the
# answer, the corresponding test above would be guarding nothing.


def mutant_exit(cfg, sf, mod, high=100.0, fired=0):
    (d,) = mod.evaluate_exits({"H": 1.0}, sf, {"H": high}, {"H": fired}, cfg)
    return d


def test_break_x1_off_by_one(cfg):
    bad = load_mutant("src.strategy.rules", "rank > cfg.exit.rank_exit", "rank >= cfg.exit.rank_exit")
    sf = frame({"H": {"rank": 16}})
    assert not exit_of(cfg, sf).should_sell and mutant_exit(cfg, sf, bad).should_sell


def test_break_no_buffer_exit_at_the_entry_rank(cfg):
    """Using the entry threshold for exits removes the 10-in/16-out buffer."""
    bad = load_mutant("src.strategy.rules", "rank > cfg.exit.rank_exit",
                      "rank > cfg.entry.rank_threshold")
    sf = frame({"H": {"rank": 12}})
    assert not exit_of(cfg, sf).should_sell and mutant_exit(cfg, sf, bad).should_sell


def test_break_ladder_measured_from_a_fixed_price_instead_of_the_peak(cfg):
    """The mutant reads the drawdown against a fixed 100 (a stand-in for the entry
    price) instead of the highest close. A name at 130 after running to 150 is 13.3%
    below its peak and must fire level 1; against 100 it is up 30% and does not."""
    bad = load_mutant("src.strategy.rules", "high = position_highs.get(sym)",
                      "high = 100.0")
    sf = frame({"H": {"rank": 1, "close": 130.0}})
    assert exit_of(cfg, sf, high=150.0).rule == "L1"
    assert not mutant_exit(cfg, sf, bad, high=150.0).should_sell


def test_break_ladder_level_fires_again_after_it_was_used(cfg):
    bad = load_mutant("src.strategy.rules", "elif crossed > fired:\n            new = crossed - fired",
                      "elif crossed > 0:\n            new = crossed")
    sf = frame({"H": {"rank": 1, "close": 87.0}})
    assert not exit_of(cfg, sf, fired=1).should_sell
    assert mutant_exit(cfg, sf, bad, fired=1).should_sell


def test_break_ladder_boundary_is_strict(cfg):
    bad = load_mutant("src.strategy.rules", "close <= high * (FULL_WEIGHT - d)",
                      "close < high * (FULL_WEIGHT - d)")
    sf = frame({"H": {"rank": 1, "close": 88.0}})
    assert exit_of(cfg, sf).rule == "L1" and not mutant_exit(cfg, sf, bad).should_sell


def test_break_ladder_sells_a_third_of_what_is_left_wrongly(cfg):
    """Selling n/3 of the CURRENT shares at level 2 would leave the position at 2/9,
    not 0: the second sale must be half of what is left."""
    bad = load_mutant("src.strategy.rules", "new / (n - fired)", "new / n")
    sf = frame({"H": {"rank": 1, "close": 80.0}})
    assert exit_of(cfg, sf, fired=1).sell_fraction == pytest.approx(1 / 2)
    assert mutant_exit(cfg, sf, bad, fired=1).sell_fraction == pytest.approx(1 / 3)


def test_break_x4_fires_at_zero(cfg):
    bad = load_mutant("src.strategy.rules", "elif momentum < ZERO", "elif momentum <= ZERO")
    sf = frame({"H": {"rank": 1, "mom": 0.0}})
    assert not exit_of(cfg, sf).should_sell and mutant_exit(cfg, sf, bad).should_sell


def test_break_exits_no_longer_fail_closed(cfg):
    bad = load_mutant("src.strategy.rules", "if _missing(close) or _missing(high):", "if False:")
    sf = frame({"H": {"rank": 1}})
    assert exit_of(cfg, sf, high=NAN).should_sell
    assert not mutant_exit(cfg, sf, bad, high=NAN).should_sell


def test_break_e6_position_count_off_by_one(cfg):
    bad = load_mutant("src.strategy.rules", "len(held) + new_count + 1 > cfg.risk.max_positions",
                      "len(held) + new_count + 1 >= cfg.risk.max_positions")
    held = [f"H{i}" for i in range(9)]
    sf = frame({"N": {"rank": 1}, **{h: {"rank": 20} for h in held}})
    assert decisions(sf, cfg, held=held)["N"].eligible
    out = bad.evaluate_entries(sf, set(held), {}, {}, cfg, regime_normal=True, sectors={"N": "S"})
    assert out[0].blocked_by == ["E6"]


def test_break_e8_uses_a_strict_cap(cfg):
    bad = load_mutant("src.strategy.rules",
                      "sector_now.get(sector, ZERO) + weight > cfg.risk.max_sector_weight",
                      "sector_now.get(sector, ZERO) + weight >= cfg.risk.max_sector_weight")
    sf = frame({"N": {"rank": 1}})
    out = bad.evaluate_entries(sf, set(), {"S": 0.25}, {}, cfg, regime_normal=True, sectors={"N": "S"})
    assert decisions(sf, cfg, sector_weights={"S": 0.25})["N"].eligible
    assert out[0].blocked_by == ["E8"]


def test_break_e8_treats_unknown_sector_as_fine(cfg):
    bad = load_mutant("src.strategy.rules", "if sector is None or sector_now.get(sector, ZERO) + weight",
                      "if sector is not None and sector_now.get(sector, ZERO) + weight")
    sf = frame({"N": {"rank": 1}})
    out = bad.evaluate_entries(sf, set(), {}, {}, cfg, regime_normal=True, sectors={})
    assert decisions(sf, cfg, sectors={})["N"].blocked_by == ["E8"]
    assert out[0].eligible


def test_break_e3_allows_zero_momentum(cfg):
    bad = load_mutant("src.strategy.rules", 'row["blended_momentum"] > cfg.entry.min_momentum:      # NaN fails',
                      'row["blended_momentum"] >= cfg.entry.min_momentum:      # NaN fails')
    sf = frame({"N": {"rank": 1, "mom": 0.0}})
    out = bad.evaluate_entries(sf, set(), {}, {}, cfg, regime_normal=True, sectors={"N": "S"})
    assert "E3" in decisions(sf, cfg)["N"].blocked_by and out[0].eligible


def test_break_e1_ignores_the_regime(cfg):
    bad = load_mutant("src.strategy.rules",
                      "blocked: list[str] = []\n\n        if cfg.entry.require_market_on and not regime_normal:",
                      "blocked: list[str] = []\n\n        if False:")
    sf = frame({"N": {"rank": 1}})
    out = bad.evaluate_entries(sf, set(), {}, {}, cfg, regime_normal=False, sectors={"N": "S"})
    assert decisions(sf, cfg, regime_normal=False)["N"].blocked_by == ["E1"] and out[0].eligible


def test_break_reentry_without_the_50_day_condition(cfg):
    bad = load_mutant("src.strategy.rules", 'and row["close"] > row["sma_reentry"]', "and True")
    sf = frame({"N": {"rank": 1, "close": 80.0, "sma": 90.0}})
    out = bad.evaluate_entries(sf, set(), {}, {}, cfg, regime_normal=True, sectors={"N": "S"},
                               exit_weeks={"N": 5})
    assert decisions(sf, cfg, exit_weeks={"N": 5})["N"].blocked_by == ["R1"]
    assert out[0].eligible


def test_break_reentry_without_the_one_week_wait(cfg):
    bad = load_mutant("src.strategy.rules", "exited[sym] >= reentry.min_weeks_after_exit", "True")
    sf = frame({"N": {"rank": 1, "close": 95.0, "sma": 90.0}})
    out = bad.evaluate_entries(sf, set(), {}, {}, cfg, regime_normal=True, sectors={"N": "S"},
                               exit_weeks={"N": 0})
    assert decisions(sf, cfg, exit_weeks={"N": 0})["N"].blocked_by == ["R1"]
    assert out[0].eligible


def test_break_topup_before_the_prior_peak_is_retaken(cfg):
    bad = load_mutant("src.strategy.rules", 'if not row["close"] > partials[sym]:', "if False:")
    sf = frame({"P": {"rank": 1, "close": 95.0}})
    out = bad.evaluate_topups(sf, {"P": 100.0}, {"P": 0.05}, {}, {}, cfg, regime_normal=True,
                              sectors={"P": "S"})
    assert topups(cfg, {"P": {"rank": 1, "close": 95.0}}, {"P": 100.0})["P"].blocked_by == ["T1"]
    assert out[0].eligible


def test_break_refill_ignores_the_position_cap(cfg):
    """The refill sized without the 15% cap: two names would take 42.5% each."""
    bad = load_mutant(
        "src.strategy.rules",
        "cfg.sizing.min_position_weight, cfg.sizing.max_position_weight, budget)",
        "cfg.sizing.min_position_weight, FULL_WEIGHT, budget)")
    syms = names(2)
    good = size_positions(syms, vols(syms), 10000.0, 0.85, cfg)
    assert (good <= 1500.0 + 1e-6).all()
    assert (bad.size_positions(syms, vols(syms), 10000.0, 0.85, cfg) > 4000.0).all()


def test_break_sizing_renormalizes_after_clipping(cfg):
    """Reintroduce the literal spec reading: the cap is silently exceeded."""
    bad = load_mutant(
        "src.strategy.rules",
        "weights = _bounded_weights(\n        FULL_WEIGHT / v, cfg.sizing.min_position_weight, cfg.sizing.max_position_weight, budget)",
        "weights = (lambda w: w.clip(cfg.sizing.min_position_weight, cfg.sizing.max_position_weight) "
        "/ w.clip(cfg.sizing.min_position_weight, cfg.sizing.max_position_weight).sum() * budget)"
        "(FULL_WEIGHT / v)")
    syms = names(2)
    assert (size_positions(syms, vols(syms), 10000.0, 0.85, cfg) <= 1500.0 + 1e-6).all()
    assert (bad.size_positions(syms, vols(syms), 10000.0, 0.85, cfg) > 1500.0).any()


def test_break_sizing_over_the_invested_target(cfg):
    bad = load_mutant("src.strategy.rules", "budget = min(invested_target, cfg.risk.max_gross_exposure)",
                      "budget = FULL_WEIGHT")
    syms = names(10)
    assert size_positions(syms, vols(syms), 10000.0, 0.85, cfg).sum() == pytest.approx(8500.0)
    assert bad.size_positions(syms, vols(syms), 10000.0, 0.85, cfg).sum() == pytest.approx(10000.0)


def test_break_min_position_value_not_enforced(cfg):
    bad = load_mutant("src.strategy.rules",
                      "small = [s for s in want_new if not buys[s] >= cfg.sizing.min_position_value]",
                      "small = []")
    syms = names(6)
    args = (syms, syms, [], {}, vols(syms), 7000.0, 0.85, cfg)
    assert size_new_positions(*args)[1] == ["N0"]
    assert bad.size_new_positions(*args)[1] == []
