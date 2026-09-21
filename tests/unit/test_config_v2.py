"""v2.0.0 configuration and its guards (spec 4.1, 6, 8, 13)."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from src.config import DEFAULT_CONFIG_PATH, Config


def raw() -> dict:
    return yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


def test_the_shipped_config_is_v2_and_says_so():
    cfg = Config(**raw())
    assert (cfg.strategy.name, cfg.strategy.version) == ("weekly_momentum_v2", "2.0.1")
    assert cfg.account.paper_trading and cfg.risk.require_manual_approval


def test_the_v2_parameters_are_the_ones_the_spec_states():
    c = Config(**raw())
    assert (c.regime.target_normal, c.regime.target_defensive) == (0.85, 0.40)
    assert (c.regime.confirm_weeks, c.regime.refill_below) == (2, 0.80)
    assert c.exit.ladder.drawdowns == [0.12, 0.20, 0.28]
    assert (c.risk.max_positions, c.entry.rank_threshold, c.exit.rank_exit) == (10, 10, 16)
    assert (c.sizing.min_position_weight, c.sizing.max_position_weight) == (0.05, 0.15)
    assert (c.risk.max_sector_weight, c.sizing.min_position_value) == (0.40, 1000)
    assert (c.reentry.max_rank, c.reentry.sma_days, c.reentry.min_weeks_after_exit) == (10, 50, 1)
    assert (c.risk_dial.levels.normal, c.risk_dial.levels.caution) == (0.85, 0.60)
    assert (c.risk_dial.min_days_between_changes, c.risk_dial.expiry_weeks) == (30, 4)
    assert c.backtest.acceptance.beat_benchmark_margin == 0.02


def test_the_retired_v1_keys_are_gone():
    r = raw()
    assert "max_new_per_rebalance" not in r["entry"]
    assert "trailing_stop_atr" not in r["exit"] and "exit_all_on_market_off" not in r["exit"]
    for key in ("target_portfolio_vol", "scale_up_allowed", "covariance_window"):
        assert key not in r["sizing"]
    assert "drift_tolerance" not in r["schedule"]


def broken(mutate) -> dict:
    r = raw()
    mutate(r)
    return r


@pytest.mark.parametrize("mutate,message", [
    (lambda r: r["risk_dial"]["levels"].update(normal=0.90), "never raise|dial"),
    (lambda r: r["risk_dial"]["levels"].update(caution=0.85), "caution < normal|dial"),
    (lambda r: r["risk_dial"]["levels"].update(caution=0.40), "defensive"),
    (lambda r: r["regime"].update(target_normal=1.05), "less than or equal|target_normal"),
    (lambda r: r["risk"].update(max_gross_exposure=0.80), "leverage|max_gross"),
    (lambda r: r["regime"].update(refill_below=0.90), "refill_below"),
    (lambda r: r["regime"].update(target_defensive=0.85), "refill_below|target_defensive"),
    (lambda r: r["exit"]["ladder"].update(drawdowns=[0.20, 0.12, 0.28]), "increasing"),
    (lambda r: r["exit"]["ladder"].update(drawdowns=[0.12, 0.12]), "increasing"),
    (lambda r: r["exit"]["ladder"].update(drawdowns=[]), "increasing"),
    (lambda r: r["exit"]["ladder"].update(drawdowns=[0.12, 1.2]), "increasing"),
    (lambda r: r["exit"].update(rank_exit=10), "rank_exit"),
    (lambda r: r["reentry"].update(max_rank=20), "max_rank"),
    (lambda r: r["sizing"].update(min_position_weight=0.2), "min_position_weight"),
])
def test_invalid_configs_are_refused(mutate, message):
    with pytest.raises((ValidationError, ValueError), match=message):
        Config(**broken(mutate))


def test_the_no_leverage_and_veto_only_guards_are_untouched():
    with pytest.raises((ValidationError, ValueError), match="leverage"):
        Config(**broken(lambda r: r["risk"].update(max_gross_exposure=1.2)))
    with pytest.raises((ValidationError, ValueError), match="veto_only"):
        Config(**broken(lambda r: r["ai"].update(role="signal")))
    with pytest.raises((ValidationError, ValueError), match="market"):
        Config(**broken(lambda r: r["execution"].update(order_type="MARKET")))
