"""
Price cache refresh. Entry point: `make refresh`.

Two cadences (spec section 2.2):

    weekly      refresh the saved universe (~150) + SPY       ~25 min at 6/min
    quarterly   `--rebuild-universe`: refresh every S&P 500   ~83 min at 6/min
                constituent, fetch contract details, apply the section 2
                filters from the cache, save the chosen 150

Full history is fetched once (~15 years); every run after that pulls only the
missing bars plus a small overlap. The overlap doubles as the restatement
check (spec section 3.1): if adjusted closes we already hold have moved, the
symbol's history is thrown away and refetched, because stitching restated
history onto old history leaves a jump at the join.

Ends with the DATA GATE: if SPY is staler than data.max_stale_days the run is
recorded as halted, a failing data_quality row is written, and the exit code
is non-zero. Only benchmark staleness halts; coverage and jump counts are
recorded but do not by themselves stop the run.

This is the only module that talks to IBKR for history, so pacing is managed
in exactly one place.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import pandas as pd
from ib_async import Stock

from src.config import DEFAULT_CONFIG_PATH, Config, load_config
from src.data.cache import PriceCache, next_fetch_duration
from src.data.pacing import PacingLimiter
from src.data.universe import (
    ContractInfo,
    UniverseError,
    load_constituents,
    load_contract_info,
    load_universe,
    open_db,
    save_contract_info,
    save_snapshot,
    select_universe,
)

log = logging.getLogger(__name__)

EXCHANGE = "SMART"
BAR_COLUMNS = ["open", "high", "low", "close", "volume"]

# IBKR protocol codes, not tunables. 420 is the documented pacing violation;
# some gateways report the same condition as 162 with a "pacing" message.
ERR_PACING = 420
ERR_HISTORICAL_SERVICE = 162
INFO_CODES = range(2100, 2200)   # farm-connection status messages, not errors

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_HALTED = 2


class FetchError(RuntimeError):
    def __init__(self, symbol: str, code: int | None, message: str) -> None:
        super().__init__(f"{symbol}: IBKR error {code}: {message}")
        self.symbol, self.code, self.message = symbol, code, message


class PacingViolation(FetchError):
    """IBKR said we are too fast. The limiter is already cooling down."""


# ------------------------------------------------------------------ universe


def build_universe(cfg: Config) -> list[str]:
    """Apply the section 2 filters and cap at max_symbols by dollar volume.

    Reads the constituents CSV, the parquet cache and the `contract_info`
    table, saves the result as a dated snapshot in `universe_snapshots`, and
    returns the chosen symbols (benchmark excluded; it is always fetched).
    Local-only: run it after a refresh of all constituents.

    NOTE on survivorship bias: this builds from TODAY's index membership.
    Every company that failed out of the index is silently excluded, which
    makes the backtest optimistic by an unmeasured amount. Recorded in spec
    section 11 weakness 4. Point-in-time constituent data is the fix, and it
    costs money.
    """
    constituents = load_constituents(cfg)
    cache = PriceCache(cfg.cache_path)
    conn = open_db(cfg)
    try:
        sel = select_universe(cfg, cache, constituents, load_contract_info(conn))
        log.info(
            "universe: %d chosen from %d constituents; dropped %s",
            len(sel.rows), len(constituents), sel.dropped or "none",
        )
        if not sel.rows:
            # Zero survivors means a broken input (volume units, empty cache),
            # not a market with no liquid stocks. Do not overwrite a good snapshot.
            raise UniverseError(
                f"no symbol passed the section 2 filters ({sel.dropped}). "
                "Check data.volume_multiplier and that the cache is populated."
            )
        as_of = min(c.as_of_date for c in constituents)
        save_snapshot(conn, sel, date.today(), as_of)
    finally:
        conn.close()
    return [r.symbol for r in sel.rows]


# ------------------------------------------------------------------- fetching


def _pacing_error(errors: list[tuple[int, str]]) -> tuple[int, str] | None:
    for code, msg in errors:
        if code == ERR_PACING or (
            code == ERR_HISTORICAL_SERVICE and "pacing" in msg.lower()
        ):
            return code, msg
    return None


def fetch_symbol(
    ib, symbol: str, duration: str, cfg: Config, limiter: PacingLimiter
) -> pd.DataFrame:
    """One reqHistoricalData call. Acquires the limiter itself, every time.

    endDateTime is always empty: IBKR rejects a dated end for ADJUSTED_LAST,
    so every request ends "now" and is sized by `duration` alone.

    On IBKR error 420 (pacing violation), calls limiter.enter_cooldown() and
    raises PacingViolation. It does not retry: retrying immediately turns a
    cooldown into a disconnect. Any other error with no bars returned raises
    FetchError. Returns bars indexed by date with open/high/low/close/volume.

    Signature note: `limiter` was added when the caller-acquires contract was
    dropped. One place owning acquire-then-request means no request can go out
    unpaced.
    """
    errors: list[tuple[int, str]] = []

    def on_error(req_id, code, msg, contract=None) -> None:
        errors.append((int(code), str(msg)))

    contract = Stock(symbol, EXCHANGE, cfg.account.currency)
    limiter.acquire()
    ib.errorEvent += on_error
    try:
        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=cfg.data.bar_size,
            whatToShow=cfg.data.what_to_show,
            useRTH=True,
            formatDate=1,
        )
    finally:
        ib.errorEvent -= on_error

    pacing = _pacing_error(errors)
    if pacing:
        limiter.enter_cooldown()
        raise PacingViolation(symbol, *pacing)

    if not bars:
        real = [(c, m) for c, m in errors if c not in INFO_CODES]
        code, msg = real[0] if real else (None, "no bars returned")
        raise FetchError(symbol, code, msg)

    df = pd.DataFrame(
        {col: [getattr(b, col) for b in bars] for col in BAR_COLUMNS},
        index=pd.to_datetime([b.date for b in bars]),
    )
    df.index.name = "date"
    return df


@dataclass
class SymbolUpdate:
    symbol: str
    action: str            # full | incremental | refetch
    reason: str | None = None   # why a refetch happened


def _restatement_reason(
    existing: pd.DataFrame, fetched: pd.DataFrame, tolerance: float
) -> str | None:
    """Why the cached history can no longer be trusted, or None if it can."""
    common = existing.index.intersection(fetched.index)
    if common.empty:
        # Cannot verify, so do not trust it. Fail closed.
        return "no overlap between cache and fetch; cannot verify history"
    old = existing.loc[common, "close"]
    new = fetched.loc[common, "close"]
    rel = ((new - old) / old).abs()
    bad = rel > tolerance
    if bad.any():
        worst = rel.idxmax()
        return (
            f"{int(bad.sum())}/{len(common)} overlapping closes moved more than "
            f"{tolerance:.2%} (max {rel.max():.2%} on {worst.date()}); adjusted "
            "history was restated"
        )
    return None


def update_symbol(
    ib, symbol: str, cache: PriceCache, limiter: PacingLimiter, cfg: Config
) -> SymbolUpdate:
    """Bring one symbol's cache up to date.

    Nothing cached -> full history. Otherwise fetch the missing bars plus an
    overlap and compare the overlap to the cache. If it disagrees, discard the
    cached history and refetch in full (one extra request; pacing counts
    requests, not bars). If the full refetch fails, the old cache is left as
    it was.
    """
    full = next_fetch_duration(None, cfg.data.history_years)
    existing = cache.read(symbol)

    if existing is None or existing.empty:
        cache.write(symbol, fetch_symbol(ib, symbol, full, cfg, limiter))
        return SymbolUpdate(symbol, "full")

    duration = next_fetch_duration(
        existing.index[-1].date(), cfg.data.history_years,
        overlap_days=cfg.data.overlap_days,
    )
    if duration == full:
        reason = "cache too old for an incremental request; refetching in full"
    else:
        fetched = fetch_symbol(ib, symbol, duration, cfg, limiter)
        reason = _restatement_reason(existing, fetched, cfg.data.restatement_tolerance)
        if reason is None:
            cache.merge(symbol, fetched)
            return SymbolUpdate(symbol, "incremental")

    log.warning("REFETCH %s: %s", symbol, reason)
    cache.write(symbol, fetch_symbol(ib, symbol, full, cfg, limiter))
    return SymbolUpdate(symbol, "refetch", reason)


def fetch_contract_info(ib, symbol: str, cfg: Config) -> ContractInfo | None:
    """Contract details for one symbol: stock type and sector.

    Not a historical-data request, so it does not use the pacing limiter.
    Returns None when IBKR knows no such contract (dropped from the universe).
    """
    try:
        details = ib.reqContractDetails(Stock(symbol, EXCHANGE, cfg.account.currency))
    except Exception as exc:  # noqa: BLE001 - one bad symbol must not end a rebuild
        log.warning("contract details failed for %s: %s", symbol, exc)
        return None
    if not details:
        log.warning("no contract details for %s", symbol)
        return None
    d = details[0]
    return ContractInfo(
        symbol=symbol,
        con_id=getattr(d.contract, "conId", None),
        stock_type=d.stockType or None,
        sector=d.industry or None,
        category=d.category or None,
        subcategory=d.subcategory or None,
        long_name=d.longName or None,
    )


# --------------------------------------------------------------------- refresh


@dataclass
class RefreshSummary:
    symbols_expected: int = 0
    updated: int = 0
    failed: dict[str, str] = field(default_factory=dict)
    refetches: list[tuple[str, str]] = field(default_factory=list)
    pacing_violations: int = 0

    def as_detail(self) -> dict:
        return {
            "updated": self.updated,
            "failed": self.failed,
            "refetch_count": len(self.refetches),
            "refetches": [{"symbol": s, "reason": r} for s, r in self.refetches],
            "pacing_violations": self.pacing_violations,
        }


def _update_all(
    ib, symbols: list[str], cache: PriceCache, limiter: PacingLimiter, cfg: Config
) -> RefreshSummary:
    summary = RefreshSummary(symbols_expected=len(symbols))
    for i, sym in enumerate(symbols, start=1):
        try:
            res = update_symbol(ib, sym, cache, limiter, cfg)
        except PacingViolation as exc:
            # Not retried. The limiter now blocks the next symbol for ten minutes.
            summary.pacing_violations += 1
            summary.failed[sym] = str(exc)
            log.error("%s -- not retrying this run; waiting out the cooldown", exc)
            continue
        except (FetchError, ValueError) as exc:
            summary.failed[sym] = str(exc)
            log.error("%s failed: %s", sym, exc)
            continue
        summary.updated += 1
        if res.action == "refetch":
            summary.refetches.append((sym, res.reason or ""))
        log.info("[%d/%d] %s %s", i, len(symbols), sym, res.action)
    return summary


def _start_run(conn: sqlite3.Connection, cfg: Config, notes: str) -> str:
    run_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            """INSERT INTO runs (run_id, started_at, run_type, strategy_name,
               strategy_version, config_hash, mode, status, notes)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (run_id, datetime.now(timezone.utc).isoformat(), "refresh",
             cfg.strategy.name, cfg.strategy.version,
             hashlib.sha256(DEFAULT_CONFIG_PATH.read_bytes()).hexdigest(),
             "paper" if cfg.account.paper_trading else "live", "running", notes),
        )
    return run_id


def _finish_run(
    conn: sqlite3.Connection, run_id: str, status: str,
    halt_reason: str | None = None, error: str | None = None,
) -> None:
    with conn:
        conn.execute(
            "UPDATE runs SET finished_at=?, status=?, halt_reason=?, error=? WHERE run_id=?",
            (datetime.now(timezone.utc).isoformat(), status, halt_reason, error, run_id),
        )


def _data_gate(
    conn: sqlite3.Connection, run_id: str, cache: PriceCache, symbols: list[str],
    summary: RefreshSummary, cfg: Config,
) -> tuple[bool, str | None]:
    """The data gate. Returns (passed, halt_reason). Writes a data_quality row
    either way. A benchmark that is missing or unreadable fails: a check that
    cannot be evaluated counts as a failure."""
    bench = cache.inspect(cfg.universe.benchmark)
    stale_days = bench.stale_days
    reason = None
    if bench.is_empty or stale_days is None:
        reason = f"{cfg.universe.benchmark} has no cached bars"
    elif stale_days > cfg.data.max_stale_days:
        reason = (
            f"{cfg.universe.benchmark} last bar {bench.last_date} is {stale_days} "
            f"days old (limit {cfg.data.max_stale_days})"
        )
    passed = reason is None

    report = cache.staleness_report(
        symbols, cfg.data.max_stale_days, cfg.data.min_bars_required
    )
    manifest = cache.read_manifest()
    jumps = sum(manifest.get(s.upper(), {}).get("suspicious_moves", 0) for s in symbols)
    detail = summary.as_detail() | {
        "missing": report["missing"], "halt_reason": reason,
    }
    with conn:
        conn.execute(
            """INSERT INTO data_quality (run_id, checked_at, symbols_expected,
               symbols_fetched, symbols_stale, symbols_short, benchmark_bar_date,
               staleness_days, price_jumps, duplicate_bars, passed, detail)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, datetime.now(timezone.utc).isoformat(), summary.symbols_expected,
             summary.updated, len(report["stale"]), len(report["short"]),
             bench.last_date.isoformat() if bench.last_date else None,
             stale_days, jumps, 0, int(passed), json.dumps(detail)),
        )
    return passed, reason


def _connect(cfg: Config):
    from ib_async import IB

    ib = IB()
    # Read-only: a refresh must never be able to place an order.
    ib.connect(cfg.execution.ibkr_host, cfg.execution.ibkr_port,
               clientId=cfg.execution.client_id, readonly=True)
    return ib


def refresh(
    cfg: Config | None = None,
    *,
    ib=None,
    limiter: PacingLimiter | None = None,
    rebuild_universe: bool = False,
) -> int:
    """Update the cache. Returns a process exit code (0 ok, 1 error, 2 halted
    by the data gate).

    `ib` and `limiter` are injectable so the flow can be tested without a
    gateway.
    """
    cfg = cfg or load_config()
    cache = PriceCache(cfg.cache_path)
    limiter = limiter or PacingLimiter(requests_per_minute=cfg.data.ibkr_requests_per_minute)
    bench = cfg.universe.benchmark

    conn = open_db(cfg)
    run_id = _start_run(conn, cfg, "universe rebuild" if rebuild_universe else "weekly")
    owns_ib = ib is None
    try:
        if rebuild_universe:
            constituents = load_constituents(cfg)
            tickers = [c.symbol for c in constituents]
        else:
            tickers = load_universe(conn)
        symbols = [bench] + [s for s in tickers if s != bench]   # benchmark first

        eta_min = limiter.estimate_seconds(len(symbols)) / 60
        log.info("refreshing %d symbols, ETA ~%.0f min (+1 request per refetch)",
                 len(symbols), eta_min)

        if ib is None:
            ib = _connect(cfg)
        summary = _update_all(ib, symbols, cache, limiter, cfg)
        passed, halt_reason = _data_gate(conn, run_id, cache, symbols, summary, cfg)

        log.info(
            "refresh summary: %d/%d updated, %d failed, %d refetched (restated), "
            "%d pacing violations",
            summary.updated, summary.symbols_expected, len(summary.failed),
            len(summary.refetches), summary.pacing_violations,
        )
        for sym, why in summary.refetches:
            log.info("  refetch %s: %s", sym, why)

        if not passed:
            log.error("DATA GATE FAILED: %s. Halting; no orders.", halt_reason)
            _finish_run(conn, run_id, "halted", halt_reason=halt_reason)
            return EXIT_HALTED

        if rebuild_universe:
            infos = [i for s in tickers if (i := fetch_contract_info(ib, s, cfg))]
            save_contract_info(conn, infos)
            chosen = build_universe(cfg)
            log.info("universe rebuilt: %d symbols saved", len(chosen))

        _finish_run(conn, run_id, "ok")
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - record the failure, then report it
        log.exception("refresh failed")
        _finish_run(conn, run_id, "failed", error=f"{type(exc).__name__}: {exc}")
        return EXIT_ERROR
    finally:
        if owns_ib and ib is not None:
            ib.disconnect()
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh the price cache.")
    parser.add_argument(
        "--rebuild-universe", action="store_true",
        help="quarterly: refresh all constituents and re-select the universe",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return refresh(rebuild_universe=args.rebuild_universe)
    except UniverseError as exc:
        log.error("%s", exc)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
