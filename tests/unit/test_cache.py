"""Price cache tests, including the validation that refuses bad bars."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from src.data.cache import PriceCache, next_fetch_duration


def make_bars(n=300, start="2024-01-01", seed=0, price=100.0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start=start, periods=n)
    rets = rng.normal(0.0004, 0.012, n)
    close = price * np.exp(np.cumsum(rets))
    return pd.DataFrame(
        {
            "open": close * (1 + rng.normal(0, 0.002, n)),
            "high": close * (1 + abs(rng.normal(0, 0.005, n))),
            "low": close * (1 - abs(rng.normal(0, 0.005, n))),
            "close": close,
            "volume": rng.integers(1e6, 5e6, n).astype(float),
        },
        index=idx,
    )


@pytest.fixture
def cache(tmp_path):
    return PriceCache(tmp_path / "cache")


def test_roundtrip(cache):
    df = make_bars()
    stats = cache.write("AAPL", df)
    assert stats.rows == 300
    back = cache.read("AAPL")
    assert len(back) == 300
    assert back.index.name == "date"
    np.testing.assert_allclose(back["close"].values, df["close"].values)


def test_read_missing_returns_none(cache):
    assert cache.read("NOPE") is None


def test_merge_is_incremental_and_restatement_wins(cache):
    first = make_bars(n=100)
    cache.write("MSFT", first)

    # New fetch overlaps the last 5 bars with restated values.
    overlap = first.iloc[-5:].copy()
    overlap["close"] = overlap["close"] * 1.01
    extra = make_bars(n=5, start=str((first.index[-1] + timedelta(days=1)).date()), seed=7)
    new = pd.concat([overlap, extra])

    stats = cache.merge("MSFT", new)
    assert stats.rows == 105                      # no duplicates
    merged = cache.read("MSFT")
    assert merged.index.is_unique
    # Restated bar took precedence
    assert merged["close"].iloc[-6] == pytest.approx(overlap["close"].iloc[-1])


def test_rejects_non_positive_close(cache):
    df = make_bars(n=50)
    df.iloc[10, df.columns.get_loc("close")] = 0.0
    with pytest.raises(ValueError, match="non-positive close"):
        cache.write("BAD", df)


def test_rejects_high_below_low(cache):
    df = make_bars(n=50)
    df.iloc[5, df.columns.get_loc("high")] = 1.0
    df.iloc[5, df.columns.get_loc("low")] = 99.0
    with pytest.raises(ValueError, match="high < low"):
        cache.write("BAD", df)


def test_rejects_missing_columns(cache):
    df = make_bars(n=50).drop(columns=["volume"])
    with pytest.raises(ValueError, match="missing columns"):
        cache.write("BAD", df)


def test_load_matrix_does_not_forward_fill(cache):
    """A filled bar for a halted or delisted name invents a tradable price."""
    a = make_bars(n=100, seed=1)
    b = make_bars(n=60, seed=2)          # shorter history
    cache.write("AAA", a)
    cache.write("BBB", b)

    m = cache.load_matrix(["AAA", "BBB"], field="close")
    assert list(m.columns) == ["AAA", "BBB"]
    assert m["BBB"].isna().sum() > 0, "gaps must stay NaN"


def test_staleness_report_classifies(cache):
    fresh = make_bars(n=400, start=str(date.today() - timedelta(days=600)))
    cache.write("FRESH", fresh)
    cache.write("SHORT", make_bars(n=50, start=str(date.today() - timedelta(days=80))))

    rep = cache.staleness_report(["FRESH", "SHORT", "GONE"], max_stale_days=5, min_bars=300)
    assert "GONE" in rep["missing"]
    assert "SHORT" in rep["short"] or "SHORT" in rep["stale"]


def test_suspicious_move_is_flagged_not_dropped(cache):
    df = make_bars(n=100)
    df.iloc[50, df.columns.get_loc("close")] *= 2.5     # unadjusted split, say
    stats = cache.write("SPLIT", df)
    assert stats.suspicious_moves >= 1
    assert stats.rows == 100, "flagged, not silently removed"


def test_next_fetch_duration():
    assert next_fetch_duration(None, 15) == "15 Y"
    today = date(2026, 9, 19)
    assert next_fetch_duration(date(2026, 9, 12), 15, today) == "12 D"
    # Never request less than a small floor, so restatements are picked up.
    assert next_fetch_duration(date(2026, 9, 19), 15, today) == "5 D"
