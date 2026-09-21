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
    constituents_path: str
    constituents_max_age_days: int = Field(gt=0)
    adv_window_days: int = Field(gt=0)


class DataCfg(BaseModel):
    cache_dir: str
    cache_format: str
    history_years: int = Field(gt=0)
    bar_size: str
    what_to_show: str
    min_bars_required: int = Field(gt=0)
    ibkr_requests_per_minute: float = Field(gt=0, le=6)
    max_stale_days: int = Field(gt=0)
    overlap_days: int = Field(gt=0)
    restatement_tolerance: float = Field(gt=0, lt=1)
    volume_multiplier: float = Field(gt=0)

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


class ReentryCfg(BaseModel):
    max_rank: int = Field(gt=0)
    sma_days: int = Field(gt=0)
    min_weeks_after_exit: int = Field(ge=0)


class LadderCfg(BaseModel):
    drawdowns: list[float]

    @field_validator("drawdowns")
    @classmethod
    def _increasing_fractions(cls, v: list[float]) -> list[float]:
        # Each level sells a share of what is left, and the last sells the rest,
        # so the levels must deepen or a later level could fire before an earlier one.
        if not v or not all(0 < d < 1 for d in v) or v != sorted(set(v)):
            raise ValueError("exit.ladder.drawdowns must be strictly increasing fractions in (0, 1)")
        return v


class ExitCfg(BaseModel):
    rank_exit: int
    exit_on_negative_momentum: bool
    ladder: LadderCfg


class RegimeCfg(BaseModel):
    target_normal: float = Field(gt=0, le=1)
    target_defensive: float = Field(gt=0, le=1)
    confirm_weeks: int = Field(gt=0)
    refill_below: float = Field(gt=0, le=1)
    defensive_trim_band: float = Field(ge=0, lt=1)


class SizingCfg(BaseModel):
    method: str
    max_position_weight: float = Field(gt=0, le=1)
    min_position_weight: float = Field(ge=0, le=1)
    min_position_value: float = Field(ge=0)


class ScheduleCfg(BaseModel):
    timezone: str
    data_refresh: str
    exit_check: str
    entry_check: str
    execution_time: str


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


class RiskDialLevels(BaseModel):
    normal: float = Field(gt=0, le=1)
    caution: float = Field(gt=0, le=1)


class RiskDialCfg(BaseModel):
    levels: RiskDialLevels
    min_days_between_changes: int = Field(gt=0)
    expiry_weeks: int = Field(gt=0)


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
    beat_benchmark_margin: float = Field(ge=0)


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
    reentry: ReentryCfg
    exit: ExitCfg
    regime: RegimeCfg
    sizing: SizingCfg
    schedule: ScheduleCfg
    risk: RiskCfg
    risk_dial: RiskDialCfg
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
        if self.sizing.min_position_weight >= self.sizing.max_position_weight:
            raise ValueError("sizing.min_position_weight must be < max_position_weight")
        reg = self.regime
        if reg.target_normal > self.risk.max_gross_exposure:
            raise ValueError("regime.target_normal exceeds risk.max_gross_exposure (leverage)")
        if not reg.target_defensive < reg.refill_below < reg.target_normal:
            raise ValueError(
                "regime needs target_defensive < refill_below < target_normal, or the "
                "refill and the defensive trim would fight each other")
        levels = self.risk_dial.levels
        if not levels.caution < levels.normal <= reg.target_normal:
            # The dial only ever lowers exposure. Never above the normal target.
            raise ValueError(
                "risk_dial needs caution < normal <= regime.target_normal: the dial "
                "can lower the invested target, never raise it")
        if levels.caution <= reg.target_defensive:
            raise ValueError("risk_dial.caution must be above the defensive target; "
                             "defensive is set only by the market filter")
        if self.reentry.max_rank > self.exit.rank_exit:
            raise ValueError("reentry.max_rank must not exceed exit.rank_exit")

    # ---- derived paths ---------------------------------------------------

    @property
    def cache_path(self) -> Path:
        return _anchor(os.getenv("WM_CACHE_DIR", self.data.cache_dir))

    @property
    def constituents_path(self) -> Path:
        return _anchor(self.universe.constituents_path)

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
    print(f"  invested     {c.regime.target_normal:.0%} normal, "
          f"{c.regime.target_defensive:.0%} defensive")
    print(f"  entry/exit   rank <= {c.entry.rank_threshold} in, "
          f"> {c.exit.rank_exit} out")
    print(f"  leverage     {c.risk.max_gross_exposure:.2f}x (1.00 = none)")
    print(f"  cache        {c.cache_path}")
    print(f"  db           {c.db_path}")
