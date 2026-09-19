"""
IBKR implementation of BrokerInterface, over ib_async.

ib_async is the maintained fork of ib_insync, which was archived in 2024.

Connection: IB Gateway on 127.0.0.1, port 4002 (paper) or 4001 (live). The
port lives in config.yaml, and `connect()` asserts the account id matches
IBKR_EXPECTED_ACCOUNT_ID from .env — a guard against pointing a paper config
at a live account.

Historical data is deliberately NOT exposed here. Bars come from the parquet
cache; only src/data/refresh.py talks to IBKR for history, so pacing is
managed in exactly one place.

STATUS: stub. Interface is fixed; the ib_async calls are next.
"""

from __future__ import annotations

import logging

from src.config import Config
from src.execution.broker import (
    Account,
    BrokerInterface,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
)

log = logging.getLogger(__name__)


class IBKRBroker(BrokerInterface):
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._ib = None

    def connect(self) -> None:
        """Connect and verify we are pointed where we think we are.

        If cfg.account.paper_trading is True and the connected account is not
        the expected paper account, raise. Do not proceed on a mismatch.
        """
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    @property
    def is_connected(self) -> bool:
        raise NotImplementedError

    def get_account(self) -> Account:
        raise NotImplementedError

    def get_positions(self) -> list[Position]:
        raise NotImplementedError

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        raise NotImplementedError

    def submit_order(self, request: OrderRequest, dry_run: bool = True) -> OrderResult:
        """dry_run defaults to True so a forgotten argument cannot trade."""
        raise NotImplementedError

    def cancel_order(self, broker_order_id: str) -> OrderResult:
        raise NotImplementedError

    def get_order_status(self, broker_order_id: str) -> OrderResult:
        raise NotImplementedError
