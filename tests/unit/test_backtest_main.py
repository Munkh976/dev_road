"""The backtest entry point end to end, on a synthetic cache in a temp dir.

Never reads the real cache or database: `refresh_cfg` points WM_CACHE_DIR and
WM_DB_PATH at temp paths, and the report goes to a temp reports directory.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.backtest import walkforward as wf
from src.backtest.walkforward import BacktestError, load_market_data, main, walk_forward
from src.data.cache import PriceCache
from src.data.universe import ContractInfo, open_db, save_contract_info
from tests.conftest import write_constituents
from tests.support import recent_bars

SYMBOLS = ["AAPL", "MSFT", "NVDA", "JPM", "XOM", "JNJ", "AMZN"]     # all in the real constituents CSV
BARS = 1500                                                          # ~6 years: one full OOS window and a bit


def build_world(cfg, *, types=None):
    write_constituents(cfg, [(s, s) for s in SYMBOLS], "2026-09-01")   # ZZZZ is left out
    cache = PriceCache(cfg.cache_path)
    cache.write("SPY", recent_bars(BARS, seed=100))
    for i, s in enumerate(SYMBOLS):
        cache.write(s, recent_bars(BARS, seed=i, price=50 + 10 * i, volume=2e6))
    cache.write("ZZZZ", recent_bars(BARS, seed=50))                  # cached but not a constituent
    conn = open_db(cfg)
    infos = [ContractInfo(s, i, (types or {}).get(s, "COMMON"), f"Sector{i % 3}", None, None, s)
             for i, s in enumerate(SYMBOLS)]
    save_contract_info(conn, infos)
    conn.close()


def test_load_market_data_aligns_to_the_benchmark_calendar(refresh_cfg):
    build_world(refresh_cfg)
    cache = PriceCache(refresh_cfg.cache_path)
    short = recent_bars(BARS, seed=7)
    cache.write("AAPL", short.drop(short.index[[10, 400]]))          # AAPL missed two bars

    data = load_market_data(refresh_cfg)

    spy_days = cache.read("SPY").index
    assert data.closes.index.equals(spy_days)
    assert list(data.closes.columns[:1]) == ["SPY"]
    assert "ZZZZ" not in data.closes.columns                         # not a constituent
    assert data.closes["AAPL"].isna().sum() == 2                     # gaps stay NaN, no forward-fill
    assert data.opens.shape == data.closes.shape == data.volumes.shape
    assert data.sectors["MSFT"] == "Sector1"
    assert data.allowed == frozenset(SYMBOLS)


def test_security_type_comes_from_contract_info_and_missing_info_fails_closed(refresh_cfg):
    build_world(refresh_cfg, types={"XOM": "REIT"})
    conn = open_db(refresh_cfg)
    conn.execute("DELETE FROM contract_info WHERE symbol='JNJ'")
    conn.commit()
    conn.close()
    data = load_market_data(refresh_cfg)
    assert "XOM" not in data.allowed and "JNJ" not in data.allowed
    assert "AAPL" in data.allowed


def test_missing_benchmark_is_an_error(refresh_cfg):
    build_world(refresh_cfg)
    refresh_cfg.cache_path.joinpath("SPY.parquet").unlink()
    with pytest.raises(Exception, match="SPY"):
        load_market_data(refresh_cfg)


def test_walk_forward_from_the_cache(refresh_cfg):
    build_world(refresh_cfg)
    res = walk_forward(refresh_cfg)
    assert res.windows and res.equity_curve.iloc[0] == refresh_cfg.account.capital
    assert res.options.is_valid_run


def test_main_writes_a_report_and_records_the_verdict(refresh_cfg, db, tmp_path, monkeypatch, capsys):
    build_world(refresh_cfg)
    monkeypatch.setattr(wf, "REPORTS_DIR", tmp_path / "reports")

    code = main([])

    assert code in (0, 1)                                            # 1 = ran, acceptance failed
    out = capsys.readouterr().out
    assert "Out-of-sample only" in out and "E5 (AI veto) always passes" in out
    (report,) = list((tmp_path / "reports").glob("backtest_*.md"))
    assert report.read_text(encoding="utf-8").startswith("# Backtest:")
    run = db.execute("SELECT * FROM runs WHERE run_type='backtest'").fetchone()
    assert run["status"] == "ok" and run["finished_at"]
    assert run["notes"].startswith("acceptance ") and "A1=" in run["notes"] and "A6=" in run["notes"]


def test_main_never_records_an_invalid_run_as_a_result(refresh_cfg, db, tmp_path, monkeypatch):
    build_world(refresh_cfg)
    monkeypatch.setattr(wf, "REPORTS_DIR", tmp_path / "reports")
    real = wf.walk_forward
    monkeypatch.setattr(
        wf, "walk_forward",
        lambda cfg: real(cfg, options=wf.BacktestOptions(apply_costs=False)))

    assert main([]) == 3

    run = db.execute("SELECT * FROM runs WHERE run_type='backtest'").fetchone()
    assert run["status"] == "failed" and run["error"] == "invalid run"
    assert run["notes"] == "walk-forward, out-of-sample only"      # no verdict recorded
    assert not (tmp_path / "reports").exists()


def test_main_records_a_failed_run_when_history_is_too_short(refresh_cfg, db, tmp_path, monkeypatch):
    cache = PriceCache(refresh_cfg.cache_path)
    cache.write("SPY", recent_bars(400, seed=1))
    for i, s in enumerate(SYMBOLS):
        cache.write(s, recent_bars(400, seed=i, volume=2e6))
    conn = open_db(refresh_cfg)
    save_contract_info(conn, [ContractInfo(s, i, "COMMON", "S", None, None, s)
                              for i, s in enumerate(SYMBOLS)])
    conn.close()
    monkeypatch.setattr(wf, "REPORTS_DIR", tmp_path / "reports")

    assert main([]) == 2

    run = db.execute("SELECT * FROM runs WHERE run_type='backtest'").fetchone()
    assert run["status"] == "failed" and "not enough history" in run["error"]
    assert not (tmp_path / "reports").exists()                       # no report for a run that did not happen
