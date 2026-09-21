# Strategy Specification: `weekly_momentum_v2`

**Status:** v2.0.0. Written after v1 failed acceptance; **not yet run on real data.**
**Capital:** $15,000 (paper first)
**Owner:** [you]
**Created:** 2026-09-19 (v1) / 2026-09-21 (v2)
**Version:** 2.0.0

> This document defines the strategy completely enough to backtest without
> further decisions. If you find yourself making a judgment call while
> implementing, that judgment belongs here, in writing, first.

## 0. Why there is a v2, and what it is allowed to be

`weekly_momentum_v1` (git tag `backtest-v1`, spec 1.0.5) was backtested once and
**failed acceptance**: CAGR 4.2% against SPY's 14.3%, an average invested fraction
of 35%, and the 3 x ATR trailing stop was the most common exit (177 of 391 sells)
because it fired at roughly 6-12% below the peak, not the 15-25% §6 claimed. **v1's
verdict is final.** It stays reproducible from its tag (`git checkout backtest-v1`);
nothing here changes it, and it is not rerun with different parameters.

v2 changes the *structure* that produced those numbers, not a knob: the book was
mostly cash (E9 and the volatility target), and the stop was too tight to mean what
the spec said. Those are four changes, all made **after seeing v1's results**, so
all four are logged in `parameter_changes` (phase `backtest`) and spend the budget
(§12). The acceptance test is made harder in the same edit (§13, A1).

**If v2 fails acceptance, development as a trading strategy stops.** There is no v3.
Buy SPY and keep the hour. Whatever the result, **v2 runs on paper only**: a pass
earns paper trading, never live capital directly (§14, build order in CLAUDE.md).

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
winners. Holding less when the broad market is below its 200-day average
sidesteps most of that, at the cost of missing some early recovery. v2 grades the
response (§4.1) instead of going to cash outright.

**What would falsify this?** If out-of-sample results show no excess return
over SPY after costs, the hypothesis is not supported *for this
implementation*. For v1 that stopped the project at Phase 4 once already; for v2
it stops development entirely (§0). That is an acceptable outcome and costs
nothing but time.

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
150 names refresh in ~25 minutes. At $15,000 holding 10 positions, screening
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
descending, ties broken by symbol. Membership is **rebuilt on the first trading
day of each calendar quarter, from data through that day's close, and held until
the next rebuild**, matching live (§2.2). One extra rebuild happens the first day
the list is non-empty, because the data starts mid-quarter. (1.0.3 through the
first 1.0.5 draft recomputed daily; that let a held name slip out of the list
between rebuilds, lose its rank and exit under X1, which live never does.)

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

# Absolute trend filter on the broad market. One reading; the graded
# response to it (two consecutive weekly readings) is section 4.1.
market_on = SPY[t] > SMA(SPY, 200)[t]

# Re-entry gate (section 6.2): a name sold in full must close above this
sma_reentry = SMA(close, 50)[t]

# Ranking: 1 = highest score
rank = descending_rank(score across universe)
```

**ATR for stops:**
```python
ATR_20 = SMA(true_range, 20)
```

### 4.1 The graded market filter (v2 change 2)

v1's X2 sold every position the day SPY closed below its 200-day SMA. v2 keeps the
filter and grades the response by a **target invested fraction**:

| Mode | Target invested | How it is entered |
|---|---|---|
| NORMAL | 85% (`regime.target_normal`) | SPY above its 200-day SMA at **2 consecutive** weekly checks |
| DEFENSIVE | 40% (`regime.target_defensive`) | SPY below its 200-day SMA at **2 consecutive** weekly checks |

A weekly check is the decision-day close (last trading day of the week). One
reading in the other direction changes nothing and resets the count, so a single
whipsaw week cannot flip the book. The mode is a **fold over the weekly readings**
that **starts DEFENSIVE** (fail closed: until two readings above the SMA exist the
book is not asked to be fully invested); a reading that cannot be evaluated counts
as below. Because it is a pure function of SPY's history (`strategy/regime.py`),
live rebuilds it from prices instead of storing state that could drift.

In DEFENSIVE the book takes **no new names and no top-ups**, and every position is
**trimmed pro rata** down to 40% whenever invested exceeds 45%
(`regime.defensive_trim_band`, 5 points, so a few days of drift does not trigger a
trade every week). Exits (§6) and cap trims still apply. When the mode returns to
NORMAL the target is 85% again; §8.1 says how trimmed positions come back.

**The 40% level is set only by this filter.** The live risk dial (§8.2) cannot
select it.

---

## 5. Entry rules

A position is opened only if **all** conditions hold:

| # | Condition |
|---|---|
| E1 | The regime mode is NORMAL (§4.1) |
| E2 | `rank <= 10` |
| E3 | `blended_momentum > 0` (never buy a falling asset, however it ranks) |
| E4 | `vol_63 <= 0.80` (excludes extreme volatility) |
| E5 | AI veto layer returns `flag == False` (§7) |
| E6 | Position count after entry `<= 10` |
| E7 | Proposed position value `>= $1,000` |
| E8 | Sector weight after entry `<= 40%` |
| ~~E9~~ | *Retired in v2.* There is no cap on new names per rebalance. |
| R1 | A name previously **sold in full** returns only under §6.2 |

**Implementation of E1-E8** (`src/strategy/rules.py`, shared by backtest and
live, pure functions):

- Candidates are the ranked, not-yet-held symbols in rank order. Slots are
  handed out greedily: only a candidate that passes every rule consumes a
  position slot (E6) and sector room (E8), so a name blocked by E8 does not
  starve the next one. Every failed rule is listed.
- **E5.** `flag == True` blocks. An unavailable AI layer proceeds without the
  veto (§7). In the **backtest E5 always passes**: a veto based on news as of a
  past date cannot be reproduced, and the backtest report says so.
- **E7** needs the sized dollar value, so it is enforced after sizing: a new
  position under $1,000 is dropped, the book resized, smallest first, until every
  new position clears the minimum (inclusive). Existing positions are never
  dropped by E7. At $15,000 an average 8.5% position is $1,275, so E7 starts to
  bite when equity falls under about $11,800.
- **E8** is checked with the candidate at `max_position_weight` (now 15%, the most
  sizing can give it), so an accepted entry cannot breach the cap once sized. A
  candidate whose sector is unknown blocks (fail closed).

### 5.1 The refill (v2 change 1)

Weekly, after the week's planned sells (§6): **if invested is below 80%**
(`regime.refill_below`), buy the highest-ranked names that pass E1-E8 and are not
held, until the allocation reaches the 85% target or there are no eligible names
or free slots.

- "Invested" is measured **after** the week's planned sells, so the proceeds from a
  stopped-out name are redeployed the same week instead of waiting for the next.
  A name sold in full this week is not a candidate this week.
- Names are added one at a time in rank order; after each, the bounded allocation
  (§8) is recomputed, and the loop stops at the first name that makes the target
  reachable. With equal volatilities five names cap at 15% each (75%) and a sixth
  is needed for 85%: six at 14.2%.
- Buys are limited to the **room under the target** (85% less what is held), so a
  refill can never carry the book past the target, whatever the allocation says.
- **No monthly cap on new names.** E9 is retired. v1 built the book two names a
  month and the volatility target then held it to ~35% invested; the invested
  target replaces both.
- A refill needs at least $1,000 of room per new name (E7). With less room than
  that it does nothing, and the book sits a little under 85%. That is the rule
  working, not a bug.

---

## 6. Exit rules

Checked **weekly** (decided at the close, filled at the next open). A "full exit"
sells the whole position; the ladder sells parts.

| # | Condition | Action |
|---|---|---|
| X1 | `rank > 16` | Full exit. Buffer from the entry threshold of 10 prevents churn |
| ~~X2~~ | *Retired.* Replaced by the graded filter, §4.1 | — |
| ~~X3~~ | *Retired.* Replaced by the ladder, §6.1 | — |
| X4 | `blended_momentum < 0` | Full exit. Thesis has inverted |
| L1 | close `<= 88%` of the highest close since entry (-12%) | Sell 1/3 |
| L2 | close `<= 80%` of that high (-20%) | Sell another 1/3 |
| L3 | close `<= 72%` of that high (-28%) | Sell the rest (full exit) |

**Implementation.** When several full-exit rules fire on one position the reported
rule is, in order, L3, X4, X1 (disaster stop, thesis, rank buffer); all that fired
are listed in the detail. Fail closed: if the close or the position high, X1 or X4
cannot be evaluated (no rank today because the name left the universe or has no
bar), the position is sold in full.

### 6.1 The laddered trailing stop (v2 change 3)

- **Measured from the highest CLOSE since entry** (`position_high`), never from the
  entry price and never intraday. A name bought at 100 that ran to 150 and is now
  130 is up 30% on entry and 13.3% below its peak: level 1 fires.
- **Each level fires at most once per position.** A position is "one position" from
  its buy until it is sold in full; a top-up (below) does not reset the ladder.
- Sizes. If `c` levels are now crossed and `fired` are already used, `c - fired`
  fire together and sell `(c - fired) / (n - fired)` of the **current** shares.
  That is a third of the position, then half of what is left (another third of the
  original), then all of it; it stays well-defined after a top-up. A gap through
  two levels sells both in one order.
- **After a partial sale: no top-up until the name closes above its prior peak
  close** (at a weekly check). The peak is the position high when the sale
  triggered. A top-up also needs E1-E5 and the sector cap; a name still held takes
  no E6 slot. The top-up buys back toward the name's target weight (§8) and is
  limited by the room under the invested target. Once it is made, the block is
  spent. *Consequence to be aware of:* because the levels do not reset, a name that
  has been topped back up to full size is protected only by L2 and L3 the next time
  it falls, not L1.
- Why 12/20/28. v1's stop measured 3 x ATR and fired at 6-12% below the peak, which
  ejected momentum names on ordinary noise. The ladder gives up a third at 12% and
  keeps the rest through a normal pullback of a volatile name.

### 6.2 Re-entry after a full exit (v2 change 3)

A name **sold in full** (X1, X4 or L3) may be bought again only when **all** hold:

1. `rank <= 10` (`reentry.max_rank`),
2. its close is above its own **50-day SMA** (`reentry.sma_days`),
3. at least **1 week** has passed since the exit decision (`reentry.min_weeks_after_exit`),
   so a name sold this week cannot be rebought this week.

plus every entry rule E1-E8. A NaN SMA fails. This replaces v1's only guard, which
barred rebuying a name in the week it was stopped out: waiting says nothing about
whether the name recovered, the SMA does. (v1 had no calendar lockout.)

### 6.3 There is no take-profit rule

This is a decision, not an omission. A fixed take-profit **cuts momentum's largest
winners**, and **selling winners early is the disposition effect**, which is one of
the standard explanations for why momentum exists at all: investors sell winners too
soon, so prices adjust slowly. A rule that sold at +X% would make the strategy do
what the anomaly punishes. Profits are protected instead by the **peak-based ladder**
(a name that has run and then gives back 12% starts to be sold) and by **trimming
names that drift above the cap** (§6.4). **Adding a take-profit rule later must spend
parameter budget** (§12).

### 6.4 Cap-drift trim

A position that grows above the 15% cap (`sizing.max_position_weight`) is trimmed
back to it at the next weekly fill (`CAP_TRIM`). The excess is computed on the
decision-day close as a share of the shares then held. This is the only way a
winner is reduced without the ladder, and it is a limit on concentration, not a
price target.

**`position_high`** = highest closing price since entry (not intraday high).

---

## 7. AI veto layer

**Authority: veto only. Cannot generate signals, select names, size
positions, or place orders.**

Runs against the proposed new entries (and top-ups) each week.

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
# 1. Inverse-volatility weights across the names to be held
inv_vol = 1 / vol_63

# 2. Scale to the invested target and bound each name to [5%, 15%].
#    A name that reaches a bound STAYS there; the rest share what is left.
target = 0.85            # NORMAL (regime.target_normal); 0.40 in DEFENSIVE, but
                         # that mode never sizes new positions (§4.1)
w = bounded_weights(inv_vol, lower=0.05, upper=0.15, budget=target)

# 3. Convert to dollars, then to shares
target_value = w * account_equity
shares       = target_value / current_price        # fractional OK at IBKR
```

**Bounded allocation, unchanged from 1.0.4 except for the budget.** Read literally,
"clip then renormalize" breaks its own cap: two names capped at 15% renormalize to
50% each. The implementation pins a name that hits a bound and shares what remains
among the others, so the 5% and 15% bounds always hold and, when every name is
capped, **the rest is cash**. The two agree whenever nothing is clipped. Gross
exposure is held to `max_gross_exposure` regardless. A book whose 5% floors alone
exceed the budget is infeasible and raises (fail closed: no buys that week).

**There is no portfolio-volatility target any more.** v1 also scaled the book down to
15% portfolio volatility; with volatile momentum names that left it 35% invested on
average. The invested target is now the only thing that sets the total.

At $15,000, six equal names are 14.2% ($2,125), ten equal names 8.5% ($1,275).

### 8.1 Top-ups and restores

Buys of names **already held** are limited to two cases, both sized to the name's
target weight and limited by the room under the invested target:

- **TOPUP** — a name partly sold on the ladder, once it beats its prior peak (§6.1).
- **RESTORE** — a name trimmed pro rata by the defensive filter (§4.1), as soon as
  the mode is NORMAL again, subject to E1-E5 and the sector cap. There is no peak to
  beat: a pro rata trim says nothing about the name.

Without RESTORE the target would be nominal. The refill only buys names **not held**,
and a full book of ten has no free slot, so positions trimmed to 40% would stay
trimmed until something exited. (This mechanism is an implementation consequence of
change 2, not a further parameter; it is written here so it is not rediscovered.)

### 8.2 The risk dial (live only, not simulated)

A manual cap on the invested target, for the times a human wants less risk than the
market filter asks for. **It is not simulated in the backtest** (a test holds that
line), so backtest results say nothing about it.

| Level | Invested target | Who sets it |
|---|---|---|
| normal | 85% | default |
| caution | 60% | manual |
| defensive | 40% | **only the market filter** (§4.1) |

Enforced in code (`src/risk/dial.py`, tests in `tests/unit/test_dial.py`):

- Two hand-settable levels only; asking for defensive or anything else raises.
- **Never above 85%.** The effective target is the minimum of the regime target,
  the dial and the normal target; the dial can lower exposure and never raise it.
  Config also refuses a normal dial level above `regime.target_normal`.
- **At most one change per 30 days** (`min_days_between_changes`), counted from the
  last recorded change.
- **A journal reason is required.** The change and its journal entry are one
  transaction; a change with no reason is refused, and the table has a CHECK as the
  last line.
- **Auto-expires after 4 weeks** (`expiry_weeks`) and reverts to normal. Expiry is
  not a change and does not restart the 30 days.
- **Counts as an override** in the adherence query and in `v_override_log`. Three
  consecutive weeks of overrides is a kill criterion (§14).

**Remainder stays in cash.** Weights that sum to less than the target because every
name is capped are deliberate, not a bug.

---

## 9. Rebalance schedule

| Action | Frequency | When |
|---|---|---|
| Data refresh + signal computation | Weekly | Saturday 18:00 ET |
| Exits and the ladder (§6), cap trims, regime check | **Weekly** | Acted on Monday open |
| Refill, top-ups, restores (§5.1, §8.1) | **Weekly** | Acted on Monday open, same plan |

Everything is decided from one pure function of the Friday close
(`strategy/plan.py`) and filled at the next open. There is no monthly entry week and
no drift rebalance any more: a position is only sold by a rule in §6 and only
bought by §5.1 or §8.1. (v1's monthly cadence, and its +/-25% drift rebalance,
belonged to the two-a-month build-up they no longer have.)

---

## 10. Risk limits and kill switches

```yaml
max_gross_exposure:        1.00    # NO LEVERAGE. Non-negotiable.
max_position_weight:       0.15
min_position_weight:       0.05
max_sector_weight:         0.40
max_positions:             10
min_position_value:        1000
max_orders_per_day:        8       # NOTE: a 10-name refill can exceed this; see below
max_order_value:           4000

halt_on_drawdown:          0.20    # requires manual restart
halt_on_data_stale_days:   5
halt_on_broker_disconnect: true
require_manual_approval:   true    # keep TRUE for the first 12 months live
```

**Fail closed.** Any check that cannot be evaluated counts as a failure.

`max_orders_per_day` (8) and the 10-position book interact: a full refill from
DEFENSIVE can be ten or more orders in one Monday. The risk engine (build step 11)
is a stub and is not applied in the backtest; whether to raise that limit or spread
the refill over two days is a decision for that step, made deliberately and logged.
It is not changed here.

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
3. **Ten positions is still concentrated.** A single blow-up is up to 15% of capital
   (8.5% on average). This is the cost of trading a $15,000 account; it does not go
   away, and ten names at 5% floors leave no room to be defensive at 40% by sizing
   alone (§4.1 trims instead).
4. **Survivorship bias.** IBKR data has no point-in-time index membership and
   often lacks bars for delisted names, so the backtest will be optimistic by
   a meaningful and unmeasured amount.
   The universe is built from **today's** S&P 500 membership (§2.1), so every
   company that fell out of the index since 2007 is absent from the backtest.
   Since 1.0.3 the backtest chooses the top 150 point-in-time from those
   constituents (§2.3), which removes the hindsight in *which of them* are
   liquid, but not the bias in the membership list itself, nor sectors taken
   from today's labels.
5. **Tax drag.** Weekly refills, ladder sales and trims generate short-term capital
   gains in a taxable account. Post-tax returns will be materially below the
   backtest.
6. **Crowding.** Momentum is widely known and widely traded. Published
   anomalies tend to decay after publication.
7. **A fully invested book flatters itself on this universe.** The membership list is
   today's S&P 500 (§2.3), so a book that is 85% invested in its top names collects
   more of the survivorship bias than v1's 35% did. This is why A1 now requires
   SPY + 2.0 points (§13), and why a pass is read as "not obviously broken", not as
   proof.
8. **The graded filter is late by design.** Two weekly readings below the SMA before
   the book goes to 40% means a fast crash is mostly taken at 85%. That is the price
   of not whipsawing; it is the same trade v1's single reading made in the other
   direction.
9. **Ladder levels do not reset after a top-up (§6.1).** A name topped back up to full
   size has only L2 and L3 left, so its next 12% fall is not acted on.

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
bar whose close produced the signal. **Every decision is weekly** (v2): the regime
reading, exits and the ladder, cap trims, top-ups, restores and the refill all come
from one call of the shared planner (`strategy/plan.py`). A name sold in full in a
week is not a candidate in the same week. A fill with no bar to trade on (a halted
name) does not happen and is retried the following week (a ladder level is only
marked used when its sale fills); a bar is never invented for it. Sells fill before
buys, so proceeds fund the buys; a buy never spends more cash than is held.

**State the engine carries** (all derivable from prices and its own fills, none
stored in a positions table): shares, cash, each position's highest close, the ladder
levels used, the peak to beat for a top-up, the names awaiting a restore, the
decision week of each full exit, and the regime state (§4.1).

**Continuous simulation, out-of-sample scoring.** Nothing is fitted, so
in-sample years cannot be tuned on and every parameter is identical in every
window. The strategy is simulated in one continuous run from the first date
signals exist; in-sample years exist only so the first scored window starts from
a realistic book. Only the out-of-sample windows are reported. Starting each
window flat would charge every year a ramp-up (the regime starts DEFENSIVE and
needs two readings, the book takes a week or more to fill, cash earns 0%) that live
trading pays once.

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
that sum is not positive there is no edge to apportion and A6 **fails**, printed as
`A6  FAIL  no positive excess return` rather than a NaN. Any criterion that cannot
be computed fails.

**Quarterly universe, as live.** Membership is rebuilt on the first trading day
of each quarter (§2.3) and is stable in between, so a held name cannot lose its
rank mid-quarter. At a rebuild a held name that no longer qualifies has no rank
and exits under X1 (unevaluable -> exit, §6), as it would live.

**An invalid run cannot pass.** `BacktestOptions` switches (same-bar execution,
no costs, today's universe) exist only so tests can break the engine on purpose.
`check_acceptance` returns FAIL with reason "invalid run" if any is set,
whatever the numbers, and `main` records such a run as failed with no report and
no verdict.

**Left out, and printed in every report:** E5 always passes; the risk engine's
order-level checks (order size, order count, drawdown halt) are not applied; **the
live risk dial is not simulated** (§8.2); sectors, security type and index
membership are today's (§2.3).

**What the report adds in v2.** Average invested fraction; the share of days at the
85% and 40% targets; sells per ladder level (L1, L2, L3) and by X1, X4, cap-drift
trim and defensive trim; top-ups after partial sales; restores after defensive
trims; re-entries after full exits; new entries. They exist so a change in behavior
is visible without reading trades, and so the failure v1 showed (a book 35%
invested, a stop that fired at 6-12%) would show up in the first report.

**Parameter budget: 5 changes, total, logged.**

Every time a parameter is adjusted after seeing results, some out-of-sample
validity is spent. Keep a log (the same rows are in the `parameter_changes` table,
phase `backtest`; `SELECT 5 - COUNT(*) FROM parameter_changes` is what is left):

| # | Date | Parameter | From | To | Why |
|---|---|---|---|---|---|
| 1 | 2026-09-21 | Invested target (replaces the volatility target **and** E9) | Portfolio volatility target 15%, scale down only; E9 at most 2 new names per rebalance; monthly entries | Target 85% invested while the market filter is on; weekly refill when invested < 80%; no cap on new names | v1 averaged 35% invested; the vol target and E9 together kept it mostly in cash while SPY compounded |
| 2 | 2026-09-21 | Market filter response (replaces X2's full exit) | X2: sell everything at one weekly reading of SPY below its 200-day SMA | Two consecutive readings: target 40%, trim pro rata, no new names; two consecutive above: back to 85% | An all-or-nothing exit on one reading whipsaws and misses recoveries; the graded response keeps some exposure and needs confirmation |
| 3 | 2026-09-21 | Trailing stop and re-entry (replaces X3) | 3.0 x ATR stop from the highest close (fired at ~6-12%, 177 of 391 v1 sells); only guard against rebuying was the same week | Ladder at -12%/-20%/-28% from the highest close, selling 1/3 each; top-up only after a new peak; re-entry only at rank <= 10 and close > 50-day SMA, at least 1 week later | v1's stop was tighter than its own spec claimed and was the top exit; a ladder gives up a third early and keeps the rest through ordinary pullbacks |
| 4 | 2026-09-21 | Number of positions | max_positions 6, entry rank <= 6, exit rank > 10, weights 8%-20% | max_positions 10, entry rank <= 10, exit rank > 16, weights 5%-15%; sector cap (40%) and min value ($1,000) unchanged | With a book that is actually invested, six names is a 20%-cap concentration; ten spreads it and keeps a churn buffer (in at rank 10, out beyond 16) |

**Spent after v2: 4 of 5.** The last change is **reserved for a problem found in
paper trading**, not for tuning the backtest. The A1 margin (§13) is recorded in
the changelog and here, not counted in the table: it *tightens* the test and was set
before any v2 result exists, so it cannot have been fitted to one.

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
| A1 | CAGR vs. SPY, after costs | **>= SPY CAGR + 2.0 percentage points** (`beat_benchmark_margin`) |
| A2 | Max drawdown | < 35% |
| A3 | Worst rolling 12-month return | > −30% |
| A4 | Sharpe ratio | > 0.6 |
| A5 | Annual turnover | < 400% |
| A6 | Consistency | No single walk-forward window contributes > 50% of total excess return |

**A6 is the one most people skip.** A strategy whose entire edge came from
one lucky year is not a strategy.

**Why A1 is now SPY + 2.0 points (set 2026-09-21, before any v2 result exists).** The
universe's membership list is today's S&P 500 (§2.3), which favors a fully invested
momentum book: it holds names that, by construction, survived and grew. v1 hid that
behind a 35% invested book; v2 will not. So matching SPY is not evidence. This makes
the test harder, not easier, which is why it is safe to write down now.

**If A1 fails: stop.** Buy SPY, keep the hour. That is a legitimate and
honorable outcome, and it costs you nothing but the build time.

**If v2 fails any criterion, development as a trading strategy stops (§0).** There is
no v3 and no retuning v2 to pass. **Whatever the result, v2 runs on paper only.** A
pass earns twelve weeks of paper trading (CLAUDE.md, build order), not live money.

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
| 1.0.5 | 2026-09-20 | Pre-backtest; spends no parameter budget. Backtest engine implemented; the protocol's open choices are fixed in §12.1: signals at the weekly close and fills at the next open, monthly entries and weekly exits, continuous simulation with out-of-sample-only scoring, windows anchored to the data start with a flagged partial last window, half the spread charged per side, turnover and Sharpe definitions, A6 and uncomputable criteria fail closed, no same-week rebuy of a stopped-out name; the backtest universe is rebuilt quarterly to match live (supersedes the daily recompute of 1.0.3); a run with any BacktestOptions switch set fails acceptance as "invalid run". config.yaml `strategy.version` now matches (metadata only). |
| 2.0.0 | 2026-09-21 | **New strategy, `weekly_momentum_v2`, written after v1 failed acceptance (tag `backtest-v1`; v1's verdict is final).** Spends 4 of 5 parameter changes, each logged in `parameter_changes` with its rationale (§12): (1) invested target 85% replaces the volatility target and E9 (weekly refill below 80%, no cap on new names, inverse-vol weights bounded 5%-15% scaled to the target; §5.1, §8); (2) graded market filter replaces X2 (two consecutive weekly readings: 40% target, pro rata trim, no new names; two above: 85%; §4.1); (3) laddered trailing stop -12/-20/-28% from the highest close replaces X3, with top-up only after a new peak and re-entry only at rank <= 10 and above the 50-day SMA, at least 1 week later (§6.1, §6.2); (4) ten positions, entry rank <= 10, exit rank > 16 (§6, §8). **No take-profit rule, with the reason recorded (§6.3).** Adds the live-only risk dial (§8.2), not simulated. A1 tightened to SPY + 2.0 points (§13). **If v2 fails, development as a trading strategy stops; v2 runs on paper only.** Implementation readings fixed here: the regime folds from a DEFENSIVE start; invested is measured after the week's sells; RESTORE brings defensively-trimmed names back (§8.1); cap-drift trim added and the +/-25% drift rebalance and monthly entry cadence retired; A6 prints `no positive excess return`. Retired keys: `exit.trailing_stop_atr`, `exit.exit_all_on_market_off`, `entry.max_new_per_rebalance`, `sizing.target_portfolio_vol`, `scale_up_allowed`, `covariance_window`, `schedule.drift_tolerance`. `strategy.name` and `version` in config.yaml match. |
