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

-- Optional least-privilege role for the cron service:
--   GRANT USAGE ON SCHEMA raw TO ops_cron;
--   GRANT INSERT ON raw.ops_check_run TO ops_cron;
--   GRANT USAGE ON SEQUENCE raw.ops_check_run_id_seq TO ops_cron;
