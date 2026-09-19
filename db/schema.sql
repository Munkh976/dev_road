-- weekly_momentum_v1 — operational store
--
-- SQLite holds RECORDS (runs, proposals, approvals, orders, fills, journal).
-- Parquet holds PRICE HISTORY. IBKR holds POSITIONS AND BALANCES.
-- Nothing here duplicates broker state — that is why there is no
-- reconciliation engine to maintain.
--
-- Every table is designed to be readable in Datasette without a join.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;


-- ===========================================================================
-- runs — one row per pipeline execution. The spine of the audit trail.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,          -- uuid4
    started_at        TEXT NOT NULL,             -- ISO8601 UTC
    finished_at       TEXT,
    run_type          TEXT NOT NULL,             -- refresh|signals|propose|execute|backtest
    strategy_name     TEXT NOT NULL,
    strategy_version  TEXT NOT NULL,
    config_hash       TEXT NOT NULL,             -- sha256 of config.yaml at run time
    mode              TEXT NOT NULL,             -- paper|live
    status            TEXT NOT NULL,             -- running|ok|halted|failed
    halt_reason       TEXT,                      -- populated when status='halted'
    error             TEXT,
    notes             TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);


-- ===========================================================================
-- data_quality — the staleness / validity gate. A failure here halts the run.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS data_quality (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL REFERENCES runs(run_id),
    checked_at        TEXT NOT NULL,
    symbols_expected  INTEGER NOT NULL,
    symbols_fetched   INTEGER NOT NULL,
    symbols_stale     INTEGER NOT NULL,
    symbols_short     INTEGER NOT NULL,          -- fewer bars than min_bars_required
    benchmark_bar_date TEXT,                     -- last SPY bar we have
    staleness_days    INTEGER,
    price_jumps       INTEGER NOT NULL DEFAULT 0,-- suspicious single-day moves
    duplicate_bars    INTEGER NOT NULL DEFAULT 0,
    passed            INTEGER NOT NULL,          -- 0/1 — 0 means NO ORDERS
    detail            TEXT                       -- json
);


-- ===========================================================================
-- signals — one row per symbol per signal date. Reproducible from the cache.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS signals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL REFERENCES runs(run_id),
    signal_date       TEXT NOT NULL,             -- last completed trading day
    symbol            TEXT NOT NULL,
    close             REAL NOT NULL,
    mom_6m            REAL,
    mom_12m           REAL,
    blended_momentum  REAL,
    vol_63            REAL,
    score             REAL,
    rank              INTEGER,
    atr_20            REAL,
    dollar_volume_20d REAL,
    sector            TEXT,
    market_on         INTEGER NOT NULL,          -- 0/1, same for every row in a run
    UNIQUE(run_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_signals_date_rank ON signals(signal_date, rank);


-- ===========================================================================
-- ai_reviews — veto layer output. Advisory only; cannot create a proposal.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS ai_reviews (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL REFERENCES runs(run_id),
    reviewed_at       TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    model             TEXT,                      -- model id actually used
    prompt_version    TEXT NOT NULL,
    flag              INTEGER,                   -- 0/1; NULL = layer unavailable
    severity          TEXT,                      -- low|medium|high
    reasoning         TEXT,
    sources           TEXT,                      -- json array
    status            TEXT NOT NULL,             -- ok|invalid_schema|error|disabled
    raw_response      TEXT,
    UNIQUE(run_id, symbol)
);


-- ===========================================================================
-- proposals — what the strategy wants to do. NOT yet an order.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id       TEXT PRIMARY KEY,          -- uuid4
    run_id            TEXT NOT NULL REFERENCES runs(run_id),
    created_at        TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    action            TEXT NOT NULL,             -- BUY|SELL
    reason            TEXT NOT NULL,             -- E1..E9 / X1..X4 rule code
    reason_detail     TEXT,
    rank              INTEGER,
    target_weight     REAL,
    target_value      REAL,
    reference_price   REAL NOT NULL,
    quantity          REAL NOT NULL,
    estimated_value   REAL NOT NULL,
    risk_status       TEXT NOT NULL,             -- PASS|REJECT
    risk_rejections   TEXT,                      -- json array of failed checks
    ai_flag           INTEGER,                   -- copied from ai_reviews for display
    status            TEXT NOT NULL DEFAULT 'PENDING_APPROVAL',
                      -- PENDING_APPROVAL|APPROVED|REJECTED|MODIFIED|EXPIRED
    UNIQUE(run_id, symbol, action)
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status, created_at DESC);


-- ===========================================================================
-- approvals — the human decision. Append-only; never update a proposal's
-- history in place. This is the record of whether you followed your own rules.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS approvals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id       TEXT NOT NULL REFERENCES proposals(proposal_id),
    decided_at        TEXT NOT NULL,
    decision          TEXT NOT NULL,             -- APPROVE|REJECT|MODIFY
    modified_quantity REAL,                      -- set when decision='MODIFY'
    override          INTEGER NOT NULL DEFAULT 0,-- 1 if you went against the system
    override_reason   TEXT,
    decided_by        TEXT NOT NULL DEFAULT 'human'
);
CREATE INDEX IF NOT EXISTS idx_approvals_override ON approvals(override, decided_at DESC);


-- ===========================================================================
-- orders — broker lifecycle. broker_order_id is IBKR's, and IBKR is truth.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS orders (
    order_id          TEXT PRIMARY KEY,          -- our uuid4
    proposal_id       TEXT REFERENCES proposals(proposal_id),
    run_id            TEXT NOT NULL REFERENCES runs(run_id),
    broker_order_id   TEXT,                      -- IBKR permId / orderId
    submitted_at      TEXT,
    symbol            TEXT NOT NULL,
    action            TEXT NOT NULL,
    order_type        TEXT NOT NULL,
    quantity          REAL NOT NULL,
    limit_price       REAL,
    time_in_force     TEXT,
    status            TEXT NOT NULL,
        -- BUILT|VALIDATED|SUBMITTED|PARTIAL|FILLED|CANCELLED|REJECTED|EXPIRED|FAILED
    filled_quantity   REAL NOT NULL DEFAULT 0,
    avg_fill_price    REAL,
    commission        REAL,
    last_update       TEXT,
    error             TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, submitted_at DESC);


-- ===========================================================================
-- order_events — every state transition, appended. Never overwritten.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS order_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id          TEXT NOT NULL REFERENCES orders(order_id),
    occurred_at       TEXT NOT NULL,
    from_status       TEXT,
    to_status         TEXT NOT NULL,
    detail            TEXT
);


-- ===========================================================================
-- position_state — the ONE thing we must track that IBKR does not:
-- highest close since entry, for the trailing stop. Everything else about a
-- position is read live from the broker.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS position_state (
    symbol            TEXT PRIMARY KEY,
    opened_at         TEXT NOT NULL,
    entry_price       REAL NOT NULL,
    position_high     REAL NOT NULL,             -- highest CLOSE since entry
    high_updated_at   TEXT NOT NULL,
    entry_run_id      TEXT REFERENCES runs(run_id),
    closed_at         TEXT                       -- non-null once exited
);


-- ===========================================================================
-- equity_curve — one row per run, for drawdown tracking and the kill switch.
-- Values are read from IBKR, not computed here.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS equity_curve (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL REFERENCES runs(run_id),
    as_of             TEXT NOT NULL,
    net_liquidation   REAL NOT NULL,
    cash              REAL NOT NULL,
    gross_position_value REAL NOT NULL,
    peak_net_liq      REAL NOT NULL,             -- running maximum
    drawdown          REAL NOT NULL,             -- (peak - current) / peak
    n_positions       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equity_asof ON equity_curve(as_of DESC);


-- ===========================================================================
-- system_state — kill switch and health. Single row, id = 1.
-- Fail closed: if trading_enabled is 0, no orders are built. Full stop.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS system_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    trading_enabled   INTEGER NOT NULL DEFAULT 1,
    halt_reason       TEXT,
    halted_at         TEXT,
    halted_by         TEXT,
    last_run_id       TEXT,
    last_ok_run_at    TEXT,
    orders_today      INTEGER NOT NULL DEFAULT 0,
    orders_today_date TEXT,
    updated_at        TEXT NOT NULL
);


-- ===========================================================================
-- journal — your weekly notes. The 15 minutes that decides whether this works.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS journal (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    week_of           TEXT NOT NULL,
    written_at        TEXT NOT NULL,
    run_id            TEXT REFERENCES runs(run_id),
    followed_system   INTEGER,                   -- 0/1
    entry             TEXT NOT NULL,
    emotional_note    TEXT                       -- what you felt, honestly
);
CREATE INDEX IF NOT EXISTS idx_journal_week ON journal(week_of DESC);


-- ===========================================================================
-- parameter_changes — the 5-change budget from spec section 12.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS parameter_changes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at        TEXT NOT NULL,
    parameter         TEXT NOT NULL,
    old_value         TEXT NOT NULL,
    new_value         TEXT NOT NULL,
    rationale         TEXT NOT NULL,
    phase             TEXT NOT NULL              -- backtest|paper|live
);


-- ===========================================================================
-- Views for Datasette. These are what you actually look at each week.
-- ===========================================================================

DROP VIEW IF EXISTS v_pending_approval;
CREATE VIEW v_pending_approval AS
SELECT
    p.proposal_id,
    p.created_at,
    p.symbol,
    p.action,
    p.reason,
    p.rank,
    ROUND(p.quantity, 4)         AS qty,
    ROUND(p.reference_price, 2)  AS ref_price,
    ROUND(p.estimated_value, 2)  AS est_value,
    ROUND(p.target_weight, 4)    AS target_wt,
    p.risk_status,
    p.risk_rejections,
    CASE p.ai_flag WHEN 1 THEN 'FLAGGED' WHEN 0 THEN 'clear' ELSE 'unavailable' END
                                 AS ai_veto,
    p.reason_detail
FROM proposals p
WHERE p.status = 'PENDING_APPROVAL'
ORDER BY p.action DESC, p.rank ASC;


DROP VIEW IF EXISTS v_override_log;
CREATE VIEW v_override_log AS
SELECT
    a.decided_at,
    p.symbol,
    p.action,
    p.reason,
    a.decision,
    a.override,
    a.override_reason,
    ROUND(p.estimated_value, 2) AS est_value
FROM approvals a
JOIN proposals p ON p.proposal_id = a.proposal_id
WHERE a.override = 1
ORDER BY a.decided_at DESC;


DROP VIEW IF EXISTS v_run_health;
CREATE VIEW v_run_health AS
SELECT
    r.run_id,
    r.started_at,
    r.run_type,
    r.status,
    r.halt_reason,
    d.symbols_fetched || '/' || d.symbols_expected AS data_coverage,
    d.staleness_days,
    CASE d.passed WHEN 1 THEN 'PASS' ELSE 'FAIL' END AS data_gate,
    (SELECT COUNT(*) FROM proposals WHERE run_id = r.run_id) AS n_proposals,
    (SELECT COUNT(*) FROM orders    WHERE run_id = r.run_id) AS n_orders
FROM runs r
LEFT JOIN data_quality d ON d.run_id = r.run_id
ORDER BY r.started_at DESC;


DROP VIEW IF EXISTS v_current_signals;
CREATE VIEW v_current_signals AS
SELECT
    s.rank,
    s.symbol,
    s.sector,
    ROUND(s.close, 2)            AS close,
    ROUND(s.blended_momentum, 4) AS momentum,
    ROUND(s.vol_63, 4)           AS vol,
    ROUND(s.score, 4)            AS score,
    ROUND(s.atr_20, 2)           AS atr,
    CASE s.market_on WHEN 1 THEN 'RISK-ON' ELSE 'RISK-OFF' END AS regime
FROM signals s
WHERE s.run_id = (SELECT run_id FROM runs
                  WHERE run_type = 'signals' AND status = 'ok'
                  ORDER BY started_at DESC LIMIT 1)
ORDER BY s.rank ASC;
