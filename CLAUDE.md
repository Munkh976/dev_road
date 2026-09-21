# CLAUDE.md — dev_road

Guidance for Claude Code working in this repository.

---

## What this is

A rules-based weekly momentum trading system for a $15,000 Interactive
Brokers account. Single user, runs on one Windows laptop, ~1 hour per week.
The system **proposes** trades; a human approves them before anything reaches
the broker.

**Read `docs/STRATEGY_SPEC.md` before changing any behavior.** It defines the
rules, the rationale, the known weaknesses, and the acceptance criteria. This
file tells you how to work in the repo; that file tells you what the repo is
supposed to do.

`config.yaml` holds every operational parameter. There are no numeric
literals in `src/` by design — if you need a number, add it to config.

---

## Non-negotiable invariants

These are enforced in code with validators and tests. Do not weaken, bypass,
or "temporarily" disable any of them. If a task seems to require it, stop and
say so rather than working around the guard.

| Invariant | Enforced in |
|---|---|
| **No leverage.** `max_gross_exposure` may never exceed 1.00 | `src/config.py` validator |
| **No bare market orders.** LIMIT only, marketable offset | `config.py` + `OrderRequest.__post_init__` |
| **The AI layer cannot trade.** `ai.role` must be `veto_only` | `config.py` validator |
| **`rank_exit` > `rank_threshold`.** The buffer prevents churn | `config.py` cross-check |
| **IBKR pacing ≤ 6 requests/minute** (60 per 10 min is IBKR's hard cap) | `src/data/pacing.py` |
| **Orders default to `dry_run=True`** | `BrokerInterface.submit_order` |
| **Risk rejections are final.** A `risk_status = REJECT` proposal cannot be approved | `datasette/plugins/approval.py` raises `Forbidden`, and the write's `WHERE` clause refuses it again |
| **Fail closed.** A check that cannot be evaluated counts as a failure | risk engine |
| **No forward-filling price data.** Gaps stay NaN | `PriceCache.load_matrix` |

### Why no forward-fill

A forward-filled bar for a halted or delisted name manufactures a tradable
price that never existed. The backtest then "trades" at it. This is one of
the quietest ways a backtest lies.

---

## Architecture decisions already made

Do not relitigate these. They were decided deliberately; if you think one is
wrong, raise it rather than changing it.

**SQLite, not PostgreSQL.** Single user, one laptop, under a million rows.
Postgres would add backups, migrations and a service to keep alive for zero
benefit, and SQLite means Datasette serves the approval UI for free.

**Parquet for price history, SQLite for records.** Bars are immutable and the
backtest loads the whole matrix repeatedly across walk-forward windows.
Columnar parquet reads into pandas roughly an order of magnitude faster for
that pattern. SQLite holds runs, proposals, approvals, orders, journal.

**Positions and balances are never stored.** They are read live from IBKR on
every run. **This is why there is no reconciliation engine** — there is no
second copy to drift. Do not add a positions table.

The single exception: `position_state.position_high` (highest close since
entry, for the trailing stop). IBKR does not track it, so we must.

**Human approval stays in the loop.** `require_manual_approval: true` for at
least the first 12 months live. Approving writes an approval row; a separate
execution step submits orders. A stray click cannot reach the market.

**Historical data comes from the parquet cache, never on demand.** Only
`src/data/refresh.py` talks to IBKR for history, so pacing is managed in
exactly one place. `BrokerInterface` deliberately has no
`get_historical_data` method.

---

## Current status

**Built and tested (see `python -m pytest tests\ -q` for the current count):**

- `src/config.py` — typed pydantic loader, safety validators
- `db/schema.sql` — 16 tables, 4 views (adds `contract_info`, `universe_snapshots`,
  `risk_dial_changes`)
- `scripts/init_db.py` — idempotent
- `src/data/pacing.py` — token bucket + sliding window
- `src/data/cache.py` — parquet cache, incremental merge, bar validation
- `src/data/universe.py` — constituents CSV loader (`BRK.B` -> `BRK B`), section 2
  filters, `universe_snapshots` read/write
- `src/data/refresh.py` — `build_universe()`, `fetch_symbol()`, `update_symbol()`
  (overlap-based restatement check, full refetch), weekly and
  `--rebuild-universe` and `--backfill` refresh (`--backfill --all-constituents`
  is resumable, for the point-in-time backtest universe), data gate. Tested against a
  mocked IB; needs no gateway. Run against a real gateway: one full universe
  rebuild (504/504) and a SPY backfill to 2004.
- `scripts/update_constituents.py` — manual quarterly helper; dry run by default
- `src/strategy/signals.py` — momentum, volatility, ATR, regime over the full
  panel; `compute()` cuts at `as_of`. Look-ahead tested per signal.
- `src/strategy/run_signals.py` — cache -> `signals` table + `runs` row; halts
  on a stale SPY. Run against the real cache once.
- `src/strategy/rules.py` — E1–E8, R1, T1, X1, X4, the laddered stop, sizing
  (`size_new_positions` enforces E7; bounds hold with few names, see spec §8).
  **v2.0.0**: E9, X2, X3 and the volatility target are retired (spec §0)
- `src/strategy/regime.py` — graded market filter, a pure fold over weekly readings
- `src/strategy/plan.py` — the weekly order plan (exits, ladder, trims, top-ups,
  refill); the one place backtest and live decide what to trade
- `src/risk/dial.py` — live-only risk dial (85%/60%, once a month, journal reason,
  4-week expiry, counts as an override); the backtest never reads it
- `src/backtest/walkforward.py` — walk-forward engine (spec 12.1), `check_acceptance`
  A1–A6 (A1 is SPY + 2.0 points since v2), report. Tested on synthetic markets only
  (hand-checked trades, whole-engine look-ahead, deliberate breaks); **v2 has never
  been run on real data**. **v1 was run once and FAILED** (git tag `backtest-v1`,
  spec 1.0.5): its verdict is final and it is not rerun with new parameters
- `src/data/universe.py` also has `point_in_time_universe` (spec 2.3), used by the backtest
- `src/runlog.py` — shared `runs` row start/finish with the config hash
- `src/execution/broker.py` — interface and dataclasses
- `datasette/metadata.json` — 10 canned queries, all verified against schema
- `datasette/plugins/approval.py` — approve/reject/halt/journal routes; atomic
  decisions, same-origin guard on every POST
- `datasette/plugins/host_guard.py` — Host allowlist on every request (DNS
  rebinding); 34 integration tests for both plugins in `tests/integration/`

**Stubs — signatures and docstrings fixed, bodies raise `NotImplementedError`:**

- `src/risk/engine.py` — 14 checks
- `src/ai/veto.py` — prompt and schema written, call not wired
- `src/execution/ibkr.py` — ib_async implementation
- `src/execution/approve.py`, `src/reporting/weekly.py`

When implementing a stub, keep the existing signature and docstring. The
docstrings encode decisions, not just description.

---

## Build order — do not skip ahead

Each step is useless without the one before it.

1. ~~Config + validation~~ ✅
2. ~~SQLite schema~~ ✅
3. ~~Parquet cache + pacing~~ ✅
4. ~~Broker interface~~ ✅
5. ~~`build_universe()`~~ ✅
6. ~~IBKR fetch~~ ✅ (mocked; first real run is a manual step)
7. ~~Signals~~ ✅ (explicit look-ahead tests)
8. ~~Rules + sizing~~ ✅ (moved ahead of the backtest, which needs them)
9. ~~Backtest engine~~ ✅ (synthetic data only; the first real run is a manual step)
10. **Acceptance gate (A1–A6)** — v1 ran and **failed** (tag `backtest-v1`). v2 is
    written (spec 2.0.0) and its first real run is a manual step ← next.
    **If v2 fails, development as a trading strategy stops; buy SPY.** Either way v2
    runs on paper only
11. Risk engine
12. AI veto layer
13. Report + approval UI
14. IBKR execution, paper only
15. 12 weeks paper trading
16. Live at $3,000, manual approval

Step 9 is a real gate, not a formality. It exists so the stop decision is
pre-committed rather than made later while attached to weeks of work.

---

## The parameter budget

**Five changes total**, logged in the `parameter_changes` table. **v2 spent four**
(invested target, graded filter, ladder and re-entry, ten positions; spec §12). The
last one is reserved for a problem found in paper trading, not for tuning the backtest.

Every adjustment made after seeing backtest results spends out-of-sample
validity. Ten tweaks and the backtest is fiction.

If a task would change a value in `config.yaml` after backtesting has begun,
**stop and ask** rather than editing. Then log it:

```sql
INSERT INTO parameter_changes (changed_at, parameter, old_value, new_value, rationale, phase)
VALUES (...);
SELECT 5 - COUNT(*) AS remaining FROM parameter_changes;
```

---

## Commands (Windows)

`make` is not installed on Windows by default. Either install it
(`winget install ezwinports.make` or `scoop install make`) or run the Python
modules directly — they are equivalent:

| Task | make | direct |
|---|---|---|
| Init database | `make init-db` | `python scripts\init_db.py` |
| Check config | — | `python -m src.config` |
| Refresh cache | `make refresh` | `python -m src.data.refresh` |
| Signals | `make signals` | `python -m src.strategy.run_signals` |
| Proposals | `make propose` | `python -m src.strategy.propose` |
| Report | `make report` | `python -m src.reporting.weekly` |
| Approve | `make approve` | `python -m src.execution.approve` |
| Backtest | `make backtest` | `python -m src.backtest.walkforward` |
| Tests | `make test` | `python -m pytest tests\ -v` |

Datasette:
```
datasette serve db\operations.db --metadata datasette\metadata.json --plugins-dir datasette\plugins --template-dir datasette\templates --reload
```

Virtual environment: `.venv\Scripts\activate`

---

## Conventions

- **Python 3.11+**, type hints everywhere, `from __future__ import annotations`
- **`pathlib.Path`, never string paths.** This repo runs on Windows; a
  hardcoded `/` will break it.
- **No numeric literals in `src/`.** Everything comes from `Config`.
- **Pure functions in `src/strategy/`** — no IO, no broker, no database. That
  is what lets backtest and live paths share identical code.
- **Datasette `execute_write_fn` does not wrap in a transaction** (0.65.5). It
  just runs your function on the write connection, so write functions need
  their own `with conn:` for commit/rollback. Without it, a failure midway
  leaves half-written state.
- **Tests alongside implementation.** A stub is not done until it has a test.
- **Signal functions must be tested for look-ahead** by feeding truncated data
  and asserting the output is unchanged.
- Docstrings explain *why*, not *what*. The existing ones encode reasoning
  that should survive refactoring.

### Commit messages

```
feat(data): implement build_universe with liquidity filters
fix(cache): handle restated bars on incremental merge
test(signals): assert no look-ahead in momentum computation
```

---

## Things to refuse

If a request would do any of these, say so rather than complying:

- Raise `max_gross_exposure` above 1.00, or add margin/leverage anywhere
- Give the AI layer authority to select, size, or order
- Add a positions table or reconciliation engine
- Forward-fill price data
- Change `paper_trading` to `false` before acceptance criteria pass
- Remove or soften a risk check to "unblock" something
- Skip build-order steps (e.g. wire IBKR execution before the backtest)
- Set `require_manual_approval: false`
- Edit `config.yaml` post-backtest without logging the change
- Add a take-profit rule, or simulate the risk dial in the backtest (a take-profit
  spends budget; the dial is live only, spec §6.3 and §8.2)
- Retune v2 to pass, or write a v3, after a failed acceptance run

None of these are hypothetical failure modes; each is a known way that
systematic trading projects lose real money.
