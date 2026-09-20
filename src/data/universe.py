"""
Universe selection: constituents file, filters, and the saved snapshot.

Everything here is local — the committed constituents CSV, the parquet cache
and SQLite. Nothing talks to IBKR, which is what lets the section 2 filters
be tested (and re-run for the audit trail) without a gateway. The one IBKR
input, contract details, is fetched by `src/data/refresh.py` into the
`contract_info` table before selection runs.

Symbols are IBKR-form everywhere past `load_constituents`: index lists write
`BRK.B`, IBKR calls it `BRK B`.
"""

from __future__ import annotations

import csv
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from src.config import Config
from src.data.cache import PriceCache

log = logging.getLogger(__name__)

CSV_COLUMNS = ("symbol", "name", "as_of_date")


class UniverseError(RuntimeError):
    """The universe cannot be built or loaded. Never swallowed: no universe,
    no fetch list, no trades."""


# ------------------------------------------------------------ constituents


@dataclass(frozen=True)
class Constituent:
    symbol: str          # IBKR form
    name: str
    as_of_date: date


def to_ibkr_symbol(symbol: str) -> str:
    """Index lists write share classes with a dot (BRK.B); IBKR uses a space."""
    return symbol.strip().upper().replace(".", " ")


def load_constituents(cfg: Config, today: date | None = None) -> list[Constituent]:
    """Read the committed S&P 500 list.

    Warns when the list is older than `constituents_max_age_days`. It does not
    halt: a slightly stale membership list costs selection quality, it cannot
    cause an unsafe order.
    """
    path = cfg.constituents_path
    if not path.exists():
        raise UniverseError(
            f"{path} not found. Run scripts/update_constituents.py, review the "
            "result, and commit it."
        )
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in CSV_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise UniverseError(f"{path}: missing columns {missing}")
        rows = [
            Constituent(
                to_ibkr_symbol(r["symbol"]), r["name"].strip(),
                date.fromisoformat(r["as_of_date"].strip()),
            )
            for r in reader
            if r["symbol"].strip()
        ]
    if not rows:
        raise UniverseError(f"{path}: no constituents")

    dupes = len(rows) - len({c.symbol for c in rows})
    if dupes:
        raise UniverseError(f"{path}: {dupes} duplicate symbols")

    age = ((today or date.today()) - min(c.as_of_date for c in rows)).days
    if age > cfg.universe.constituents_max_age_days:
        log.warning(
            "constituents list is %d days old (limit %d). Run "
            "scripts/update_constituents.py, review, and commit.",
            age, cfg.universe.constituents_max_age_days,
        )
    return rows


# ----------------------------------------------------------- contract info


@dataclass(frozen=True)
class ContractInfo:
    symbol: str
    con_id: int | None
    stock_type: str | None
    sector: str | None
    category: str | None
    subcategory: str | None
    long_name: str | None
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def open_db(cfg: Config) -> sqlite3.Connection:
    """Connect to the operational DB, insisting the schema is there."""
    if not cfg.db_path.exists():
        raise UniverseError(f"{cfg.db_path} not found. Run scripts/init_db.py.")
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    needed = {"contract_info", "universe_snapshots", "runs", "data_quality"}
    if not needed <= tables:
        conn.close()
        raise UniverseError(
            f"{cfg.db_path} is missing tables {sorted(needed - tables)}. "
            "Run scripts/init_db.py (it is idempotent)."
        )
    return conn


def save_contract_info(conn: sqlite3.Connection, infos: list[ContractInfo]) -> None:
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO contract_info
               (symbol, con_id, stock_type, sector, category, subcategory,
                long_name, fetched_at) VALUES (?,?,?,?,?,?,?,?)""",
            [(i.symbol, i.con_id, i.stock_type, i.sector, i.category,
              i.subcategory, i.long_name, i.fetched_at) for i in infos],
        )


def load_contract_info(conn: sqlite3.Connection) -> dict[str, ContractInfo]:
    return {
        r["symbol"]: ContractInfo(
            r["symbol"], r["con_id"], r["stock_type"], r["sector"], r["category"],
            r["subcategory"], r["long_name"], r["fetched_at"],
        )
        for r in conn.execute("SELECT * FROM contract_info")
    }


# --------------------------------------------------------------- selection

# How many symbols each end of the post-rebuild report shows.
REPORT_EDGE_COUNT = 5


def avg_dollar_volume(df, window: int, multiplier: float) -> float:
    """Mean of close x volume over the last `window` bars, in dollars.

    One definition, shared by the universe filter and the smoke report, so
    the number you eyeball is the number the filter used.
    """
    tail = df.iloc[-window:]
    return float((tail["close"] * tail["volume"]).mean()) * multiplier


@dataclass(frozen=True)
class UniverseRow:
    symbol: str
    name: str
    sector: str | None
    category: str | None
    stock_type: str
    avg_dollar_volume_20d: float
    price: float
    days_listed: int
    rank: int


@dataclass
class UniverseSelection:
    rows: list[UniverseRow]
    data_as_of: date
    dropped: dict[str, int]      # reason -> count; the funnel, for the log


def select_universe(
    cfg: Config,
    cache: PriceCache,
    constituents: list[Constituent],
    infos: dict[str, ContractInfo],
) -> UniverseSelection:
    """Apply the section 2 filters from the cache and cap by dollar volume.

    A pure function of its inputs: same cache, same list, same contract info,
    same answer. Ties in dollar volume break on symbol for that reason.
    """
    u = cfg.universe
    bench = cache.read(u.benchmark)
    if bench is None or bench.empty:
        raise UniverseError(
            f"{u.benchmark} is not cached; it anchors the staleness check. "
            "Refresh before building the universe."
        )
    data_as_of = bench.index[-1].date()

    dropped: dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    passing: list[UniverseRow] = []
    window = u.adv_window_days
    for c in constituents:
        df = cache.read(c.symbol)
        if df is None or df.empty:
            drop("no_cache")
            continue
        last = df.index[-1].date()
        if (data_as_of - last).days > cfg.data.max_stale_days:
            # Delisted, halted, or failed to fetch: its dollar volume is not current.
            drop("stale")
            continue
        if len(df) < window:
            drop("too_few_bars")
            continue

        price = float(df["close"].iloc[-1])
        if not price >= u.min_price:
            drop("price")
            continue

        adv = avg_dollar_volume(df, window, cfg.data.volume_multiplier)
        if not adv >= u.min_dollar_volume_20d:
            drop("dollar_volume")
            continue

        days_listed = (last - df.index[0].date()).days
        if days_listed < u.min_days_listed:
            drop("days_listed")
            continue

        info = infos.get(c.symbol)
        if info is None:
            drop("no_contract_info")   # fail closed: cannot verify it is common stock
            continue
        if (info.stock_type or "").upper() not in {t.upper() for t in u.security_types}:
            drop("stock_type")
            continue

        passing.append(UniverseRow(
            c.symbol, c.name, info.sector, info.category, info.stock_type or "",
            adv, price, days_listed, rank=0,
        ))

    passing.sort(key=lambda r: (-r.avg_dollar_volume_20d, r.symbol))
    if len(passing) > u.max_symbols:
        dropped["over_cap"] = len(passing) - u.max_symbols
    chosen = [
        replace(r, rank=i)
        for i, r in enumerate(passing[: u.max_symbols], start=1)
    ]
    return UniverseSelection(chosen, data_as_of, dropped)


# ------------------------------------------------------- point-in-time (backtest)


def point_in_time_universe(
    closes: pd.DataFrame,
    volumes: pd.DataFrame,
    cfg: Config,
    allowed: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Who was in the universe on each date, from data available on that date.

    Spec 2.3. The saved snapshot is today's top 150 by dollar volume, which is
    hindsight: stocks are big today largely because they went up. Here, for each
    date D: the section 2 filters as of D (20-day average dollar volume,
    price, calendar days since the first cached bar, a bar on D), then the top
    `max_symbols` by dollar volume, ties broken by symbol.

    Row D depends only on rows <= D: rolling windows are trailing, and
    `days_listed` needs only the first bar, which for any D on or after it is
    fixed. A window touching a missing bar is NaN and fails the filter (a halted
    name is not liquid on paper).

    `allowed` is the set of symbols whose security type passes (today's label;
    None = no type filter). The benchmark column, if present, is never a member.
    Returns a bool dates x symbols frame.
    """
    u = cfg.universe
    cols = sorted(c for c in closes.columns if c != u.benchmark)
    px, vol = closes[cols], volumes.reindex(index=closes.index, columns=cols)

    window = u.adv_window_days
    adv = (px * vol * cfg.data.volume_multiplier).rolling(window, min_periods=window).mean()

    first_bar = px.notna().idxmax().where(px.notna().any())          # NaT if never traded
    dates = px.index.values.astype("datetime64[D]")[:, None]
    listed = (dates - first_bar.values.astype("datetime64[D]")[None, :]) / np.timedelta64(1, "D")

    passes = (
        (adv >= u.min_dollar_volume_20d)
        & (px >= u.min_price)
        & pd.DataFrame(listed >= u.min_days_listed, index=px.index, columns=cols)
    )
    if allowed is not None:
        allowed = set(allowed)
        passes = passes & pd.Series({c: c in allowed for c in cols})
    rank = adv.where(passes).rank(axis=1, ascending=False, method="first")
    return (rank <= u.max_symbols).astype(bool)


def todays_universe(
    closes: pd.DataFrame, volumes: pd.DataFrame, cfg: Config,
    allowed: Iterable[str] | None = None,
) -> pd.DataFrame:
    """The membership of the LAST date, applied to every date.

    This is the hindsight universe that `point_in_time_universe` replaces. It
    exists so the backtest can be broken on purpose and shown to look
    different; nothing that reports results should use it.
    """
    pit = point_in_time_universe(closes, volumes, cfg, allowed)
    return pd.DataFrame({c: bool(pit[c].iloc[-1]) for c in pit.columns}, index=pit.index)


# ---------------------------------------------------------------- snapshot


def save_snapshot(
    conn: sqlite3.Connection,
    sel: UniverseSelection,
    snapshot_date: date,
    constituents_as_of: date | None,
) -> None:
    """Persist the chosen universe. A same-day rebuild replaces that day's rows
    in one transaction, so a reader never sees half of a list."""
    with conn:
        conn.execute(
            "DELETE FROM universe_snapshots WHERE snapshot_date = ?",
            (snapshot_date.isoformat(),),
        )
        conn.executemany(
            """INSERT INTO universe_snapshots
               (snapshot_date, symbol, rank, name, sector, category, stock_type,
                avg_dollar_volume_20d, price, days_listed, constituents_as_of,
                data_as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (snapshot_date.isoformat(), r.symbol, r.rank, r.name, r.sector,
                 r.category, r.stock_type, r.avg_dollar_volume_20d, r.price,
                 r.days_listed,
                 constituents_as_of.isoformat() if constituents_as_of else None,
                 sel.data_as_of.isoformat())
                for r in sel.rows
            ],
        )


def load_universe(conn: sqlite3.Connection, as_of: date | None = None) -> list[str]:
    """Symbols of the snapshot in force on `as_of` (default: latest)."""
    if as_of is None:
        row = conn.execute("SELECT MAX(snapshot_date) FROM universe_snapshots").fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(snapshot_date) FROM universe_snapshots WHERE snapshot_date <= ?",
            (as_of.isoformat(),),
        ).fetchone()
    snap = row[0]
    if snap is None:
        raise UniverseError(
            "no universe snapshot saved. Run: python -m src.data.refresh --rebuild-universe"
        )
    return [
        r["symbol"] for r in conn.execute(
            "SELECT symbol FROM universe_snapshots WHERE snapshot_date = ? ORDER BY rank",
            (snap,),
        )
    ]



def format_universe_report(sel: UniverseSelection, n_constituents: int) -> str:
    """Top and bottom of the chosen list, and what each filter removed.

    Printed after a rebuild so a broken input (wrong volume units, a bad
    sector feed) is visible at a glance instead of buried in a snapshot table.
    """
    def line(r: UniverseRow) -> str:
        return (f"  {r.rank:>3}  {r.symbol:<8} ${r.avg_dollar_volume_20d / 1e6:>10,.1f}M/day"
                f"  {r.sector or '-'}")

    edge = REPORT_EDGE_COUNT
    out = [f"Universe: {len(sel.rows)} chosen from {n_constituents} constituents "
           f"(data as of {sel.data_as_of})", "", f"Top {edge} by dollar volume:"]
    out += [line(r) for r in sel.rows[:edge]]
    out += ["", f"Bottom {edge} of the selection:"]
    out += [line(r) for r in sel.rows[-edge:]]
    out += ["", "Dropped by filter:"]
    out += [f"  {reason:<18} {count}" for reason, count in
            sorted(sel.dropped.items(), key=lambda kv: -kv[1])] or ["  (none)"]
    return "\n".join(out)
