"""
Risk engine — deterministic, independent of the AI layer, and the last thing
between a proposal and the broker.

Two properties that must survive any refactor:

  FAIL CLOSED   A check that cannot be evaluated counts as a failure. If the
                broker is unreachable, we do not "assume it's fine".
  NO OVERRIDE   The AI layer cannot relax a check, and neither can the
                approval UI. A REJECT is final for that proposal.

STATUS: stub. Signatures and contracts are fixed; bodies are next.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.config import Config
from src.execution.broker import Account, OrderRequest, Position


@dataclass
class RiskResult:
    passed: bool
    failures: list[str] = field(default_factory=list)   # human-readable reasons
    checks_run: list[str] = field(default_factory=list)

    def fail(self, reason: str) -> None:
        self.passed = False
        self.failures.append(reason)


# Each check takes (request, account, positions, cfg, state) and mutates a
# RiskResult. Listed here so the set is visible in one place and a new check
# cannot be added without appearing in the audit trail.
CHECKS = (
    "system_enabled",        # kill switch is off
    "data_gate_passed",      # the run's data quality check passed
    "broker_connected",      # we have a live connection
    "no_leverage",           # gross exposure stays <= 1.00
    "cash_sufficient",       # buy has settled cash behind it
    "position_weight",       # <= max_position_weight after fill
    "position_count",        # <= max_positions
    "sector_weight",         # <= max_sector_weight
    "order_value",           # <= max_order_value
    "min_position_value",    # >= min_position_value, else skip
    "daily_order_count",     # <= max_orders_per_day
    "price_deviation",       # reference price still close to the live quote
    "duplicate_order",       # no open order already on this symbol
    "drawdown_halt",         # portfolio drawdown < halt_on_drawdown
)


def validate(
    request: OrderRequest,
    account: Account,
    positions: list[Position],
    cfg: Config,
    state: dict,
) -> RiskResult:
    """Run every check in CHECKS. Returns PASS only if all of them pass.

    `state` carries what the broker cannot: orders already sent today, the
    data-quality verdict for this run, current drawdown, kill-switch status.
    """
    raise NotImplementedError


def check_drawdown_halt(
    account: Account, peak_net_liq: float, cfg: Config
) -> tuple[bool, float]:
    """Returns (should_halt, drawdown). A halt requires a MANUAL restart —
    it does not clear itself when the market recovers, because the point is
    to force you to look at what happened."""
    raise NotImplementedError
