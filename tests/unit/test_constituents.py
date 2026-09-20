"""Constituents file: ticker punctuation, staleness warning, bad files."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pytest

from src.data.universe import UniverseError, load_constituents, to_ibkr_symbol
from tests.conftest import write_constituents


def test_share_class_dot_becomes_space():
    assert to_ibkr_symbol("BRK.B") == "BRK B"
    assert to_ibkr_symbol("bf.b") == "BF B"
    assert to_ibkr_symbol(" AAPL ") == "AAPL"


def test_loader_returns_ibkr_symbols(refresh_cfg):
    write_constituents(refresh_cfg, [("AAPL", "Apple"), ("BRK.B", "Berkshire")],
                       date.today().isoformat())
    assert [c.symbol for c in load_constituents(refresh_cfg)] == ["AAPL", "BRK B"]


def test_stale_list_warns_but_does_not_halt(refresh_cfg, caplog):
    limit = refresh_cfg.universe.constituents_max_age_days
    old = date.today() - timedelta(days=limit + 1)
    write_constituents(refresh_cfg, [("AAPL", "Apple")], old.isoformat())
    with caplog.at_level(logging.WARNING):
        rows = load_constituents(refresh_cfg)
    assert len(rows) == 1                      # returned, not raised
    assert "days old" in caplog.text


def test_fresh_list_does_not_warn(refresh_cfg, caplog):
    limit = refresh_cfg.universe.constituents_max_age_days
    write_constituents(refresh_cfg, [("AAPL", "Apple")],
                       (date.today() - timedelta(days=limit)).isoformat())
    with caplog.at_level(logging.WARNING):
        load_constituents(refresh_cfg)
    assert "days old" not in caplog.text


def test_missing_file_says_how_to_fix(refresh_cfg):
    with pytest.raises(UniverseError, match="update_constituents"):
        load_constituents(refresh_cfg)


def test_duplicate_symbols_rejected(refresh_cfg):
    write_constituents(refresh_cfg, [("AAPL", "Apple"), ("AAPL", "Apple again")],
                       date.today().isoformat())
    with pytest.raises(UniverseError, match="duplicate"):
        load_constituents(refresh_cfg)
