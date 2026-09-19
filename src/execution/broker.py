"""
Broker abstraction.

Two reasons this interface exists rather than calling ib_async directly:

1. The backtest and the paper/live paths run the same strategy code. A
   simulated broker satisfying this interface means the strategy has no idea
   which one it is talking to.
2. Positions and balances are READ from the broker, never stored. There is no
   internal position table to drift out of sync, which is why this system has
   no reconciliation engine.

The one piece of state the broker cannot give us is `position_high` (highest
close since entry, for the trailing stop). That lives in SQLite's
position_state table and nowhere else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class OrderAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    BUILT = "BUILT"
    VALIDATED = "VALIDATED"
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Account:
    account_id: str
    net_liquidation: float
    total_cash: float
    buying_power: float
    gross_position_value: float
    currency: str = "USD"
    as_of: datetime | None = None

    @property
    def gross_exposure(self) -> float:
        if self.net_liquidation <= 0:
            return 0.0
        return self.gross_position_value / self.net_liquidation


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: float
    average_cost: float
    market_price: float
    market_value: float
    unrealized_pnl: float
    currency: str = "USD"

    @property
    def is_open(self) -> bool:
        return abs(self.quantity) > 1e-9


@dataclass(frozen=True)
class Quote:
    symbol: str
    last: float
    bid: float | None = None
    ask: float | None = None
    as_of: datetime | None = None

    @property
    def spread_bps(self) -> float | None:
        if self.bid is None or self.ask is None or self.last <= 0:
            return None
        return (self.ask - self.bid) / self.last * 10_000


@dataclass
class OrderRequest:
    symbol: str
    action: OrderAction
    quantity: float
    order_type: str = "LIMIT"
    limit_price: float | None = None
    time_in_force: str = "DAY"
    client_ref: str | None = None      # our order_id, for correlation

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive; direction comes from action")
        if self.order_type.upper() == "MARKET":
            raise ValueError(
                "Market orders are refused at the interface level. A weekend "
                "gap or a halt fills a market order at any price."
            )
        if self.order_type.upper() == "LIMIT" and self.limit_price is None:
            raise ValueError("LIMIT order requires limit_price")


@dataclass
class OrderResult:
    broker_order_id: str | None
    status: OrderStatus
    filled_quantity: float = 0.0
    avg_fill_price: float | None = None
    commission: float | None = None
    message: str | None = None
    submitted_at: datetime | None = None


class BrokerError(RuntimeError):
    """Broker-layer failure. Callers treat this as fail-closed."""


class BrokerInterface(ABC):
    """Everything the system is allowed to ask a broker to do.

    Note what is absent: no `get_historical_data`. Historical bars come from
    the parquet cache, because the pacing limit makes on-demand history
    impractical. Only the cache refresh job touches IBKR for history.
    """

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def get_account(self) -> Account:
        """Live account values. Never cached, never stored."""

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Live positions. The broker is the source of truth."""

    @abstractmethod
    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Current prices for order pricing and marking."""

    @abstractmethod
    def submit_order(self, request: OrderRequest, dry_run: bool = True) -> OrderResult:
        """Send an order. `dry_run=True` builds and validates without sending.

        The default is True on purpose. A caller has to opt in to real
        submission, so a forgotten argument cannot place a trade.
        """

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> OrderResult: ...

    @abstractmethod
    def get_order_status(self, broker_order_id: str) -> OrderResult:
        """Poll a submitted order. Never assume a submitted order filled."""

    # ------------------------------------------------------------- helpers

    def get_position(self, symbol: str) -> Position | None:
        for p in self.get_positions():
            if p.symbol.upper() == symbol.upper() and p.is_open:
                return p
        return None

    def held_symbols(self) -> set[str]:
        return {p.symbol.upper() for p in self.get_positions() if p.is_open}

    @staticmethod
    def marketable_limit(
        action: OrderAction, reference_price: float, offset_pct: float
    ) -> float:
        """Limit price that fills like a market order in normal conditions but
        will not chase through a gap."""
        factor = (1 + offset_pct) if action is OrderAction.BUY else (1 - offset_pct)
        return round(reference_price * factor, 2)
