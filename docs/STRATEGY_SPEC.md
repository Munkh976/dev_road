# Strategy Specification: `weekly_momentum_v1`

**Status:** Draft — not yet backtested
**Capital:** $15,000 (paper first)
**Owner:** [you]
**Created:** 2026-09-19
**Version:** 1.0.0

> This document defines the strategy completely enough to backtest without
> further decisions. If you find yourself making a judgment call while
> implementing, that judgment belongs here, in writing, first.

---

## 1. Edge hypothesis

**What inefficiency is being harvested?**

Cross-sectional momentum. Assets that have outperformed over the past 6–12
months tend to continue outperforming over the following 1–3 months. This is
one of the most widely documented anomalies in finance (Jegadeesh & Titman
1993, and roughly thirty years of replication since), and it persists across
asset classes, countries, and decades.

**Why hasn't it been arbitraged away?**

Two commonly proposed reasons, neither fully settled:

1. **Behavioral** — investors underreact to news, so prices adjust gradually
   rather than instantly.
2. **Risk-based** — momentum suffers rare, violent crashes (see §11). The
   returns may be compensation for bearing that crash risk.

Note the honest implication of (2): if momentum is a risk premium rather than
a free lunch, then the drawdowns are the *price*, not a flaw to be engineered
away.

**Why the trend filter?** Momentum's worst episodes occur during market
recoveries off a bottom, when beaten-down stocks violently outperform prior
winners. Holding cash when the broad market is below its 200-day average
sidesteps most of that, at the cost of missing some early recovery.

**What would falsify this?** If out-of-sample results show no excess return
over SPY after costs, the hypothesis is not supported *for this
implementation* and the project stops at Phase 4. That is an acceptable
outcome and costs nothing but time.

---

## 2. Universe

```
START:   S&P 500 constituents (point-in-time if available; see §12)
FILTER:  avg_dollar_volume_20d  >= $20,000,000
         price                   >= $10.00
         days_listed             >= 400
         security_type           == common stock (no ADRs, no REITs, no trusts)
EXCLUDE: tickers with a pending merger/acquisition (AI veto layer, §7)
CAP:     top 150 by 20-day average dollar volume
```

**Why cap at 150?** IBKR's historical-data pacing limit (60 requests per
10 minutes ≈ 6 symbols/minute) makes a 500-name refresh take ~83 minutes.
150 names refresh in ~25 minutes. At $15,000 holding 6 positions, screening
500 candidates instead of 150 adds no meaningful selection benefit.

**Benchmark ticker:** `SPY` (also used for the trend filter; always fetched
regardless of universe membership).

---

## 3. Data

| Field | Source | Frequency |
|---|---|---|
| Daily OHLCV, split/dividend adjusted | IBKR `ADJUSTED_LAST`, cached to parquet | Weekly incremental |
| Current quote | IBKR live | Each run |
| Account equity, buying power | IBKR live | Each run |
| Positions | IBKR live | Each run |
| News (AI veto layer only) | Any provider | Each run, held names + candidates |

**History required:** 15 years minimum (for backtest), 300 trading days
minimum per symbol (for signal computation).

**Staleness gate:** if the most recent bar for SPY is more than 5 calendar
days old, **halt — generate no orders.**

---

## 4. Signal computation

All computed on adjusted closes, as of the last completed trading day.

```python
# Momentum, skipping the most recent 21 days.
# The skip avoids short-term reversal, which is a documented and
# opposite-signed effect at the 1-month horizon.
mom_6m  = P[t-21] / P[t-147] - 1     # ~6 months, skip 1
mom_12m = P[t-21] / P[t-273] - 1     # ~12 months, skip 1

blended_momentum = 0.5 * mom_6m + 0.5 * mom_12m

# Realized volatility, annualized
vol_63 = stdev(daily_returns[-63:]) * sqrt(252)

# Risk-adjusted score. The floor prevents low-vol names from
# receiving an unbounded score.
score = blended_momentum / max(vol_63, 0.10)

# Absolute trend filter on the broad market
market_on = SPY[t] > SMA(SPY, 200)[t]

# Ranking: 1 = highest score
rank = descending_rank(score across universe)
```

**ATR for stops:**
```python
ATR_20 = SMA(true_range, 20)
```

---

## 5. Entry rules

A position is opened only if **all** conditions hold:

| # | Condition |
|---|---|
| E1 | `market_on == True` |
| E2 | `rank <= 6` |
| E3 | `blended_momentum > 0` (never buy a falling asset, however it ranks) |
| E4 | `vol_63 <= 0.80` (excludes extreme volatility) |
| E5 | AI veto layer returns `flag == False` (§7) |
| E6 | Position count after entry `<= 6` |
| E7 | Proposed position value `>= $1,000` |
| E8 | Sector weight after entry `<= 40%` |
| E9 | New positions this rebalance `<= 2` |

**E9 matters more than it looks.** Capping entries at 2 per rebalance means
the portfolio builds gradually rather than deploying all $15,000 into whatever
six names happened to rank top on one arbitrary date. It spreads entry timing
without requiring you to time anything.

---

## 6. Exit rules

Checked **weekly**. Any single condition triggers a full exit of that position.

| # | Condition | Rationale |
|---|---|---|
| X1 | `rank > 10` | Buffer from the entry threshold of 6 prevents churn on names oscillating at the boundary |
| X2 | `market_on == False` | Exit **all** positions to cash |
| X3 | `price < (position_high - 3.0 × ATR_20)` | Trailing disaster stop |
| X4 | `blended_momentum < 0` | Thesis has inverted |

**On the stop width.** 3× ATR is deliberately wide — roughly a 15–25% move
for a typical name in this universe. Tight stops systematically *reduce*
momentum returns, because momentum stocks are volatile by construction and a
10% stop repeatedly ejects you from positions that then continue higher. X3
is disaster insurance. Your real exits are X1 and X2.

**`position_high`** = highest closing price since entry (not intraday high).

---

## 7. AI veto layer

**Authority: veto only. Cannot generate signals, select names, size
positions, or place orders.**

Runs against the 6 proposed holdings each rebalance.

```
Flag ONLY if evidence of:
  - pending acquisition or merger
  - accounting investigation or restatement
  - delisting notice
  - bankruptcy filing or going-concern doubt
  - fraud allegations by a regulator
  - trading halt

Do NOT flag: earnings misses, analyst downgrades, ordinary bad news,
             valuation opinions, price predictions.
```

Output schema (validated; malformed response = treated as unavailable):
```json
{"ticker": "str", "flag": bool, "severity": "low|medium|high", "reasoning": "str", "sources": []}
```

**Failure behavior:** if the AI layer errors or returns invalid output,
the system proceeds **without** the veto and marks the run
`ai_veto: UNAVAILABLE` in the audit log. An AI outage must not halt trading,
and must not silently approve either — it is logged and visible in the report.

**Why this narrow?** A momentum strategy's one specific, well-known failure
is loading into a stock that is rising because of a fixed-price buyout, which
then goes nowhere. That is a real, bounded use for a language model. Asking
it whether a stock is a good buy is not.

---

## 8. Position sizing

```python
# 1. Inverse-volatility weights across selected names
inv_vol = 1 / vol_63
w = inv_vol / inv_vol.sum()

# 2. Clip to bounds, renormalize
w = clip(w, lower=0.08, upper=0.20)
w = w / w.sum()

# 3. Portfolio volatility target — scale DOWN only, never up
port_vol = sqrt(w.T @ Σ @ w) * sqrt(252)    # Σ = 63-day covariance
scalar   = min(1.0, 0.15 / port_vol)
w_final  = w * scalar

# 4. Convert to shares
target_value  = w_final * account_equity
shares        = floor(target_value / current_price)   # fractional OK at IBKR
```

**Remainder stays in cash.** `scalar < 1.0` means the portfolio is
deliberately under-invested because its constituents are volatile. That is
the mechanism working, not a bug.

At $15,000 with 6 positions, equal weight is 16.7% (~$2,500 each). The 20%
cap allows modest tilt toward lower-volatility names; the 8% floor prevents
positions too small to matter after commissions.

---

## 9. Rebalance schedule

| Action | Frequency | When |
|---|---|---|
| Data refresh + signal computation | Weekly | Saturday 18:00 ET |
| **Exit checks (X1–X4)** | **Weekly** | Acted on Monday open |
| **New entries (E1–E9)** | **Monthly** | First Monday of the month |
| Drift rebalance | Monthly | Only if weight is ±25% off target |

**The asymmetry is intentional.** Risk control (exits) responds weekly.
Position-taking (entries) is monthly. This keeps you protected without
generating the turnover that would come from chasing rank changes.

---

## 10. Risk limits and kill switches

```yaml
max_gross_exposure:        1.00    # NO LEVERAGE. Non-negotiable.
max_position_weight:       0.20
min_position_weight:       0.08
max_sector_weight:         0.40
max_positions:             6
min_position_value:        1000
max_new_per_rebalance:     2
max_orders_per_day:        8
max_order_value:           4000

halt_on_drawdown:          0.20    # requires manual restart
halt_on_data_stale_days:   5
halt_on_broker_disconnect: true
require_manual_approval:   true    # keep TRUE for the first 12 months live
```

**Fail closed.** Any check that cannot be evaluated counts as a failure.

---

## 11. Known weaknesses

Written down now so they are not discovered as surprises later.

1. **Momentum crashes.** The strategy's worst historical episodes are sharp
   reversals off market bottoms (March–May 2009, April 2020), where prior
   losers violently outperform prior winners. The 200-day filter reduces but
   does not eliminate this. Expect the ugliest drawdowns here.
2. **200-DMA whipsaw.** In choppy, directionless markets the trend filter can
   flip on and off repeatedly, generating full-portfolio exits and re-entries
   with their associated costs. 2015 and 2018 are instructive.
3. **Six positions is concentrated.** A single blow-up is ~16% of capital.
   This is the cost of trading a $15,000 account; it does not go away.
4. **Survivorship bias.** IBKR data has no point-in-time index membership and
   often lacks bars for delisted names, so the backtest will be optimistic by
   a meaningful and unmeasured amount.
5. **Tax drag.** Monthly entries and weekly exits generate short-term capital
   gains in a taxable account. Post-tax returns will be materially below the
   backtest.
6. **Crowding.** Momentum is widely known and widely traded. Published
   anomalies tend to decay after publication.

---

## 12. Backtest protocol

**Non-negotiable rules:**

```
PERIOD:        2007-01-01 → present   (must include 2008, 2020, 2022)
METHOD:        Walk-forward
               in-sample:     3 years
               out-of-sample: 1 year
               step:          12 months
REPORT:        Out-of-sample results ONLY
COSTS:         commission  $0.005/share, $1.00 minimum
               slippage    5 bps
               spread      3 bps
BENCHMARK:     SPY buy-and-hold, same costs
```

**Parameter budget: 5 changes, total, logged.**

Every time a parameter is adjusted after seeing results, some out-of-sample
validity is spent. Keep a log:

| # | Date | Parameter | From | To | Why |
|---|---|---|---|---|---|
| 1 | | | | | |

When the budget is exhausted, the spec is frozen. Further tuning means the
results are no longer trustworthy.

**Forbidden:**
- Testing a parameter, seeing a bad result, and reverting without logging it
- Changing the start date to exclude an unfavorable period
- Adding a filter that happens to remove the worst trades

---

## 13. Acceptance criteria — go / no-go to paper trading

All must pass on **out-of-sample** results:

| # | Criterion | Threshold |
|---|---|---|
| A1 | CAGR vs. SPY, after costs | Exceeds SPY |
| A2 | Max drawdown | < 35% |
| A3 | Worst rolling 12-month return | > −30% |
| A4 | Sharpe ratio | > 0.6 |
| A5 | Annual turnover | < 400% |
| A6 | Consistency | No single walk-forward window contributes > 50% of total excess return |

**A6 is the one most people skip.** A strategy whose entire edge came from
one lucky year is not a strategy.

**If A1 fails: stop.** Buy SPY, keep the hour. That is a legitimate and
honorable outcome, and it costs you nothing but the build time.

---

## 14. Live kill criteria

Once live, halt and review if any of these occur:

| Trigger | Action |
|---|---|
| Drawdown > 20% | Automatic halt, manual restart required |
| Underperforms SPY by > 15% over 12 months | Manual review, consider retirement |
| 3 consecutive weeks of manual overrides | **Stop.** The system is not being followed, which makes it worse than no system |
| Live results diverge from paper by > 5% annualized | Investigate execution assumptions |

The third one is the one that will actually happen. Watch for it.

---

## 15. Changelog

| Version | Date | Change |
|---|---|---|
| 1.0.0 | 2026-09-19 | Initial specification, pre-backtest |
