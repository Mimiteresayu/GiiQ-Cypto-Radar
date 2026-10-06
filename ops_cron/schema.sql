-- ops_cron run log (insert-only). Run once against the Brain database before setting BRAIN_DSN.
CREATE SCHEMA IF NOT EXISTS raw;

CREATE TABLE IF NOT EXISTS raw.ops_check_run (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT        NOT NULL UNIQUE,          -- uuid4 per run
    check_name  TEXT        NOT NULL,                 -- 'exit_monitor' | 'daily_audit'
    run_at      TIMESTAMPTZ NOT NULL,                 -- check time (UTC)
    status      TEXT        NOT NULL,                 -- 'ok' | 'problem'
    n_problems  INTEGER     NOT NULL,
    summary     TEXT        NOT NULL,                 -- one line, same text as the alert subject
    alerted     BOOLEAN     NOT NULL,                 -- an alert / daily summary was sent this run
    report      JSONB       NOT NULL,                 -- full report (same object as the stdout JSON line)
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ops_check_run_check_time ON raw.ops_check_run (check_name, run_at DESC);

-- Optional least-privilege role for the cron service (INSERT only; the cron creates tables if missing):
--   GRANT USAGE ON SCHEMA raw TO ops_cron;
--   GRANT INSERT ON ALL TABLES IN SCHEMA raw TO ops_cron;
--   GRANT USAGE ON ALL SEQUENCES IN SCHEMA raw TO ops_cron;

-- River C48-3 forward-test scoreboard. One insert per run. No updates.
CREATE TABLE IF NOT EXISTS raw.river_c48_ft_score (
    id               BIGSERIAL PRIMARY KEY,
    run_at           TIMESTAMPTZ NOT NULL,
    wallet           TEXT,
    nav              DOUBLE PRECISION,
    nav_start        DOUBLE PRECISION,
    net_pnl          DOUBLE PRECISION,
    max_drawdown_pct DOUBLE PRECISION,
    leverage         DOUBLE PRECISION,
    notional_pct     DOUBLE PRECISION,
    margin_pct       DOUBLE PRECISION,
    hard_sl_hit      BOOLEAN NOT NULL,
    status           TEXT NOT NULL,
    report           JSONB NOT NULL
);

-- River desk veto + continuation table. One insert per signal per run.
CREATE TABLE IF NOT EXISTS raw.river_desk_veto (
    id                BIGSERIAL PRIMARY KEY,
    run_at            TIMESTAMPTZ NOT NULL,
    as_of             DATE NOT NULL,
    coin              TEXT NOT NULL,
    strategy          TEXT,
    desk_decision     TEXT,
    decider           TEXT,
    ret_48h           DOUBLE PRECISION,
    ret_48h_sl_aware  DOUBLE PRECISION,
    excess_vs_btc     DOUBLE PRECISION,
    hard_sl_hit       BOOLEAN,
    outcome           TEXT,
    report            JSONB NOT NULL
);

-- River daily trade / decision journal. One insert per trade or decision. decider column is text.
CREATE TABLE IF NOT EXISTS raw.river_trade_log (
    id           BIGSERIAL PRIMARY KEY,
    run_at       TIMESTAMPTZ NOT NULL,
    trade_time   TIMESTAMPTZ,
    coin         TEXT,
    side         TEXT,
    size         DOUBLE PRECISION,
    price        DOUBLE PRECISION,
    fee          DOUBLE PRECISION,
    pnl          DOUBLE PRECISION,
    decider      TEXT,
    soft_sl      JSONB,
    hard_sl      JSONB,
    hard_sl_hit  BOOLEAN,
    report       JSONB NOT NULL
);

-- Cove BO live report. One insert per run (morning and evening).
CREATE TABLE IF NOT EXISTS raw.bo_live_report (
    id          BIGSERIAL PRIMARY KEY,
    run_at      TIMESTAMPTZ NOT NULL,
    report_date DATE NOT NULL,
    session     TEXT,
    nav         DOUBLE PRECISION,
    pnl_day     DOUBLE PRECISION,
    pnl_week    DOUBLE PRECISION,
    markdown    TEXT NOT NULL,
    report      JSONB NOT NULL
);

-- Harbor daily P&L + portfolio. One insert per run. markdown is the same text as harbor/out/pnl_YYYY-MM-DD.md.
CREATE TABLE IF NOT EXISTS raw.harbor_pnl_daily (
    id          BIGSERIAL PRIMARY KEY,
    run_at      TIMESTAMPTZ NOT NULL,
    report_date DATE NOT NULL,
    markdown    TEXT NOT NULL,
    report      JSONB NOT NULL
);
