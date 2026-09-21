"""Graded market filter (spec section 4.1): two weekly readings to switch, either way."""

from __future__ import annotations

import pytest
import yaml

from src.config import DEFAULT_CONFIG_PATH, Config
from src.strategy.regime import (
    DEFENSIVE,
    NORMAL,
    RegimeState,
    next_mode,
    regime_modes,
    target_invested,
)
from tests.mutants import load_mutant


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config(**yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")))


def modes(readings: str, cfg) -> list[str]:
    """'++--' style: + is SPY above its SMA that week, - is below."""
    return regime_modes([c == "+" for c in readings], cfg)


N, D = NORMAL, DEFENSIVE


def test_the_fold_starts_defensive_and_needs_two_readings_above_to_go_normal(cfg):
    assert modes("+", cfg) == [D]                        # fail closed: one reading is not enough
    assert modes("++", cfg) == [D, N]
    assert modes("-", cfg) == [D]


def test_two_consecutive_readings_below_move_normal_to_defensive(cfg):
    assert modes("++-", cfg) == [D, N, N]                # one week below changes nothing
    assert modes("++--", cfg) == [D, N, N, D]


def test_one_reading_the_other_way_resets_the_count(cfg):
    assert modes("++-+-", cfg) == [D, N, N, N, N]        # never two below in a row
    assert modes("++-+--", cfg) == [D, N, N, N, N, D]
    assert modes("--+-+", cfg) == [D, D, D, D, D]        # never two above in a row
    assert modes("--++", cfg) == [D, D, D, N]


def test_the_streak_is_taken_from_the_state_not_from_the_history(cfg):
    st = RegimeState(NORMAL, 0)
    st = next_mode(st, False, cfg)
    assert st == RegimeState(NORMAL, 1)
    st = next_mode(st, False, cfg)
    assert st == RegimeState(DEFENSIVE, 0)               # confirmed; the count starts over
    st = next_mode(st, True, cfg)
    assert st == RegimeState(DEFENSIVE, 1)
    assert next_mode(st, True, cfg) == RegimeState(NORMAL, 0)


def test_confirm_weeks_comes_from_config(cfg):
    three = cfg.model_copy(deep=True)
    three.regime.confirm_weeks = 3
    assert modes("+++", three) == [D, D, N]
    assert modes("+++--", three) == [D, D, N, N, N]
    assert modes("+++---", three) == [D, D, N, N, N, D]


def test_targets_follow_the_mode(cfg):
    assert target_invested(NORMAL, cfg) == 0.85
    assert target_invested(DEFENSIVE, cfg) == 0.40


def test_the_mode_at_a_week_does_not_depend_on_later_weeks(cfg):
    """Look-ahead: the fold over a prefix is the prefix of the fold."""
    readings = "++++--+-++--------++"
    full = modes(readings, cfg)
    for cut in range(1, len(readings) + 1):
        assert modes(readings[:cut], cfg) == full[:cut]


def test_break_one_reading_switches_the_mode(cfg):
    bad = load_mutant("src.strategy.regime", "if streak >= cfg.regime.confirm_weeks:", "if streak >= 1:")
    good = regime_modes([True, True, False, True], cfg)
    got = bad.regime_modes([True, True, False, True], cfg)
    assert good == [D, N, N, N] and got != good


def test_break_the_fold_starts_normal(cfg):
    bad = load_mutant("src.strategy.regime", "mode: str = DEFENSIVE", "mode: str = NORMAL")
    assert modes("+", cfg) == [D]
    assert bad.regime_modes([True], cfg) == [N]
