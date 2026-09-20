# Strategy Specification: `weekly_momentum_v1`

**Status:** Draft — not yet backtested
**Capital:** $15,000 (paper first)
**Owner:** [you]
**Created:** 2026-09-19
**Version:** 1.0.5

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
         days_listed             >= 400   (calendar days, not bars; see §2.2)
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

### 2.1 Where the constituent list comes from

IBKR's API does not expose index membership, and the list is **never fetched
at runtime**. It is a committed file, `data/universe/sp500_constituents.csv`,
with columns `symbol, name, as_of_date`.

- `scripts/update_constituents.py` is a manual, run-quarterly helper. It
  builds the CSV from a source the owner reviews (via `git diff`) before
  committing. Nothing in the refresh path calls it.
- The CSV stores symbols as index lists write them (`BRK.B`). The loader
  converts to IBKR form on read: `.` becomes a space (`BRK B`, `BF B`).
  IBKR symbols are the only form used downstream (cache filenames, signals,
  the snapshot table).
- `refresh` **warns** when `as_of_date` is older than
  `universe.constituents_max_age_days` (100). It warns; it does not halt. A
  slightly stale membership list degrades selection quality, it does not make
  orders unsafe.

### 2.2 Two cadences: rebuilding the universe vs. refreshing it

The dollar-volume filter needs price history, but the universe decides what
gets fetched. Resolved by running at two speeds:

| Cadence | What | Requests | Time at 6/min |
|---|---|---|---|
| **Quarterly** rebuild (`refresh --rebuild-universe`) | Refresh history for **all** ~500 constituents + SPY, request contract details for each, apply the filters above **from the cache**, save the chosen 150 with the date | ~500 | ~83 min |
| **Weekly** refresh (`refresh`) | Refresh only the **saved** 150 + SPY | ~150 | ~25 min |

Filter definitions, so they are not judgment calls at implementation time:

- `avg_dollar_volume_20d` = mean of `close x volume x volume_multiplier`
  over the last 20 cached bars (`universe.adv_window_days`). Fewer than 20
  bars fails the filter.
- `price` = last cached close.
- `days_listed` = **calendar days** (not trading bars) from the first cached
  bar to the last cached bar. IBKR contract details carry no listing date; the
  first bar is the proxy. History is capped at `data.history_years`, so
  anything older reads as that many years, which is fine for a `>= 400` test.
  400 calendar days is ~275 trading bars, slightly under the 300 bars §3
  requires for signals. That is harmless: a symbol with too little history gets
  NaN signals and is left out of the ranking (§4), so this filter only spares
  the ranking a name that could not be scored anyway. The first rebuild dropped
  three names on it, all recent spin-offs (FDXF, HONA, Q).
- `security_type` = IBKR `ContractDetails.stockType`. Keep `COMMON`; REIT,
  ETF and ADR (and anything else) are dropped. A symbol with no contract
  details is dropped (fail closed).
- A symbol whose last bar is older than `data.max_stale_days` relative to
  SPY's last bar is dropped: it is delisted, halted or failed to fetch, and
  its dollar volume is not current.
- Ranking is by `avg_dollar_volume_20d` descending, ties broken by symbol, so
  a rebuild on identical inputs gives an identical list.
- **Sector** for entry rule E8 is `ContractDetails.industry`, stored alongside
  `category` and `subcategory`. It is captured at rebuild time and carried in
  the snapshot.
- Contract-detail requests are not historical-data requests and do not count
  against the 60-per-10-minutes pacing cap.

**Snapshot.** Each rebuild writes the chosen list to the SQLite table
`universe_snapshots` (one row per symbol, keyed by `snapshot_date`, with rank,
sector, dollar volume, price, and the `as_of_date` of the constituent list
used). The backtest and the audit trail answer "which list was in force on
date D" with the latest `snapshot_date <= D`. The weekly refresh reads the
latest snapshot; it never recomputes the filters. If no snapshot exists the
weekly refresh fails with an instruction to run a rebuild.

**Volume units.** IBKR daily-bar volume for US stocks has historically been
reported in round lots in some configurations. `data.volume_multiplier`
(default 1, meaning shares) exists so a 100x unit error is a one-line config
correction rather than a code change. A rebuild that selects zero symbols
fails loudly; check SPY's computed dollar volume after the first rebuild.

### 2.3 The backtest universe is chosen point-in-time

The saved snapshot (§2.2) is today's top 150 by dollar volume. Backtesting on
it builds in hindsight: a stock is among the most traded today largely because
it went up, so a 2009 backtest on that list is handed the future winners. The
backtest therefore does **not** use the snapshot.

**Rule.** For each date `D`, the backtest universe is the top 150 by trailing
20-day average dollar volume among **current** constituents, computed only from
bars on or before `D`. The §2 filters are applied as of `D` with the same
definitions as §2.2: `avg_dollar_volume_20d >= $20M`, `price >= $10` (close on
`D`), `days_listed >= 400` (calendar days from the first cached bar to `D`),
and a bar on `D` (no bar, no price, not tradable). Ranking is by dollar volume
descending, ties broken by symbol. The result is recomputed every trading day;
live trading recomputes it quarterly (§2.2), a difference that only makes the
backtest slightly more responsive than live.

Needs full history for every constituent, not only today's 150:
`refresh --backfill --all-constituents` (§3.2).

**What this fixes.** Selection into the universe no longer uses information
from after `D`.

**What remains, and biases the backtest optimistic.** Recorded so they are not
rediscovered later:

1. **Survivorship in the membership list itself.** The list is today's S&P 500.
   Companies that were in the index in 2009 and have since been removed
   (acquired, bankrupt, shrunk, delisted) are absent, and companies that joined
   the index *because* they grew are present before they qualified. Point-in-time
   index membership is a paid data product; without it this bias is unmeasured.
   Read early-window results with it in mind (§3.2, §11).
2. **Sectors are today's labels** (`ContractDetails.industry` at rebuild time).
   E8's 40% sector cap uses the current classification for every date. A company
   that changed sector, or an industry reclassified since, is mislabelled in
   the past. The error is small for large caps and its direction is not known.
3. **Security type is today's label**, for the same reason. It is static per
   security in practice, so this is the least worrying of the three.
4. **Liquidity filters run on adjusted prices.** The cache holds
   `ADJUSTED_LAST`, which is scaled back through every dividend and split. In
   old years the `$10` price floor and dollar volume (adjusted close x volume)
   therefore read lower than what actually traded. The ranking is barely
   affected, since every name is scaled by its own factor, but a stock whose
   real price was just over $10 in 2006 can be filtered out.

**IBKR volume undercount does not affect selection.** IBKR daily volume can
undercount consolidated volume. Selection is a *ranking* by dollar volume, so a
uniform undercount leaves the order unchanged, and the `$20M` floor is not the
binding constraint: at the 2026-09-20 rebuild the 150th name (COF) traded
$290.8M a day, about 14x the floor. The cap binds, not the floor. No
`volume_multiplier` change.

---

## 3. Data

| Field | Source | Frequency |
|---|---|---|
| Daily OHLCV, split/dividend adjusted | IBKR `ADJUSTED_LAST`, cached to parquet | Weekly incremental |
| Current quote | IBKR live | Each run |
| Account equity, buying power | IBKR live | Each run |
| Positions | IBKR live | Each run |
| News (AI veto layer only) | Any provider | Each run, held names + candidates |

**History required:** `data.history_years` = **22** (data from ~2004), and 300
trading days minimum per symbol (for signal computation). See §3.2 for why 22.

**Staleness gate:** if the most recent bar for SPY is more than 5 calendar
days old, **halt — generate no orders.**

### 3.1 Adjusted-price restatement

`ADJUSTED_LAST` history is recalculated all the way back whenever a stock
pays a dividend or splits. Appending new adjusted bars to old adjusted history
leaves a jump at the join every time, and a split appears as a fake crash,
which corrupts momentum, volatility and ATR.

Rule: every incremental fetch overlaps the cache by `data.overlap_days` (5).
Compare the overlapping closes to the cache. If **any** differs by more than
`data.restatement_tolerance` (0.1%), discard the cached history for that
symbol and replace it with a full-history fetch. If there is **no** overlap at
all, the history cannot be verified, so it is refetched too (fail closed).

- Pacing counts requests, not bars: a full refetch costs one extra request
  for that symbol, however much history it returns.
- Every refetch is logged with its reason; the count for the run appears in
  the refresh summary and is stored in `data_quality.detail`. A week with many
  refetches is normal around ex-dividend dates; a week where every symbol
  refetches means something is wrong with the fetch, not the data.
- A cache older than one year cannot be topped up with a day-denominated
  request (IBKR rejects `N D` beyond a year), so it is refetched in full.
- `ADJUSTED_LAST` requires an **empty** `endDateTime`; a dated end is
  rejected. Every historical request therefore ends "now" and is sized by
  duration alone.
- Never forward-fill or patch a gap between old and new data. Restated
  history replaces; it is not stitched.

**Refresh order and errors.** SPY is fetched first, so the gate can be
evaluated even if a run is interrupted. Each request acquires the pacing
limiter. IBKR error 420 (and error 162 whose text is a pacing violation)
puts the limiter into its ten-minute cooldown and the symbol is **not**
retried in that run. A symbol that fails to fetch keeps its old cache and is
counted stale by the gate.

### 3.2 Why 22 years of history

§12 requires 2008 among the tested years, but 15 years of history starts in
2011-09. Signals need ~13 months of warm-up (`lookback_long` 273 bars plus the
skip), and the walk-forward needs a 3-year in-sample window before the first
out-of-sample year, so the first tested year would begin around 2016: the 2008
crash and the 2009 momentum crash (§11 weakness 1) would never be tested.

With 22 years the data starts 2004-09 (confirmed: IBKR served SPY from
2004-09-27), warm-up ends ~2005-10, and the first out-of-sample year begins
~2008-10. That covers the Q4-2008 crash and the March-May 2009 rebound. It does
**not** cover January-September 2008; that would need history from ~2002.

**Trade-off.** Survivorship bias (§11 weakness 4) grows the further back the
universe reaches: today's constituents are, by construction, the ones that
survived and grew. Early years will be flattered. The SPY trend filter, which is
what matters most in 2008, has no survivorship bias: SPY is one continuous
series. Read early-window results with that in mind; the stock-selection part is
optimistic, the regime part is not.

**Backfill.** Incremental fetches only extend forwards, so raising
`history_years` does not add older bars to an existing cache. `refresh
--backfill` does a full refetch at the configured `history_years` for the saved
universe plus SPY (~150 requests, ~25 min), replacing each cache file (never
stitching, §3.1). `refresh --symbols SPY --backfill` does it for SPY alone as a
smoke test.

`refresh --backfill --all-constituents` does the same for **every** symbol in
the constituents CSV plus SPY (~500 requests, ~84 min cold) so the backtest can
choose its universe point-in-time (§2.3). It is **resumable**: a symbol already
backfilled to the target start is skipped. "Backfilled" is recorded as
`history_years` in the cache manifest by every full-history write, because a
recent listing never has bars back to the target start and the bars alone could
not distinguish it from a shallow cache. A cache written before the marker
existed counts as backfilled if its first bar reaches the target start. SPY is
always refetched, so the data gate sees fresh bars.

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

**Implementation of E1-E9** (`src/strategy/rules.py`, shared by backtest and
live, pure functions):

- Candidates are the ranked, not-yet-held symbols in rank order. Slots are
  handed out greedily: only a candidate that passes every rule consumes a
  position slot (E6), a new-entry slot (E9) and sector room (E8), so a name
  blocked by E8 does not starve the next one. Every failed rule is listed.
- **E5.** `flag == True` blocks. An unavailable AI layer proceeds without the
  veto (§7). In the **backtest E5 always passes**: a veto based on news as of a
  past date cannot be reproduced, and the backtest report says so.
- **E7** needs the sized dollar value, so it is enforced after sizing: a new
  position under $1,000 is dropped, the book resized, smallest first, until every
  new position clears the minimum (inclusive). Existing positions are never
  dropped by E7.
- **E8** is checked with the candidate at `max_position_weight` (the most
  sizing can give it), so an accepted entry cannot breach the cap once sized. A
  candidate whose sector is unknown blocks (fail closed).

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

**Implementation of X1-X4.** When several fire on one position the reported
rule is, in order, X2, X3, X4, X1 (regime, disaster stop, thesis, rank buffer);
all that fired are listed in the detail. Fail closed: if X1, X3 or X4 cannot be
evaluated (no rank today because the name left the universe or has no bar; no
ATR; no position high) the position exits under that rule.

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

**Clip-and-renormalize, as implemented.** Read literally, step 2 breaks its own
cap: with fewer than five names, clipping to 20% and renormalizing to 100% gives
two names 50% each, above `max_position_weight` (§10). Since E9 builds the book
two names a month, that is the normal state for the first months. The
implementation instead pins a name that hits a bound and shares what remains
among the others, so the 8% and 20% bounds always hold and, when every name is
capped, the rest is cash. The two agree whenever nothing is clipped. Gross
exposure is also held to `max_gross_exposure` regardless of `scale_up_allowed`.
The covariance window must be complete; a gap raises rather than being filled.

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
   The universe is built from **today's** S&P 500 membership (§2.1), so every
   company that fell out of the index since 2007 is absent from the backtest.
   Since 1.0.3 the backtest chooses the top 150 point-in-time from those
   constituents (§2.3), which removes the hindsight in *which of them* are
   liquid, but not the bias in the membership list itself, nor sectors taken
   from today's labels.
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

### 12.1 How the engine implements this protocol

`src/backtest/walkforward.py`. Every reading below is a judgment call the
protocol above leaves open, made once, here, before any real result exists.

**Timing.** Signals are computed at the close of the last trading day of each
week. Orders decided then fill at the **next bar's open**; nothing fills on the
bar whose close produced the signal. Exits (X1-X4) are decided every week.
Entries (E1-E9) and the drift rebalance (§9, +/-25% off target) are decided only
when the next fill is the first weekly fill of a calendar month (the first
Monday, or the first trading day of the first trading week). A name stopped out
in a week is not a candidate for entry in the same week. A fill with no bar to
trade on (a halted name) does not happen and is retried the following week; a
bar is never invented for it.

**Continuous simulation, out-of-sample scoring.** Nothing is fitted, so
in-sample years cannot be tuned on and every parameter is identical in every
window. The strategy is simulated in one continuous run from the first date
signals exist; in-sample years exist only so the first scored window starts from
a realistic book. Only the out-of-sample windows are reported. Starting each
window flat would charge every year a ramp-up (E9 builds the book two names a
month, cash earns 0%) that live trading pays once.

**Windows.** The first out-of-sample window begins `in_sample_years` (3) after
the first date signals exist, then one window per `step_months` (12), which must
equal `out_of_sample_years` x 12 (checked, else windows overlap or leave gaps).
Windows are anchored at the start of the data and never trimmed to make the last
one whole: trimming the front would drop 2008 (§3.2). A last window shorter than
a year is kept and flagged partial. `backtest.start` is the earliest date an
out-of-sample window may begin, a check on the protocol, not a way to move the
period (see "Forbidden"). With data from 2004-09 the first window begins about
2008-10.

**Costs.** Per side: commission `max(shares x $0.005, $1.00)`, plus slippage
(5 bps) plus **half** of the 3 bps spread, of the traded value. A round trip
crosses the spread once. The benchmark, SPY bought with the same capital at the
first out-of-sample fill and held, pays the same costs. A buy never spends more
cash than is held (no leverage): shares are cut back to what cash plus costs
allows. Cash earns 0%, which slightly understates the strategy in risk-off
periods. The report says so.

**Statistics.** CAGR from calendar days / 365.25. Sharpe is the mean over the
standard deviation of daily returns x sqrt(252), risk-free rate 0 (cash earns 0%
on both sides). Turnover is half the traded value (buys + sells) over average
equity, annualized: replacing the whole book once is 100%. Worst rolling 12
months is over 252 trading days on the chained out-of-sample curve. A6 is the
best window's excess return over the sum of all windows' excess returns; when
that sum is not positive there is no edge to apportion and A6 **fails**. Any
criterion that cannot be computed fails.

**Point-in-time universe daily, live quarterly.** A held name that drops out of
the daily point-in-time top 150 has no rank that day and exits under X1
(unevaluable -> exit, §6). Live, a held name stays in the quarterly snapshot for
the quarter. The backtest can therefore churn somewhat more than live on names
near the liquidity boundary. It errs pessimistic.

**Left out, and printed in every report:** E5 always passes; the risk engine's
order-level checks (order size, order count, drawdown halt) are not applied;
sectors, security type and index membership are today's (§2.3).

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
| 1.0.1 | 2026-09-19 | Pre-backtest; spends no parameter budget. Settles three implementation gaps: (1) S&P 500 list is a committed CSV with a manual quarterly update script, warn-only staleness check, `BRK.B` -> `BRK B` mapping (§2.1); (2) universe is rebuilt quarterly from all constituents and saved as a queryable snapshot, weekly refresh fetches only the saved 150 + SPY, sector comes from IBKR contract details, filter definitions made explicit (§2.2); (3) adjusted-price restatement is detected by overlap comparison at 0.1% and answered with a full refetch, with `endDateTime` empty (§3.1). Adds config keys only: `universe.constituents_path`, `constituents_max_age_days`, `adv_window_days`, `data.overlap_days`, `restatement_tolerance`, `volume_multiplier`. No strategy parameter changed. |
| 1.0.2 | 2026-09-20 | Pre-backtest; spends no parameter budget. (1) `days_listed` counts calendar days, not bars (§2.2). (2) `data.history_years` 15 -> 22 so the backtest can reach the 2008-09 crash, with the survivorship-bias trade-off recorded, and `refresh --backfill` added to refetch existing caches at the new depth (§3.2). This is a data-coverage change, not a strategy parameter. |
| 1.0.3 | 2026-09-20 | Pre-backtest; spends no parameter budget. (1) The backtest universe is chosen point-in-time: for each date, top 150 by trailing 20-day dollar volume among current constituents using only data available then (§2.3), instead of today's saved snapshot. (2) Records what remains biased: survivorship in the membership list, sectors and security type from today's labels. (3) `refresh --backfill --all-constituents` added, resumable (§3.2). (4) Notes the IBKR volume undercount does not affect selection (150th name ~14x the floor); no config change. |
| 1.0.4 | 2026-09-20 | Pre-backtest; spends no parameter budget. Rules and sizing implemented, which forced these readings into the spec: E5 always passes in backtests; E7 enforced after sizing, smallest new name first; E8 checked at `max_position_weight`, unknown sector blocks; greedy slot allocation in rank order; exit-rule reporting priority X2>X3>X4>X1; unevaluable exit checks fail closed. **§8 correction:** "clip then renormalize" breaks the 20% cap with fewer than five names (two names -> 50% each); replaced by pin-at-bound-and-redistribute so the bounds always hold (§8). |
| 1.0.5 | 2026-09-20 | Pre-backtest; spends no parameter budget. Backtest engine implemented; the protocol's open choices are fixed in §12.1: signals at the weekly close and fills at the next open, monthly entries and weekly exits, continuous simulation with out-of-sample-only scoring, windows anchored to the data start with a flagged partial last window, half the spread charged per side, turnover and Sharpe definitions, A6 and uncomputable criteria fail closed, no same-week rebuy of a stopped-out name, and the daily-vs-quarterly universe asymmetry (pessimistic for the backtest). |
