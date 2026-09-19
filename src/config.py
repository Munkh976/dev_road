"""
Typed configuration loader.

Single rule this module exists to enforce: every operational number lives in
config.yaml and nowhere else. If you find yourself writing a literal like
`0.15` or `200` anywhere in src/, it belongs here instead.

Pydantic validates on load, so a typo in config.yaml fails at startup with a
readable message rather than silently producing wrong trades.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"


# ------------------------------------------------------------------ sections


class StrategyMeta(BaseModel):
    name: str
    version: str
    spec: str


class AccountCfg(BaseModel):
    capital: float = Field(gt=0)
    currency: str
    paper_trading: bool
    fractional_shares: bool


class UniverseCfg(BaseModel):
    source: str
    benchmark: str
    min_dollar_volume_20d: float = Field(ge=0)
    min_price: float = Field(ge=0)
    min_days_listed: int = Field(ge=0)
    security_types: list[str]
    max_symbols: int = Field(gt=0)


class DataCfg(BaseModel):
    cache_dir: str
    cache_format: str
    history_years: int = Field(gt=0)
    bar_size: str
    what_to_show: str
    min_bars_required: int = Field(gt=0)
    ibkr_requests_per_minute: float = Field(gt=0, le=6)
    max_stale_days: int = Field(gt=0)

    @field_validator("ibkr_requests_per_minute")
    @classmethod
    def _under_ibkr_cap(cls, v: float) -> float:
        # IBKR: no more than 60 historical requests per 10 minutes.
        # 6/min is the ceiling; going over earns a 10-minute cooldown.
        if v > 6:
            raise ValueError("would exceed IBKR's 60-per-10-minutes pacing limit")
        return v


class MomentumCfg(BaseModel):
    skip_days: int
    lookback_short: int
    lookback_long: int
    weight_short: float
    weight_long: float


class VolatilityCfg(BaseModel):
    window: int
    annualize: int
    floor: float = Field(gt=0)


class TrendFilterCfg(BaseModel):
    symbol: str
    ma_period: int
    ma_type: str


class AtrCfg(BaseModel):
    period: int


class SignalsCfg(BaseModel):
    momentum: MomentumCfg
    volatility: VolatilityCfg
    trend_filter: TrendFilterCfg
    atr: AtrCfg


class EntryCfg(BaseModel):
    require_market_on: bool
    rank_threshold: int = Field(gt=0)
    min_momentum: float
    max_volatility: float
    require_ai_veto_pass: bool
    max_new_per_rebalance: int = Field(ge=0)


class ExitCfg(BaseModel):
    rank_exit: int
    exit_all_on_market_off: bool
    trailing_stop_atr: float = Field(gt=0)
    exit_on_negative_momentum: bool


class SizingCfg(BaseModel):
    method: str
    max_position_weight: float = Field(gt=0, le=1)
    min_position_weight: float = Field(ge=0, le=1)
    target_portfolio_vol: float = Field(gt=0)
    scale_up_allowed: bool
    min_position_value: float = Field(ge=0)
    covariance_window: int


class ScheduleCfg(BaseModel):
    timezone: str
    data_refresh: str
    exit_check: str
    entry_check: str
    execution_time: str
    drift_tolerance: float


class RiskCfg(BaseModel):
    max_gross_exposure: float = Field(gt=0)
    max_positions: int = Field(gt=0)
    max_sector_weight: float = Field(gt=0, le=1)
    max_orders_per_day: int = Field(gt=0)
    max_order_value: float = Field(gt=0)
    halt_on_drawdown: float = Field(gt=0, le=1)
    halt_on_broker_disconnect: bool
    require_manual_approval: bool

    @field_validator("max_gross_exposure")
    @classmethod
    def _no_leverage(cls, v: float) -> float:
        # Spec section 10: non-negotiable. Guarded here so a stray edit to
        # config.yaml cannot quietly introduce leverage.
        if v > 1.0:
            raise ValueError("max_gross_exposure > 1.0 would mean leverage; refused")
        return v


class AiCfg(BaseModel):
    enabled: bool
    role: str
    on_failure: str
    flag_categories: list[str]

    @field_validator("role")
    @classmethod
    def _veto_only(cls, v: str) -> str:
        if v != "veto_only":
            raise ValueError(
                "ai.role must be 'veto_only'. The AI layer may not generate "
                "signals, size positions or place orders (spec section 7)."
            )
        return v


class ExecutionCfg(BaseModel):
    order_type: str
    limit_offset_pct: float = Field(ge=0)
    time_in_force: str
    ibkr_host: str
    ibkr_port: int
    client_id: int

    @field_validator("order_type")
    @classmethod
    def _never_market(cls, v: str) -> str:
        if v.upper() == "MARKET":
            raise ValueError(
                "Bare market orders are refused; a gap or halt fills you at "
                "any price. Use LIMIT with limit_offset_pct."
            )
        return v


class WalkForwardCfg(BaseModel):
    in_sample_years: int
    out_of_sample_years: int
    step_months: int


class CostsCfg(BaseModel):
    commission_per_share: float
    commission_min: float
    slippage_bps: float
    spread_bps: float


class AcceptanceCfg(BaseModel):
    beat_benchmark: bool
    max_drawdown: float
    worst_rolling_12m: float
    min_sharpe: float
    max_annual_turnover: float
    max_single_window_contribution: float


class BacktestCfg(BaseModel):
    start: str
    walk_forward: WalkForwardCfg
    costs: CostsCfg
    parameter_change_budget: int
    acceptance: AcceptanceCfg


class Config(BaseModel):
    strategy: StrategyMeta
    account: AccountCfg
    universe: UniverseCfg
    data: DataCfg
    signals: SignalsCfg
    entry: EntryCfg
    exit: ExitCfg
    sizing: SizingCfg
    schedule: ScheduleCfg
    risk: RiskCfg
    ai: AiCfg
    execution: ExecutionCfg
    backtest: BacktestCfg

    # ---- cross-section invariants ----------------------------------------

    def model_post_init(self, _ctx) -> None:
        if self.exit.rank_exit <= self.entry.rank_threshold:
            raise ValueError(
                f"exit.rank_exit ({self.exit.rank_exit}) must exceed "
                f"entry.rank_threshold ({self.entry.rank_threshold}). Without "
                "the buffer, names oscillating at the boundary churn every "
                "week and commissions eat the strategy (spec section 6)."
            )
        if self.sizing.max_position_weight * self.risk.max_positions < 1.0:
            # Not fatal — it just means the book can never be fully invested.
            pass
        if self.sizing.min_position_weight >= self.sizing.max_position_weight:
            raise ValueError("sizing.min_position_weight must be < max_position_weight")

    # ---- derived paths ---------------------------------------------------

    @property
    def cache_path(self) -> Path:
        return _anchor(os.getenv("WM_CACHE_DIR", self.data.cache_dir))

    @property
    def db_path(self) -> Path:
        default = Path("db") / "operations.db"
        return _anchor(os.getenv("WM_DB_PATH", str(default)))

    @property
    def is_live(self) -> bool:
        return not self.account.paper_trading


# ------------------------------------------------------------------ loading


def _anchor(raw: str) -> Path:
    """Resolve a relative path against the repo root, not the current directory.

    Otherwise running from another directory silently creates a second,
    empty database and cache next to wherever the shell happened to be.
    """
    p = Path(raw)
    return (p if p.is_absolute() else REPO_ROOT / p).resolve()


@lru_cache(maxsize=1)
def load_config(path: str | Path | None = None) -> Config:
    """Load and validate config.yaml. Cached — call freely."""
    load_dotenv(REPO_ROOT / ".env")
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with cfg_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Config(**raw)


def require_secret(name: str) -> str:
    """Fetch a secret from the environment, or fail with a useful message."""
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in. "
            "Secrets never belong in config.yaml or source."
        )
    return val


if __name__ == "__main__":
    c = load_config()
    print(f"{c.strategy.name} v{c.strategy.version}")
    print(f"  capital      ${c.account.capital:,.0f}")
    print(f"  mode         {'PAPER' if c.account.paper_trading else 'LIVE'}")
    print(f"  universe     {c.universe.max_symbols} symbols from {c.universe.source}")
    print(f"  positions    {c.risk.max_positions} max, "
          f"{c.sizing.max_position_weight:.0%} cap each")
    print(f"  entry/exit   rank <= {c.entry.rank_threshold} in, "
          f"> {c.exit.rank_exit} out")
    print(f"  leverage     {c.risk.max_gross_exposure:.2f}x (1.00 = none)")
    print(f"  cache        {c.cache_path}")
    print(f"  db           {c.db_path}")
