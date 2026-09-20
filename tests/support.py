"""Test doubles for IBKR. Nothing here needs IB Gateway."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd


def recent_bars(n: int = 400, end: date | None = None, seed: int = 0,
                price: float = 100.0, volume: float = 1e6) -> pd.DataFrame:
    """Synthetic daily bars ending on the latest business day on/before `end`."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp(end or date.today()), periods=n)
    close = price * np.exp(np.cumsum(rng.normal(0.0004, 0.01, n)))
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.004,
            "low": close * 0.996,
            "close": close,
            "volume": np.full(n, volume),
        },
        index=idx.rename("date"),
    )


class Event(list):
    """Minimal stand-in for eventkit.Event: supports `ev += fn` / `ev -= fn`."""

    def __iadd__(self, fn):
        self.append(fn)
        return self

    def __isub__(self, fn):
        self.remove(fn)
        return self

    def emit(self, *args) -> None:
        for fn in list(self):
            fn(*args)


class FakeLimiter:
    """Records acquire/cooldown into a shared event log; never sleeps."""

    def __init__(self, events: list | None = None) -> None:
        self.events = events if events is not None else []
        self.cooldowns = 0

    def acquire(self, timeout=None) -> None:
        self.events.append("acquire")

    def enter_cooldown(self, seconds=None) -> None:
        self.cooldowns += 1
        self.events.append("cooldown")

    def estimate_seconds(self, n: int) -> float:
        return 0.0


class FakeIB:
    """Serves `history[symbol]` like IBKR would.

    "N Y" durations return everything; "N D" returns bars from N calendar days
    ago. `scale[symbol]` multiplies OHLC, simulating a restatement.
    `errors[symbol]` is a list of (code, message) to emit instead of bars.
    """

    def __init__(self, history: dict[str, pd.DataFrame], events: list | None = None) -> None:
        self.history = history
        self.scale: dict[str, float] = {}
        self.errors: dict[str, list[tuple[int, str]]] = {}
        self.info_errors: list[tuple[int, str]] = []
        self.details: dict[str, SimpleNamespace] = {}
        self.errorEvent = Event()
        self.requests: list[tuple[str, dict]] = []
        self.events = events if events is not None else []
        self.disconnected = False
        self.max_requests: int | None = None    # fail fast on a runaway retry loop

    def reqHistoricalData(self, contract, **kw):
        if self.max_requests is not None and len(self.requests) >= self.max_requests:
            raise AssertionError(f"unexpected extra request for {contract.symbol} (retry?)")
        self.events.append("request")
        self.requests.append((contract.symbol, kw))
        for code, msg in self.info_errors:
            self.errorEvent.emit(1, code, msg, None)
        if contract.symbol in self.errors:
            for code, msg in self.errors[contract.symbol]:
                self.errorEvent.emit(1, code, msg, None)
            return []
        df = self.history.get(contract.symbol)
        if df is None:
            self.errorEvent.emit(1, 200, "No security definition has been found", None)
            return []
        num, unit = kw["durationStr"].split()
        if unit == "D":
            cutoff = pd.Timestamp(date.today() - timedelta(days=int(num)))
            df = df[df.index >= cutoff]
        df = df.copy()
        k = self.scale.get(contract.symbol, 1.0)
        for col in ("open", "high", "low", "close"):
            df[col] = df[col] * k
        return [
            SimpleNamespace(date=ts.date(), open=r.open, high=r.high, low=r.low,
                            close=r.close, volume=r.volume)
            for ts, r in df.iterrows()
        ]

    def reqContractDetails(self, contract):
        d = self.details.get(contract.symbol)
        return [d] if d is not None else []

    def disconnect(self) -> None:
        self.disconnected = True


def details(stock_type="COMMON", industry="Technology", category="Software",
            subcategory="Apps", name="Acme", con_id=1) -> SimpleNamespace:
    return SimpleNamespace(
        contract=SimpleNamespace(conId=con_id), stockType=stock_type,
        industry=industry, category=category, subcategory=subcategory, longName=name,
    )


class FakeClock:
    """Injectable clock for PacingLimiter: sleeping advances time instantly, so a
    ten-minute cooldown costs zero real seconds."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.now += seconds
