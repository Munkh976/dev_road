"""
Compute this week's signals from the parquet cache. Entry point: `make signals`.

Reads the saved universe and the cache, computes every signal as of SPY's last
bar, writes one `signals` row per symbol under a `runs` row (with the
config hash), and prints the regime and the top of the ranking.

IO lives here so that `signals.py` can stay pure and be shared, unchanged,
with the backtest.

Fail closed: if SPY is missing or stale (data.max_stale_days) the run is
recorded as halted and writes no signals. A stale benchmark means the regime
is not current, and an out-of-date "market on" is the one wrong answer that
loses money.
"""

from __future__ import annotations

import logging
import sqlite3

import pandas as pd

from src.config import Config, load_config
from src.data.cache import PriceCache
from src.data.refresh import EXIT_ERROR, EXIT_HALTED, EXIT_OK
from src.data.universe import UniverseError, load_universe, open_db
from src.runlog import finish_run, start_run
from src.strategy.signals import SignalFrame, compute

log = logging.getLogger(__name__)

RUN_TYPE = "signals"
TOP_N_SHOWN = 10


def _load_panels(cache: PriceCache, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """One aligned dates x symbols matrix per field. Gaps stay NaN."""
    fields = ("close", "high", "low", "volume")
    return {f: cache.load_matrix(symbols, f) for f in fields}


def _dollar_volume(panels: dict[str, pd.DataFrame], cfg: Config, upto: pd.Timestamp) -> pd.Series:
    """Same definition as the universe filter: mean of close x volume over the
    last adv_window_days bars, times the volume multiplier."""
    dv = (panels["close"] * panels["volume"]).loc[:upto].iloc[-cfg.universe.adv_window_days:]
    return dv.mean() * cfg.data.volume_multiplier


def _sectors(conn: sqlite3.Connection) -> dict[str, str | None]:
    return {
        r["symbol"]: r["sector"]
        for r in conn.execute(
            """SELECT symbol, sector FROM universe_snapshots
               WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM universe_snapshots)"""
        )
    }


def _none_if_nan(value):
    return None if pd.isna(value) else value


def write_signals(
    conn: sqlite3.Connection, run_id: str, frame: SignalFrame,
    sectors: dict[str, str | None], dollar_volume: pd.Series,
) -> int:
    rows = [
        (
            run_id, frame.signal_date.date().isoformat(), symbol, float(r["close"]),
            _none_if_nan(r["mom_6m"]), _none_if_nan(r["mom_12m"]),
            _none_if_nan(r["blended_momentum"]), _none_if_nan(r["vol_63"]),
            _none_if_nan(r["score"]),
            None if pd.isna(r["rank"]) else int(r["rank"]),
            _none_if_nan(r["atr_20"]),
            _none_if_nan(dollar_volume.get(symbol)), sectors.get(symbol),
            int(frame.market_on),
        )
        for symbol, r in frame.table.iterrows()
    ]
    with conn:
        conn.executemany(
            """INSERT INTO signals (run_id, signal_date, symbol, close, mom_6m,
               mom_12m, blended_momentum, vol_63, score, rank, atr_20,
               dollar_volume_20d, sector, market_on)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
    return len(rows)


def format_report(frame: SignalFrame, sectors: dict[str, str | None], n_universe: int) -> str:
    t = frame.table
    ranked = int(t["rank"].notna().sum())
    if frame.benchmark_close is None or frame.benchmark_sma is None:
        why = "benchmark not evaluable"
    else:
        rel = ">" if frame.benchmark_close > frame.benchmark_sma else "<="
        why = f"benchmark {frame.benchmark_close:,.2f} {rel} SMA {frame.benchmark_sma:,.2f}"
    out = [
        f"Signals as of {frame.signal_date.date()}",
        f"Regime: {'RISK-ON' if frame.market_on else 'RISK-OFF (hold cash)'}  ({why})",
        f"Ranked {ranked} of {n_universe} universe symbols "
        f"({n_universe - ranked} unranked: too little history or a missing bar)",
        "",
        f"Top {TOP_N_SHOWN}:",
        f"  {'rank':>4}  {'symbol':<8} {'close':>9} {'momentum':>9} {'vol':>7} "
        f"{'score':>7} {'atr':>7}  sector",
    ]
    for symbol in frame.top(TOP_N_SHOWN):
        r = t.loc[symbol]
        out.append(
            f"  {int(r['rank']):>4}  {symbol:<8} {r['close']:>9,.2f} "
            f"{r['blended_momentum']:>+9.1%} {r['vol_63']:>7.1%} {r['score']:>7.2f} "
            f"{r['atr_20']:>7.2f}  {sectors.get(symbol) or '-'}"
        )
    return "\n".join(out)


def run(cfg: Config | None = None) -> int:
    """Returns a process exit code: 0 ok, 1 error, 2 halted on stale data."""
    cfg = cfg or load_config()
    cache = PriceCache(cfg.cache_path)
    bench = cfg.universe.benchmark
    conn = open_db(cfg)
    run_id = start_run(conn, cfg, RUN_TYPE, "")
    try:
        symbols = [s for s in load_universe(conn) if s != bench]

        stats = cache.inspect(bench)
        if stats.is_empty or stats.stale_days is None:
            reason = f"{bench} has no cached bars"
        elif stats.stale_days > cfg.data.max_stale_days:
            reason = (f"{bench} last bar {stats.last_date} is {stats.stale_days} days "
                      f"old (limit {cfg.data.max_stale_days})")
        else:
            reason = None
        if reason:
            log.error("DATA GATE FAILED: %s. No signals written.", reason)
            finish_run(conn, run_id, "halted", halt_reason=reason)
            return EXIT_HALTED

        panels = _load_panels(cache, [bench] + symbols)
        missing = [s for s in symbols if s not in panels["close"].columns]
        if missing:
            log.warning("%d universe symbols have no cache and are skipped: %s",
                        len(missing), missing)
        signal_date = pd.Timestamp(stats.last_date)
        frame = compute(
            panels["close"], panels["high"], panels["low"], cfg,
            as_of=signal_date, universe=symbols,
        )
        sectors = _sectors(conn)
        report = format_report(frame, sectors, len(symbols))
        n = write_signals(conn, run_id, frame, sectors,
                          _dollar_volume(panels, cfg, frame.signal_date))
        finish_run(conn, run_id, "ok")
        log.info("wrote %d signal rows (run %s)", n, run_id)
        print(report)
        return EXIT_OK
    except UniverseError as exc:
        log.error("%s", exc)
        finish_run(conn, run_id, "failed", error=f"{type(exc).__name__}: {exc}")
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - record the failure, then report it
        log.exception("signals failed")
        finish_run(conn, run_id, "failed", error=f"{type(exc).__name__}: {exc}")
        return EXIT_ERROR
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
