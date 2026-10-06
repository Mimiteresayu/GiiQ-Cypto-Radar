# ops_cron — read-only Railway cron checks

Pure Python. No LLM calls, no order placement, no cancels, no changes to exec / entry / exit / sizing.
One package, one entrypoint, one Railway cron service per schedule.

```bash
python -m ops_cron.main <job> [--dry-run] [--fixture FILE] [--now ISO]
```

`python -m ops_cron <job>` is the same entrypoint.

## Jobs

Railway cron is UTC. HKT = UTC+8. The four-hour exit monitor uses the same clock hours in both zones
(`17 0,4,8,12,16,20 * * *` is 00:17 / 04:17 / 08:17 / 12:17 / 16:17 / 20:17 HKT).

| Job | Service | HKT | UTC cron | Always sends | Railway config |
|---|---|---|---|---|---|
| `exit-monitor` | `ops-exit-monitor` | every 4h at :17 | `17 0,4,8,12,16,20 * * *` | no — alert only on problems | `railway.exit-monitor.json` |
| `desk-missing` | `ops-desk-missing` | 08:30 | `30 0 * * *` | no | `railway.desk-missing.json` |
| `c48-scoreboard` | `ops-c48-scoreboard` | every 3h UTC (last slot on 10/7 is 20:00 HKT) | `0 */3 * * *` | no; silent at and after 2026-10-07 21:00 HKT | `railway.c48-scoreboard.json` |
| `c48-scoreboard` | `ops-c48-scoreboard-final` | 2026-10-07 20:40 (before the 20:44 cutoff) | `40 12 7 10 *` | same job, one extra run | `railway.c48-scoreboard-final.json` |
| `desk-veto` | `ops-desk-veto` | 09:05 | `5 1 * * *` | no | `railway.desk-veto.json` |
| `harbor-pnl` | `ops-harbor-pnl` | 09:15 | `15 1 * * *` | yes, one message | `railway.harbor-pnl.json` |
| `trade-journal` | `ops-trade-journal` | 09:15 | `15 1 * * *` | no — alert on failure or a Hard SL hit | `railway.trade-journal.json` |
| `daily-audit` | `ops-daily-audit` | 09:22 | `22 1 * * *` | yes, one message | `railway.daily-audit.json` |
| `bo-report` | `ops-bo-report` | weekdays 09:32 and 20:32 | `32 1,12 * * 1-5` | yes, one message | `railway.bo-report.json` |

Harbor and the trade journal share a clock time and stay two services (different start commands).

Exit monitor reads cockpit `GET /api/exit/health`, `/api/scheduler/status`, `/api/bx/status`, `/api/exec/pending`,
plus the public Hyperliquid info API. It alerts on a missing Hard SL on an open HL position, a stale scheduler
job, a tripped BX breaker, and source errors. A healthy run sends nothing.

Daily audit (09:22, after the 08:55 executor) always sends one summary: today's run report, stored decisions,
fills, open positions with both stops, and BX status. Problems are inside that message.

Desk-missing (08:30) alerts only when today's cockpit decisions have no non-fallback POST. The alert text is
exactly: `no Claude desk POST yet; Railway 08:50 fallback will apply (HL Base 2% + Hard SL, Chase veto)`.
`source=fallback` alone is not a desk POST. A missing or unreadable `decisions` field is `DATA_UNAVAILABLE`,
not that sentence. This job does not change the 08:50 fallback.

Harbor (09:15) is the daily P&L and portfolio note. Monday (HKT weekday 0) adds the latest infra-fee line
against trading P&L. It also flags a missing Hard SL or a liquidation price closer than the Hard SL.
Cove BO (weekdays 09:32 and 20:32) is the live book: NAV, day and week P&L, weekly cost, positions, actors,
anomalies. Session is morning when the HKT hour is before 12, otherwise evening.

Harbor and Cove share `ops_cron.stops.sl_distance`: the soft exit is the 1H Lower and the Hard SL is the
4H Filter (Small/Tiny pair). Distance is from mark; `pnl_if_hit` is the loss if that level trades.
Margin cap is 70% of NAV. Weekly cost shown on the BO report is $91. Neither number is read from sizing code.

River's three jobs write insert-only rows to giiq-brain:

- **C48-3 FT scoreboard** reads the HL **testnet** public info API (`TESTNET_WALLET_ADDRESS` only).
  Checks: margin ≤ 2% NAV, notional ≤ 8% NAV, leverage ≤ 3x, drawdown ≤ 4% from `C48_NAV_START`.
  BTC only. The every-3h cron's last slot on 2026-10-07 is 20:00 HKT, which is before the 20:44 cutoff,
  so a second service runs once at 20:40 HKT (`40 12 7 10 *`). At and after 21:00 HKT the job exits ok,
  sends nothing, and inserts no score row.
- **Desk veto** (09:05) scores the **D-2** cockpit decision batch (run-report two HKT dates ago), not today's
  radar crosses. `ret_48h` is the move in the 48 hours **after** that signal (did it continue). Candles are
  HL 1h, else Bitunix public `{COIN}USDT`. If a 1h low in that forward window trades through the 4H Filter
  as of the signal, `ret_48h_sl_aware` uses that stop. BTC is the benchmark over the same forward window.
  Strategy comes from the decision `type` (BASE / CHASE). Cap 25.
- **Trade journal** (09:15) loads fills from the last successful journal run (`raw.ops_check_run` run_at,
  else `raw.river_trade_log` trade_time). The first run looks back 26 hours, so a close after yesterday's
  09:15 is not dropped. One row per fill in that window, plus decision symbols with no fill.
  Long size is positive and short size is negative. The decider column is `unknown` until cockpit stores
  an actor. `source` is not treated as an actor.

### Decider

Cockpit does not record who posted. The column is `unknown`. An explicit `actor` or `decider` value is
copied only when it is already `Claude.ai`, `Forge`, `Railway 08:50 fallback`, or `unknown`.
`source=claude`, `source=forge`, and `source=fallback` are not an actor. Recording the actor in cockpit
is a follow-up.

Missing numbers are written `未知` or `未核實`. A previous run's number is never reused.
BX NAV is `未知` (owner Harbor): there is no read-only Bitunix key and no BX NAV table.
`available_usdt` is not NAV. Prop is owner Helm. Stocks and cash are owner MMT.
Percents use known sleeves only. All-time realised P&L is `未核實` when `userFills` returns 2000 rows.
Infra fee comes from the latest `river/admin/ai_infra_usage_review_*.md` (`RIVER_ADMIN_DIR`); if that
file is missing the fee is `未知` (owner River). Months are not summed.

GC periods match the live scanner with Lag/Fast off: 1H 48, 4H 72, 1D 144 (`ops_cron/gc_levels.py`).
The last closed bar is the one whose close time is ≤ now. Allowed HL info types:
`clearinghouseState`, `frontendOpenOrders`, `userFills`, `userFillsByTime`, `userFunding`,
`candleSnapshot`, `allMids`. Any other type raises.

## Alert sink

1. Telegram when both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set.
2. Email only when the Telegram vars are **absent** and `SMTP_HOST` + `ALERT_EMAIL_TO` are set.
   A failed Telegram send does not fall through to email.
3. If neither is set, the message is a log line only (`[OPS_ALERT]` on stderr).

The Telegram URL contains the bot token and is never logged. Secrets are scrubbed from error text.
Exit code is 0 for `ok` and `problem`. Exit code 1 only when the script crashes (it still tries to alert).

## Brain tables

Postgres via `BRAIN_DATABASE_URL` (older name `BRAIN_DSN` still works). Insert-only: no UPDATE, no DELETE.
`CREATE TABLE IF NOT EXISTS` runs from [`schema.sql`](schema.sql) before the run row is inserted.
Proposed names (schema `raw`):

| Table | Written by |
|---|---|
| `raw.ops_check_run` | every job, one row per run |
| `raw.harbor_pnl_daily` | `harbor-pnl` (markdown matches the file below) |
| `raw.bo_live_report` | `bo-report` |
| `raw.river_c48_ft_score` | `c48-scoreboard` (none after the cutoff) |
| `raw.river_desk_veto` | `desk-veto`, one row per signal |
| `raw.river_trade_log` | `trade-journal`, one row per fill or decision (`decider` text column) |

Harbor also writes `harbor/out/pnl_YYYY-MM-DD.md` (`HARBOR_OUT_DIR`, default `harbor/out`).
That directory is gitignored. `--dry-run` writes neither the file nor the database.

## Environment variables

Nothing secret is in the repo. Cockpit auth is the `X-AI-Key` header from `COCKPIT_AI_KEY`.
The key is never placed in a URL. `AI_DECISION_KEY` is not set on the cockpit service; the live
read key is `ENTRY_READ_KEY`. Reference it, do not paste a value:

```
COCKPIT_AI_KEY=${{cockpit.ENTRY_READ_KEY}}
```

HL calls use wallet **address** env vars only (no private keys).

### Every service

`HL_ADDRESS` is required on every cron service, including desk-missing and the C48 scoreboard. It is the
public master wallet address (not a secret, not a private key). If it is unset the job alerts
`HL_ADDRESS not set` and writes `unknown`. It does not skip quietly.

| Variable | Required | Meaning |
|---|---|---|
| `HL_ADDRESS` | yes | public master wallet address |
| `TELEGRAM_BOT_TOKEN` | one sink | Telegram bot token |
| `TELEGRAM_CHAT_ID` | with the token | chat that receives alerts and reports |
| `SMTP_HOST`, `ALERT_EMAIL_TO` | email fallback | used only when both Telegram vars are unset |
| `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_SSL`, `SMTP_STARTTLS`, `ALERT_EMAIL_FROM` | no | email details (port 587, STARTTLS on, unless `SMTP_SSL=1`) |
| `BRAIN_DATABASE_URL` | no | giiq-brain DSN. Tables are created if missing |
| `OPS_HTTP_TIMEOUT_S` | no | per-request timeout, default 20s. A timeout is `DATA_UNAVAILABLE` |

### `ops-exit-monitor`, `ops-daily-audit`, `ops-desk-missing`, `ops-harbor-pnl`, `ops-bo-report`, `ops-desk-veto`, `ops-trade-journal`

| Variable | Required | Meaning |
|---|---|---|
| `COCKPIT_URL` | yes | cockpit base URL |
| `COCKPIT_AI_KEY` | yes | `${{cockpit.ENTRY_READ_KEY}}` |
| `HL_ADDRESS` | yes (also listed under every service) | public master wallet address |
| `HL_INFO_URL` | no | default `https://api.hyperliquid.xyz/info` |

Desk-missing calls cockpit only, but it still requires `HL_ADDRESS` so a missing address alerts instead of a silent ok.

### Extra

| Service | Variable | Meaning |
|---|---|---|
| exit-monitor, daily-audit | `OPS_BX_ENABLED` | `0` skips BX reads and rules (default on) |
| exit-monitor | `OPS_LOOKBACK_MIN` | close look-back, default 65 |
| daily-audit | `OPS_AUDIT_WINDOW_H` | default 24 |
| daily-audit | `OPS_SLIPPAGE_MAX_BP` | default 60 |
| daily-audit | `OPS_EXPECT_BX_LIVE` | `1` makes `BX_LIVE=0` a problem |
| daily-audit | `OPS_BX_EXPECTED_COUNTRY` | default `SG` |
| daily-audit | `OPS_BX_EXPECTED_REGION_PREFIX` | default `asia-southeast1` |
| harbor-pnl | `HARBOR_OUT_DIR` | default `harbor/out` |
| harbor-pnl | `RIVER_ADMIN_DIR` | default `river/admin` (latest infra review file) |
| desk-veto | `BX_PUBLIC_URL` | Bitunix public kline host, default `https://fapi.bitunix.com` (GET, no key) |
| c48-scoreboard | `TESTNET_WALLET_ADDRESS` | HL testnet address. Not a private key |
| c48-scoreboard | `HL_TESTNET_INFO_URL` | default `https://api.hyperliquid-testnet.xyz/info` |
| c48-scoreboard | `C48_NAV_START` | NAV baseline for the 4% drawdown check. Unset → drawdown `未知` and a problem |

C48 does not need `COCKPIT_URL` and does not call mainnet. It still requires `HL_ADDRESS` (the public master address) in addition to `TESTNET_WALLET_ADDRESS`.

Spot USDC is `spotClearinghouseState` balances where `coin` is USDC (`total`). Perp `accountValue` is printed
separately and is not used as cash. A failed spot or perp read is the word `unknown`, never `0`.

A Hard SL is a trigger/stop, reduce-only or `isPositionTpsl`, on the same coin, opposite the position,
with the trigger on the losing side of the mark (long: trigger < mark, short: trigger > mark) and size
covering the position. Distance from the mark is printed in percent. A take-profit alone is `Hard SL: NO`.
Exit monitor, daily audit, Harbor, and the BO report share `hlparse.hard_sl_status`.

## What each job reads

| Source | Calls | Auth |
|---|---|---|
| cockpit | `GET /api/exit/health`, `/api/scheduler/status`, `/api/bx/status`, `/api/bx/day?date=`, `/api/exec/pending`, `/api/exec/run-report?date=` | `X-AI-Key: COCKPIT_AI_KEY` |
| cockpit | `GET /api/public/radar` | none |
| Hyperliquid public info | the types above, plus `spotClearinghouseState` for spot USDC | none (`HL_ADDRESS` or `TESTNET_WALLET_ADDRESS`) |
| Bitunix public | `GET /api/v1/futures/market/kline` | none |

`/api/exec/run-report` includes a `decisions` object for that HKT date (posted, count, records, history).
No file path and no key in the body.

## Exit-monitor and daily-audit rules

A run is `problem` when `problems` is non-empty. Duplicate `(code, coin)` pairs are reported once.

### Exit monitor

| Code | Fires when |
|---|---|
| `DATA_UNAVAILABLE` | a source errors, times out, returns non-2xx, `/api/bx/status` has no `breaker` / `open`, or `/api/exec/pending` fails |
| `NO_SL` and other exit-health codes | cockpit `/api/exit/health` reports them (`JOB_MISSED` is replaced by `JOB_STALE`) |
| `NO_SL` | an open HL position has no trigger / reduce-only / TP-SL order (`frontendOpenOrders`) |
| `HL_ENTRY_CAP` | more than 3 distinct HL opening order ids today HKT |
| `JOB_STALE` | `1h_scan_exits` > 75 min, `4h_scan_exits` or `pending_entries` > 265 min, `1d_scan_candidates` or `executor` > 1470 min, or never run |
| `SCHEDULER_DISABLED` | `SCHEDULER_ENABLED=0` |
| `BX_BREAKER` | BX circuit breaker tripped |
| `BX_BREAKER_NOT_TRIPPED` | realised BX P&L ≤ −3% of baseline NAV and the breaker is not tripped |
| `BX_MAX_OPEN` / `BX_ENTRY_CAP` / `BX_NO_SL` | more than `max_open` (2), more than `max_new_per_day` (1), or an open live BX position with no Hard SL |
| `BX_EGRESS` / `BX_LIVE_BLOCKED` | `BX_LIVE=1` and egress or `live_ready` is not ok |
| `BX_JOB_ERROR` / `BX_JOB_STALE` | bx-exec job last ended in error, or is older than 1h 75 / 4h 265 / daily 1470 / entries 1470 min |

Pending continuation being disabled is recorded, not a problem. Info only: recent closes, `BX_JOB_UNKNOWN`, `BX_DISABLED`.

### Daily audit

Same BX rules, plus `RULE_VIOLATION` (Hard SL missing or < 1.5% below the fill, leverage outside 1–5x,
Base fill not above the 1D Upper, pending fill at or below the zone Lower, BX Hard SL not confirmed)
and `SLIPPAGE_HIGH` (HL fill more than 60 bp above the mid at signal). A 404 from `/api/bx/day` is
`BX_DAY_MISSING` (info). The summary is sent every day.

## Run locally

```bash
python -m ops_cron.main exit-monitor --dry-run --fixture ops_cron/tests/fixtures/exit_ok.json
python -m ops_cron.main daily-audit  --dry-run --fixture ops_cron/tests/fixtures/audit_ok.json
python -m pytest -q ops_cron
```

## Deploy

Create **eight** Railway cron services in the same project as cockpit. Do not change cockpit, bx-exec,
or chainstack-grid config except the cockpit deploy that already carries `GET /api/exec/run-report`.

For each service:

1. New service from this repo, branch `main`, after this PR is merged. Any region: these jobs never call Bitunix with a key.
2. Config-as-code path: `/ops_cron/railway.<job>.json`. That sets `ops_cron/Dockerfile`, the start command
   `python -m ops_cron.main <job>`, the UTC cron string, `restartPolicyType: NEVER`, and `watchPatterns: ["ops_cron/**"]`.
   No volume, public domain, or healthcheck.
3. Variables from the tables above. Cockpit key reference:

   `COCKPIT_AI_KEY=${{cockpit.ENTRY_READ_KEY}}`

4. Redeploy cockpit so the `decisions` field on run-report is live before the first 08:30 / 09:22 run.
5. Optional: set `BRAIN_DATABASE_URL`. The cron creates the tables. Running `ops_cron/schema.sql` once is enough
   if you want them before the first insert.

## Known gaps

- The decider column is `unknown`. Cockpit `source` is not an actor. Storing the actor is a follow-up.
- BX NAV is `未知`. Public Bitunix klines are used only as a candle fallback on the veto job.
- `userFills` is capped at 2000. All-time realised P&L is then `未核實`.
- BX slippage and BX exit efficiency are not computed. The daily BX entry cap counts open positions only.
- BX job freshness resets when bx-exec restarts (`BX_JOB_UNKNOWN` until the job runs again).
- A problem that persists is alerted on every exit-monitor run. Runs keep no alert state.
- There is no separate `OPS_READ_KEY`. `COCKPIT_AI_KEY` is the cockpit read key, sent only as a header on GETs.
