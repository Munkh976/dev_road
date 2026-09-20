"""
Parquet price cache.

Price history is immutable — yesterday's adjusted close does not change — so
it is fetched once and updated incrementally thereafter. This is what makes
the IBKR pacing limit survivable: a full 15-year pull happens once, and each
weekly refresh requests only the handful of new bars per symbol.

Layout:
    data/cache/{SYMBOL}.parquet    one file per symbol, DatetimeIndex
    data/cache/_manifest.json      last-updated dates, for fast staleness checks

Why parquet and not SQLite: the backtest loads the whole 150 x 3780 matrix at
once, repeatedly, across walk-forward windows. Columnar parquet reads into
pandas roughly an order of magnitude faster than row-oriented SQLite for that
access pattern. SQLite holds the records; parquet holds the numbers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

REQUIRED_COLUMNS = ["open", "high", "low", "close", "volume"]
MANIFEST_NAME = "_manifest.json"

# A single-day move larger than this is almost always a bad bar or an
# unadjusted split rather than a real price. Flagged, not silently dropped.
SUSPICIOUS_DAILY_MOVE = 0.50

# Calendar conversion and the tolerance on "the first bar reaches the target
# start": IBKR's "22 Y" begins at the first trading day on/after that date.
DAYS_PER_YEAR = 365.25
BACKFILL_SLACK_DAYS = 10


@dataclass
class CacheStats:
    symbol: str
    rows: int
    first_date: date | None
    last_date: date | None
    stale_days: int | None
    gaps: int = 0
    suspicious_moves: int = 0

    @property
    def is_empty(self) -> bool:
        return self.rows == 0


class PriceCache:
    """Read/write access to the parquet price cache."""

    def __init__(self, cache_dir: Path) -> None:
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self.dir / MANIFEST_NAME

    # ------------------------------------------------------------- reading

    def path_for(self, symbol: str) -> Path:
        return self.dir / f"{symbol.upper()}.parquet"

    def has(self, symbol: str) -> bool:
        return self.path_for(symbol).exists()

    def read(self, symbol: str) -> pd.DataFrame | None:
        """Return the cached bars for one symbol, or None if not cached."""
        p = self.path_for(symbol)
        if not p.exists():
            return None
        df = pd.read_parquet(p)
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        return df.sort_index()

    def last_date(self, symbol: str) -> date | None:
        """Last bar date for a symbol, read from the manifest (no file IO)."""
        manifest = self.read_manifest()
        iso = manifest.get(symbol.upper(), {}).get("last_date")
        return date.fromisoformat(iso) if iso else None

    def symbols(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.parquet"))

    def load_matrix(
        self,
        symbols: list[str],
        field: str = "close",
        start: str | date | None = None,
        end: str | date | None = None,
    ) -> pd.DataFrame:
        """Load one field for many symbols into a single aligned DataFrame.

        This is the backtest's main entry point. Columns are symbols, index is
        the union of all trading days. Missing values stay NaN — do not
        forward-fill here, because a filled bar for a delisted name silently
        manufactures a tradable price that never existed.
        """
        series: dict[str, pd.Series] = {}
        for sym in symbols:
            df = self.read(sym)
            if df is None or field not in df.columns:
                continue
            series[sym] = df[field]

        if not series:
            return pd.DataFrame()

        matrix = pd.DataFrame(series).sort_index()
        if start is not None:
            matrix = matrix.loc[pd.Timestamp(start):]
        if end is not None:
            matrix = matrix.loc[:pd.Timestamp(end)]
        return matrix

    # ------------------------------------------------------------- writing

    def write(
        self, symbol: str, df: pd.DataFrame, *, history_years: int | None = None
    ) -> CacheStats:
        """Replace the cached bars for a symbol. Validates before writing.

        `history_years` marks the write as a full-history fetch of that depth,
        recorded in the manifest so a backfill can be resumed. A short symbol
        (recent listing) never has bars back to the target start, so the bars
        alone cannot say "this was already backfilled". Writes that do not pass
        it (incremental merges) keep whatever the manifest already holds.
        """
        df = self._normalize(df)
        self._validate(symbol, df)
        df.to_parquet(self.path_for(symbol), compression="snappy")
        stats = self.inspect(symbol, df)
        self._update_manifest(symbol, stats, history_years)
        return stats

    def is_backfilled(
        self, symbol: str, history_years: int, today: date | None = None
    ) -> bool:
        """Was this symbol fetched in full at `history_years` (or deeper)?

        True if the manifest says a full fetch of that depth happened, or, for
        caches written before the marker existed, if the first bar already
        reaches the target start. Anything else is not backfilled: fail toward
        refetching, which costs one request, rather than toward a shallow cache.
        """
        entry = self.read_manifest().get(symbol.upper())
        if not entry or not entry.get("rows"):
            return False
        if (entry.get("history_years") or 0) >= history_years:
            return True
        first = entry.get("first_date")
        if not first:
            return False
        target = (today or date.today()) - timedelta(days=round(history_years * DAYS_PER_YEAR))
        return date.fromisoformat(first) <= target + timedelta(days=BACKFILL_SLACK_DAYS)

    def merge(self, symbol: str, new_bars: pd.DataFrame) -> CacheStats:
        """Merge newly fetched bars into the cache.

        New bars win on overlap — IBKR may restate a recent bar, and a
        restatement is a correction, not a duplicate.
        """
        new_bars = self._normalize(new_bars)
        existing = self.read(symbol)

        if existing is None or existing.empty:
            combined = new_bars
        else:
            combined = pd.concat([existing, new_bars])
            combined = combined[~combined.index.duplicated(keep="last")]
            combined = combined.sort_index()

        return self.write(symbol, combined)

    # ------------------------------------------------------------ inspection

    def inspect(self, symbol: str, df: pd.DataFrame | None = None) -> CacheStats:
        """Quality report for one symbol."""
        if df is None:
            df = self.read(symbol)
        if df is None or df.empty:
            return CacheStats(symbol, 0, None, None, None)

        first = df.index[0].date()
        last = df.index[-1].date()
        stale = (date.today() - last).days

        # Calendar gaps longer than a long weekend + a holiday.
        deltas = df.index.to_series().diff().dt.days.dropna()
        gaps = int((deltas > 5).sum())

        moves = df["close"].pct_change().abs()
        suspicious = int((moves > SUSPICIOUS_DAILY_MOVE).sum())

        return CacheStats(symbol, len(df), first, last, stale, gaps, suspicious)

    def staleness_report(
        self, symbols: list[str], max_stale_days: int, min_bars: int
    ) -> dict[str, list[str]]:
        """Which symbols are stale, short, or missing.

        The caller turns this into the data-quality gate: if the benchmark is
        stale, the run halts and no orders are generated.
        """
        report: dict[str, list[str]] = {"missing": [], "stale": [], "short": [], "ok": []}
        for sym in symbols:
            st = self.inspect(sym)
            if st.is_empty:
                report["missing"].append(sym)
            elif st.stale_days is not None and st.stale_days > max_stale_days:
                report["stale"].append(sym)
            elif st.rows < min_bars:
                report["short"].append(sym)
            else:
                report["ok"].append(sym)
        return report

    # -------------------------------------------------------------- manifest

    def read_manifest(self) -> dict[str, dict]:
        if not self._manifest_path.exists():
            return {}
        try:
            return json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("manifest corrupt; rebuilding from parquet files")
            return self.rebuild_manifest()

    def rebuild_manifest(self) -> dict[str, dict]:
        manifest = {}
        for sym in self.symbols():
            st = self.inspect(sym)
            manifest[sym] = self._manifest_entry(st)
        self._manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        return manifest

    def _update_manifest(
        self, symbol: str, stats: CacheStats, history_years: int | None = None
    ) -> None:
        manifest = self.read_manifest()
        entry = self._manifest_entry(stats)
        previous = manifest.get(symbol.upper(), {}).get("history_years")
        if history_years is not None or previous is not None:
            entry["history_years"] = history_years if history_years is not None else previous
        manifest[symbol.upper()] = entry
        self._manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )

    @staticmethod
    def _manifest_entry(st: CacheStats) -> dict:
        return {
            "rows": st.rows,
            "first_date": st.first_date.isoformat() if st.first_date else None,
            "last_date": st.last_date.isoformat() if st.last_date else None,
            "gaps": st.gaps,
            "suspicious_moves": st.suspicious_moves,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    # -------------------------------------------------------------- internals

    @staticmethod
    def _normalize(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df.columns = [str(c).lower() for c in df.columns]

        if not isinstance(df.index, pd.DatetimeIndex):
            for candidate in ("date", "datetime", "time"):
                if candidate in df.columns:
                    df = df.set_index(candidate)
                    break
            df.index = pd.to_datetime(df.index)

        df.index = df.index.normalize()
        df.index.name = "date"
        return df.sort_index()

    @staticmethod
    def _validate(symbol: str, df: pd.DataFrame) -> None:
        missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"{symbol}: missing columns {missing}")
        if df.index.has_duplicates:
            raise ValueError(f"{symbol}: duplicate dates in index")
        if not df.index.is_monotonic_increasing:
            raise ValueError(f"{symbol}: index not sorted")
        if (df["close"] <= 0).any():
            bad = df.index[df["close"] <= 0][:3]
            raise ValueError(f"{symbol}: non-positive close on {list(bad)}")
        if (df["high"] < df["low"]).any():
            raise ValueError(f"{symbol}: high < low on some bars")


# IBKR rejects day-denominated durations beyond a year; those need "N Y".
MAX_DAY_DURATION = 365
DEFAULT_OVERLAP_DAYS = 5


def next_fetch_duration(
    last: date | None,
    history_years: int,
    today: date | None = None,
    overlap_days: int = DEFAULT_OVERLAP_DAYS,
) -> str:
    """IBKR durationStr for the next incremental fetch.

    Full history when nothing is cached (or the gap is too long for a day
    duration); otherwise only the missing days plus a small overlap so
    restatements are picked up.
    """
    if last is None:
        return f"{history_years} Y"
    today = today or date.today()
    days = (today - last).days + overlap_days
    if days > MAX_DAY_DURATION:
        return f"{history_years} Y"
    return f"{max(days, overlap_days)} D"
