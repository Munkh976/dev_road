"""
Weekly price cache refresh. Entry point: `make refresh`.

Full history is fetched once (~15 years); every run after that pulls only the
missing bars plus a small overlap for restatements. At 6 symbols/minute,
150 names take about 25 minutes. The run prints an ETA up front so you are
not watching a silent terminal.

Ends with the DATA GATE: if the benchmark is stale or coverage is too thin,
the run is marked failed and no signals or orders are generated downstream.

STATUS: stub. Flow is fixed; the IBKR calls are next.
"""

from __future__ import annotations

import logging

from src.config import Config, load_config
from src.data.cache import PriceCache
from src.data.pacing import PacingLimiter

log = logging.getLogger(__name__)


def build_universe(cfg: Config) -> list[str]:
    """Apply the section 2 filters and cap at max_symbols by dollar volume.

    NOTE on survivorship bias: this builds from TODAY's index membership.
    Every company that failed out of the index is silently excluded, which
    makes the backtest optimistic by an unmeasured amount. Recorded in spec
    section 11 weakness 4. Point-in-time constituent data is the fix, and it
    costs money.
    """
    raise NotImplementedError


def fetch_symbol(ib, symbol: str, duration: str, cfg: Config):
    """One reqHistoricalData call. Caller must have acquired the limiter.

    On IBKR error 420 (pacing violation), call limiter.enter_cooldown() and
    do not retry for ten minutes. Retrying immediately turns a cooldown into
    a disconnect.
    """
    raise NotImplementedError


def refresh(cfg: Config | None = None) -> int:
    """Update the cache. Returns a process exit code."""
    cfg = cfg or load_config()
    cache = PriceCache(cfg.cache_path)
    limiter = PacingLimiter(requests_per_minute=cfg.data.ibkr_requests_per_minute)

    symbols = build_universe(cfg)
    eta_min = limiter.estimate_seconds(len(symbols)) / 60
    log.info("refreshing %d symbols, ETA ~%.0f min", len(symbols), eta_min)
    raise NotImplementedError


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return refresh()


if __name__ == "__main__":
    raise SystemExit(main())
