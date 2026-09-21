# weekly_momentum_v2

A rules-based weekly momentum system for a $15,000 IBKR account. Runs on one
laptop, takes about an hour a week, and proposes trades that **you** approve
before anything reaches the broker.

> **Status: scaffold.** Config, database, price cache, pacing limiter and
> broker interface are built and tested. Signals, risk engine, backtest and
> IBKR calls are stubs with fixed contracts. Nothing has been backtested, so
> nothing here is validated. See `docs/STRATEGY_SPEC.md` for what it is supposed
> to do and `docs/STRATEGY_SPEC.md` section 13 for the criteria it must meet
> before a single dollar moves.
>
> Not investment advice. A backtest is a hypothesis about the past.

---

## What it does

Rank 150 liquid S&P names by 6- and 12-month momentum divided by volatility,
hold up to 10, aiming to be 85% invested while SPY is above its 200-day average
and 40% invested once it has been below for two weekly checks. Enter at rank 10,
exit beyond rank 16. Size by inverse volatility, 5%–15% per name. A laddered
trailing stop sells a third at −12%, −20% and −28% from each name's highest
close; there is deliberately no take-profit rule.

Everything is decided weekly from one pure plan and filled at the next open:
exits and the ladder, trims, then a refill toward the target when the book is
under 80% invested.

**v1 failed its acceptance test** (tag `backtest-v1`: CAGR 4.2% vs SPY 14.3%,
35% invested). v2 is the one allowed rewrite. If it fails too, development as a
trading strategy stops; either way it runs on paper only.

Full rules, rationale, known weaknesses and acceptance criteria:
**`docs/STRATEGY_SPEC.md`**. Every parameter: **`config.yaml`**.

---

## Architecture

```
                      ┌──────────────┐
                      │ config.yaml  │  every parameter, nowhere else
                      └──────┬───────┘
                             │
   ┌─────────────────────────┼─────────────────────────┐
   │                         │                         │
   ▼                         ▼                         ▼
┌──────────┐        ┌────────────────┐        ┌────────────────┐
│  IBKR    │        │ parquet cache  │        │ SQLite         │
│  live    │        │ price history  │        │ operations.db  │
├──────────┤        ├────────────────┤        ├────────────────┤
│ positions│        │ immutable      │        │ runs           │
│ balances │        │ incremental    │        │ signals        │
│ quotes   │        │ ~50MB          │        │ proposals      │
│ orders   │        │ 6 req/min cap  │        │ approvals      │
└────┬─────┘        └───────┬────────┘        │ orders         │
     │                      │                 │ journal        │
     │  source of truth     │  numbers        │ audit trail    │
     │  never stored        │                 └───────┬────────┘
     │                      │                         │
     └──────────┬───────────┴─────────────────────────┘
                ▼
        ┌───────────────┐
        │ signals       │  pure functions, no IO
        └───────┬───────┘
                ▼
        ┌───────────────┐
        │ rules + plan  │  entries / exits / ladder / sizing
        │ regime        │
        └───────┬───────┘
                ▼
        ┌───────────────┐
        │ AI veto       │  advisory only — cannot signal, size or order
        └───────┬───────┘
                ▼
        ┌───────────────┐
        │ risk engine   │  deterministic, fails closed, cannot be overridden
        └───────┬───────┘
                ▼
        ┌───────────────┐
        │ proposals     │  written to SQLite. NOT orders.
        └───────┬───────┘
                ▼
        ┌───────────────┐
        │ YOU           │  CLI or Datasette
        └───────┬───────┘
                ▼
        ┌───────────────┐
        │ IBKR          │  marketable limit orders only
        └───────────────┘
```

### Three storage decisions worth understanding

**Positions and balances are never stored.** They are read live from IBKR on
every run. This is why there is no reconciliation engine — there is no second
copy to drift out of sync. The one exception is `position_state.position_high`
(highest close since entry, for the trailing stop), which IBKR does not track.

**Price history is parquet, not a database.** Bars are immutable and the
backtest loads the whole 150×3780 matrix repeatedly across walk-forward
windows. Columnar parquet reads into pandas roughly an order of magnitude
faster than row-oriented SQLite for that pattern.

**Records are SQLite, not PostgreSQL.** Single user, one laptop, under a
million rows. Postgres would add backups, migrations and a service to keep
alive for no benefit — and SQLite means Datasette serves the approval UI for
free.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env          # then fill it in
make init-db
make test
```

Verify the config loads and its guards work:

```bash
python -m src.config
```

### Running IB Gateway

Port **4002** is paper, **4001** is live. `config.yaml` is set to 4002 and
`account.paper_trading` is `true`. Both must change together before anything
touches real money, and `connect()` asserts the account id matches
`IBKR_EXPECTED_ACCOUNT_ID` in `.env` as a third guard.

---

## Weekly workflow (~1 hour, Saturday)

| Min | Command | What happens |
|---|---|---|
| 0–25 | `make refresh` | Pulls new bars from IBKR at 6/min. Runs unattended. |
| 25–30 | `make signals` | Computes ranks from the cache, writes to SQLite. |
| 30–35 | `make propose` | Applies rules + risk engine, writes proposals. |
| 35–45 | `make report` | HTML you read on laptop or phone. |
| 45–50 | `make approve` | You decide. Or `make serve` for the browser UI. |
| 50–60 | — | Write the journal entry. |

The last ten minutes are not padding. The journal is how you find out, six
months in, whether you are actually following the system. Most retail systems
fail there rather than in the code.

---

## Safety properties

These are enforced in code, not documentation. Each has a test or a validator.

| Property | Where enforced |
|---|---|
| No leverage, ever | `config.py` validator rejects `max_gross_exposure > 1.0` |
| No bare market orders | `config.py` validator + `OrderRequest.__post_init__` |
| AI cannot trade | `config.py` rejects any `ai.role` other than `veto_only` |
| Rank buffer prevents churn | `config.py` cross-check: `rank_exit > rank_threshold` |
| IBKR pacing respected | `PacingLimiter` sliding window + token bucket, capped at 6/min |
| Bad bars refused | `PriceCache._validate` rejects non-positive close, high<low, duplicates |
| No forward-fill | `load_matrix` leaves gaps NaN — a filled bar invents a tradable price |
| Risk rejections are final | Approval plugin raises `Forbidden` on `risk_status = REJECT` |
| Orders default to dry run | `submit_order(dry_run=True)` |
| Overrides are counted | `approvals.override` + `v_override_log` |
| System fails closed | Any uncheckable condition counts as a failure |

Run `make test` to see the guards fire.

---

## Approval UI

```bash
make serve     # http://localhost:8001
```

Canned queries, ordered by how often you will want them:

- **weekly_review** — everything awaiting a decision, exits first
- **top_ranked** — current rankings with regime
- **adherence** — overrides per week (3 in a row is a kill criterion)
- **drawdown** — equity curve with HALT/WARN status
- **fills_vs_reference** — actual slippage vs the 5bps the backtest assumed
- **run_health** — did the pipeline run, did the data gate pass
- **parameter_budget** — how many of your 5 changes remain

Approving writes an approval row. It does not place an order — a separate
execution step reads approved proposals and submits them. A stray browser
click cannot reach the market.

---

## Hardware

| | |
|---|---|
| Storage | ~4 GB total (price cache is only ~50 MB) |
| RAM, weekly run | under 500 MB |
| RAM, backtesting | 8 GB works with chunked sweeps; 16 GB comfortable |
| CPU | any laptop from the last five years |

Scheduling `make refresh` via cron requires the laptop to be awake —
`caffeinate -s` on macOS, a wake timer in Task Scheduler on Windows. Or just
run it when you sit down; 25 of the 60 minutes is the machine fetching while
you read last week's journal.

---

## Build order

Do not skip ahead. Each step is useless without the previous one.

- [x] **1. Config + validation** — guards tested
- [x] **2. SQLite schema** — 16 tables, 4 views
- [x] **3. Parquet cache + pacing limiter** — tested
- [x] **4. Broker interface** — abstraction fixed
- [x] **5. `build_universe()`** — the S&P filter in `src/data/refresh.py`
- [x] **6. IBKR fetch** — `fetch_symbol()`, tested against a mocked IB; first real `make refresh` still to do

  First real run, in order (each step catches problems the next would repeat 100x):

  ```
  python scripts\update_constituents.py --write         # review git diff, commit
  python -m src.data.refresh --symbols SPY,AAPL,MSFT    # ~1 min smoke test, no snapshot
  python -m src.data.refresh --rebuild-universe         # ~83 min, quarterly
  python -m src.data.refresh                            # ~25 min, weekly
  ```

  The smoke test prints bars, date range, last close, 20-day dollar volume,
  stockType and industry per symbol. Check the dollar volume against what you
  expect (wrong units shows up here) and that no symbol reports 354 or 162.
- [ ] **7. Signals** — `src/strategy/signals.py`, with a look-ahead test
- [ ] **8. Backtest** — walk-forward, out-of-sample only
- [ ] **9. Acceptance gate** — A1–A6. **If A1 fails, stop and buy SPY.**
- [ ] **10. Rules + sizing**
- [ ] **11. Risk engine**
- [ ] **12. AI veto layer**
- [ ] **13. Report + approval**
- [ ] **14. IBKR execution, paper only**
- [ ] **15. 12 weeks paper trading**
- [ ] **16. Live at $3,000, manual approval**

Step 9 is a real gate, not a formality. It is written into the spec so the
decision is pre-committed rather than made later while attached to three
weeks of work.

---

## The parameter budget

Five changes. Total. Logged in `parameter_changes`.

Every adjustment made after seeing backtest results spends some out-of-sample
validity. Ten tweaks and the backtest is fiction. When the budget is
exhausted, the spec is frozen.

```sql
SELECT 5 - COUNT(*) AS remaining FROM parameter_changes;
```

---

## Layout

```
weekly-momentum/
├── docs/
│   └── STRATEGY_SPEC.md     the rules and why — read this first
├── config.yaml              every parameter
├── Makefile                 all commands
├── db/schema.sql            16 tables, 4 views
├── scripts/init_db.py
├── src/
│   ├── config.py            typed loader + safety validators
│   ├── data/
│   │   ├── pacing.py        IBKR 6/min limiter
│   │   ├── cache.py         parquet price cache
│   │   └── refresh.py       weekly / quarterly cache refresh
│   ├── strategy/
│   │   ├── signals.py       pure functions
│   │   ├── regime.py        graded market filter (2 weekly readings)
│   │   ├── rules.py         E1-E8, R1, X1, X4, ladder, sizing
│   │   └── plan.py          the weekly order plan, shared by backtest and live
│   ├── ai/veto.py           advisory only (stub)
│   ├── risk/engine.py       deterministic, fails closed (stub)
│   ├── risk/dial.py         live-only invested-target cap, journalled
│   ├── execution/
│   │   ├── broker.py        interface
│   │   ├── ibkr.py          ib_async implementation (stub)
│   │   └── approve.py       CLI (stub)
│   ├── backtest/walkforward.py
│   └── reporting/weekly.py
├── datasette/
│   ├── metadata.json        canned queries
│   └── plugins/approval.py  approve/reject/halt routes
├── data/cache/              parquet (gitignored)
├── reports/                 generated HTML (gitignored)
├── journal/TEMPLATE.md
└── tests/
```
