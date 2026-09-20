"""Section 2 filters, the cap, and the saved snapshot."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.data.cache import PriceCache
from src.data.refresh import build_universe
from src.data.universe import (
    ContractInfo,
    UniverseError,
    load_universe,
    open_db,
    save_contract_info,
)
from tests.conftest import write_constituents
from tests.support import recent_bars


def info(sym, stock_type="COMMON", sector="Technology"):
    return ContractInfo(sym, 1, stock_type, sector, "cat", "sub", sym)


def seed(cfg, symbols: dict[str, dict], infos: dict[str, ContractInfo], flat: bool = False):
    """symbols: name -> recent_bars kwargs. SPY is always cached.

    flat=True pins every close to `price` so dollar volume is exact.
    """
    cache = PriceCache(cfg.cache_path)
    cache.write("SPY", recent_bars())
    for sym, kw in symbols.items():
        df = recent_bars(**kw)
        if flat:
            px = kw.get("price", 100.0)
            df["close"], df["high"], df["low"] = px, px * 1.001, px * 0.999
        cache.write(sym, df)
    write_constituents(cfg, [(s, s) for s in symbols], date.today().isoformat())
    conn = open_db(cfg)
    save_contract_info(conn, list(infos.values()))
    conn.close()


def test_each_filter_drops_its_symbol(refresh_cfg):
    stale_end = date.today() - timedelta(days=30)
    seed(
        refresh_cfg,
        {
            "GOOD": dict(price=100, volume=1e6),                 # $100M/day
            "CHEAP": dict(price=5, volume=1e8),                  # price < $10
            "THIN": dict(price=100, volume=1e3),                 # $100k/day
            "YOUNG": dict(n=200, price=100, volume=1e6),         # < 400 days
            "ETFISH": dict(price=100, volume=1e6),               # stockType ETF
            "REITX": dict(price=100, volume=1e6),                # stockType REIT
            "NOINFO": dict(price=100, volume=1e6),               # no contract info
            "OLD": dict(price=100, volume=1e6, end=stale_end),   # last bar 30d old
        },
        {
            s: info(s, t) for s, t in [
                ("GOOD", "COMMON"), ("CHEAP", "COMMON"), ("THIN", "COMMON"),
                ("YOUNG", "COMMON"), ("ETFISH", "ETF"), ("REITX", "REIT"),
                ("OLD", "COMMON"),
            ]
        },
    )
    assert build_universe(refresh_cfg) == ["GOOD"]


def test_boundaries_are_inclusive(refresh_cfg):
    u = refresh_cfg.universe
    seed(
        refresh_cfg,
        # exactly $10 and exactly $20M/day
        {"EDGE": dict(price=u.min_price, volume=u.min_dollar_volume_20d / u.min_price)},
        {"EDGE": info("EDGE")},
        flat=True,
    )
    assert build_universe(refresh_cfg) == ["EDGE"]


def test_just_below_the_boundaries_is_dropped(refresh_cfg):
    u = refresh_cfg.universe
    seed(
        refresh_cfg,
        {
            "PX": dict(price=u.min_price - 0.01, volume=1e9),
            "DV": dict(price=u.min_price, volume=u.min_dollar_volume_20d / u.min_price * 0.99),
        },
        {"PX": info("PX"), "DV": info("DV")},
        flat=True,
    )
    with pytest.raises(UniverseError):
        build_universe(refresh_cfg)


def test_cap_ranks_by_dollar_volume_with_symbol_tiebreak(refresh_cfg):
    refresh_cfg.universe.max_symbols = 3
    names = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    vols = [5e6, 9e6, 9e6, 2e6, 7e6]
    seed(
        refresh_cfg,
        {n: dict(volume=v) for n, v in zip(names, vols)},
        {n: info(n) for n in names},
        flat=True,
    )
    assert build_universe(refresh_cfg) == ["BBB", "CCC", "EEE"]   # BBB/CCC tie -> symbol order


def test_snapshot_is_saved_with_sector_and_rank(refresh_cfg, db):
    seed(
        refresh_cfg,
        {"AAA": dict(volume=5e6), "BBB": dict(volume=9e6)},
        {"AAA": info("AAA", sector="Financial"), "BBB": info("BBB", sector="Technology")},
        flat=True,
    )
    build_universe(refresh_cfg)
    rows = db.execute(
        "SELECT symbol, rank, sector, snapshot_date FROM universe_snapshots ORDER BY rank"
    ).fetchall()
    assert [(r["symbol"], r["rank"], r["sector"]) for r in rows] == [
        ("BBB", 1, "Technology"), ("AAA", 2, "Financial"),
    ]
    assert rows[0]["snapshot_date"] == date.today().isoformat()
    assert load_universe(db) == ["BBB", "AAA"]


def test_same_day_rebuild_replaces_not_duplicates(refresh_cfg, db):
    seed(refresh_cfg, {"AAA": dict(volume=5e6)}, {"AAA": info("AAA")})
    build_universe(refresh_cfg)
    build_universe(refresh_cfg)
    assert db.execute("SELECT COUNT(*) FROM universe_snapshots").fetchone()[0] == 1


def test_snapshot_lookup_is_point_in_time(refresh_cfg, db):
    db.executemany(
        """INSERT INTO universe_snapshots (snapshot_date, symbol, rank, stock_type,
           avg_dollar_volume_20d, price, days_listed, data_as_of)
           VALUES (?,?,?,?,?,?,?,?)""",
        [("2026-01-05", "OLDNAME", 1, "COMMON", 1.0, 1.0, 1, "2026-01-02"),
         ("2026-04-06", "NEWNAME", 1, "COMMON", 1.0, 1.0, 1, "2026-04-03")],
    )
    db.commit()
    assert load_universe(db, date(2026, 3, 1)) == ["OLDNAME"]
    assert load_universe(db, date(2026, 4, 6)) == ["NEWNAME"]
    assert load_universe(db) == ["NEWNAME"]
    with pytest.raises(UniverseError):
        load_universe(db, date(2025, 12, 31))


def test_zero_survivors_fails_and_keeps_old_snapshot(refresh_cfg, db):
    seed(refresh_cfg, {"AAA": dict(volume=5e6)}, {"AAA": info("AAA")})
    build_universe(refresh_cfg)
    refresh_cfg.data.volume_multiplier = 1e-9         # everything now looks illiquid
    with pytest.raises(UniverseError, match="volume_multiplier"):
        build_universe(refresh_cfg)
    assert load_universe(db) == ["AAA"]


def test_volume_multiplier_scales_dollar_volume(refresh_cfg):
    seed(refresh_cfg, {"AAA": dict(volume=5e3)}, {"AAA": info("AAA")})   # $0.5M/day
    with pytest.raises(UniverseError):
        build_universe(refresh_cfg)
    refresh_cfg.data.volume_multiplier = 100                              # $50M/day
    assert build_universe(refresh_cfg) == ["AAA"]


def test_missing_benchmark_is_an_error(refresh_cfg):
    write_constituents(refresh_cfg, [("AAA", "A")], date.today().isoformat())
    with pytest.raises(UniverseError, match="SPY"):
        build_universe(refresh_cfg)


def test_report_shows_top_and_bottom_five_in_order_with_drop_counts():
    import re

    from src.data.universe import UniverseRow, UniverseSelection, format_universe_report

    rows = [UniverseRow(f"S{i}", f"S{i}", "Tech", None, "COMMON", (20 - i) * 1e6, 50.0, 900, i)
            for i in range(1, 9)]                                    # $19M ... $12M
    text = format_universe_report(
        UniverseSelection(rows, date(2026, 9, 18), {"price": 3, "stock_type": 41}), 500)
    top, rest = text.split("Bottom 5 of the selection:")
    bottom, dropped = rest.split("Dropped by filter:")

    assert re.findall(r"S\d", top) == ["S1", "S2", "S3", "S4", "S5"]
    assert re.findall(r"S\d", bottom) == ["S4", "S5", "S6", "S7", "S8"]
    assert "19.0M/day" in top and "12.0M/day" in bottom
    assert dropped.index("stock_type") < dropped.index("price")       # biggest first
    assert re.search(r"stock_type\s+41", dropped) and re.search(r"price\s+3", dropped)
    assert "8 chosen from 500" in text
