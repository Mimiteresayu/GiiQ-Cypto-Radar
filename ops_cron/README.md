# ops_cron — deterministic exit monitor + daily live audit (Railway cron)

These two scripts replace the hourly EXIT_DESK bot check and the 09:22 BX/HL daily audit bot check
(`automation_inventory_v0` items 1 and 3). They read the same data the bot read, apply fixed rules, and alert
only when something is wrong. No LLM is involved unless an alert asks a human (or the bot) to look.

**READ-ONLY.** The scripts never place, modify or cancel orders, and never change env vars or feature flags.
They do not import any repo trading, signing or exchange-key code; the image contains only `ops_cron/`, and a
test checks that every import is standard library (plus the Postgres driver). They make these calls only:

| Source | Calls | Auth |
|---|---|---|
| cockpit | `GET /api/exit/health`, `/api/scheduler/status`, `/api/bx/status`, `/api/bx/day?date=`, `/api/exec/pending`, `/api/exec/run-report?date=` | `X-AI-Key` header = `COCKPIT_AI_KEY` (never in the URL) |
| Hyperliquid public info API | `clearinghouseState`, `frontendOpenOrders`, `userFillsByTime`, `candleSnapshot` (any other type raises) | none (public, `HL_ADDRESS` only) |
| bx-exec | through the cockpit proxy `/api/bx/*` only | the cockpit's own `BX_SERVICE_KEY` |

## Schedule

Railway cron schedules are in UTC (HKT = UTC+8). The minimum interval is 5 minutes. Each run must exit when it is
done, and Railway skips a run if the previous one is still running.

| Check | HKT | UTC cron | Railway config |
|---|---|---|---|
| Exit monitor | hourly at :12 | `12 * * * *` | `ops_cron/railway.exit-monitor.json` |
| Exit monitor daily OK summary | the 20:12 run | (same service) | `OPS_DAILY_SUMMARY_HOUR_HKT=20` |
| Daily live audit | 09:22 | `22 1 * * *` | `ops_cron/railway.daily-audit.json` |

Why :12 and 09:22: the cockpit 1H exit job runs at :07 and the 4H job at :10 (HKT). bx-exec runs hourly at :09,
every 4h at :05, and entries at 08:56. So both checks run after the jobs they check have finished.

## Alert rules

A run is **`problem`** if any rule below fires, otherwise **`ok`**. `info` items are kept in the report and never
alert. Duplicate `(code, coin)` pairs from different sources are reported once.

### Exit monitor (hourly)

| Code | Fires when | Source |
|---|---|---|
| `DATA_UNAVAILABLE` | any source errors, times out (`OPS_HTTP_TIMEOUT_S`), returns non-2xx, or `/api/bx/status` has an unexpected shape (no `breaker` / `open`) | all |
| *(passthrough)* `NO_SL`, `EXIT_NOT_DONE`, `JOB_FAILED`, `MARGIN_HIGH`, `ORPHAN_SL`, `PENDING_STALE`, `LEVERAGE_OFF`, `RADAR_STALE`, `HL_FETCH`, `BX_*` | the cockpit `/api/exit/health` reports it (rules in `exit_health.py`). `JOB_MISSED` is dropped because `JOB_STALE` below replaces it with the same 1H/4H limits. | cockpit |
| `NO_SL` | an open HL position (public `clearinghouseState`) has no trigger / reduce-only / TP-SL order on that coin (`frontendOpenOrders`). This checks HL directly, so it still works if the cockpit health check is wrong. | HL |
| `HL_ENTRY_CAP` | more than 3 HL opening orders (distinct order ids) today HKT (`MAX_NEW_ENTRIES_PER_DAY`) | HL |
| `JOB_STALE` | a cockpit job's `last_run` is older than its interval + grace: `1h_scan_exits` 75 min, `4h_scan_exits` 265 min, `pending_entries` 265 min, `1d_scan_candidates` 1470 min, `executor` 1470 min; or the job has never run | cockpit scheduler |
| `SCHEDULER_DISABLED` | cockpit `SCHEDULER_ENABLED=0` | cockpit scheduler |
| `BX_BREAKER` | the BX circuit breaker is tripped | bx status |
| `BX_BREAKER_NOT_TRIPPED` | realised BX P&L ≤ −`breaker_pct_nav`% (3%) of the baseline NAV, but the breaker is not tripped | bx status |
| `BX_MAX_OPEN` | more than `max_open` (2) live BX positions | bx status |
| `BX_ENTRY_CAP` | more than `max_new_per_day` (1) open live BX positions entered today HKT | bx status |
| `BX_NO_SL` | an open live BX position has no Hard SL in the ledger. Also passed through from bx-exec when SL repair fails. | bx status |
| `BX_EGRESS` | `BX_LIVE=1` and egress is not verified as non-US | bx status |
| `BX_LIVE_BLOCKED` | `BX_LIVE=1` but `live_ready=false` (blockers are listed) | bx status |
| `BX_JOB_ERROR` / `BX_JOB_STALE` | a bx-exec job (`1h` 75 min, `4h` 265 min, `daily` 1470 min, `entries` 1470 min) last ended in `error`, or is older than its limit | bx status |
| *(passthrough)* bx-exec `problems` (`BX_MANAGE_ERROR`, `BX_ACCOUNT`, …) | reported by the last bx-exec 1h / 4h run | bx status |

The BX limits come from `rules` in `/api/bx/status`. If that is missing, the pilot defaults apply: 2 open,
1 entry per day, −3% NAV.

Info only: `HL_CLOSE` / `BX_CLOSE` (closes in the last `OPS_LOOKBACK_MIN` = 65 min, i.e. since the last run),
`BX_JOB_UNKNOWN` (bx-exec keeps job status in memory, so after a restart a job shows as unknown until it runs
again), and `BX_DISABLED`.

**Notifications.** On `problem`, every run alerts, so a problem that lasts is repeated each hour until it is fixed.
On `ok`, nothing is sent, except the run in hour `OPS_DAILY_SUMMARY_HOUR_HKT` (20 → 20:12 HKT), which sends one
line, for example `OK · HL 1 倉 · BX 1 倉 · BX_LIVE=1 · closes 2 · 20:12 HKT 04-Oct`.

### Daily live audit (09:22 HKT, window = last `OPS_AUDIT_WINDOW_H` = 24 h)

| Code | Fires when |
|---|---|
| `DATA_UNAVAILABLE` | any source fails (as above). A 404 from `/api/bx/day` means there is no 08:56 report today; that is `BX_DAY_MISSING` (info), not a problem. |
| `BX_EGRESS` | egress is not `ok`, or the geo countries are not all `OPS_BX_EXPECTED_COUNTRY` (SG), or the region does not start with `OPS_BX_EXPECTED_REGION_PREFIX` (`asia-southeast1`) |
| `BX_LIVE_OFF` | `OPS_EXPECT_BX_LIVE=1` and `BX_LIVE=0` (unset = the flag is reported but not judged) |
| `BX_BREAKER`, `BX_BREAKER_NOT_TRIPPED`, `BX_MAX_OPEN`, `BX_ENTRY_CAP`, `BX_NO_SL`, `BX_LIVE_BLOCKED` | same rules as the exit monitor |
| `RULE_VIOLATION` | a live fill in the window breaks a rule. **HL** (from the executor / pending run report): Hard SL missing or < 1.5% below the fill; leverage outside 1–5x; Base fill not above the 1D Upper; pending (ADD_ON / CONTINUATION) fill at or below the pending zone Lower. **BX** (from the 08:56 day report and open positions): Hard SL order not confirmed; Hard SL < 1.5% below the fill. |
| `SLIPPAGE_HIGH` | an HL fill is more than `OPS_SLIPPAGE_MAX_BP` (60 bp) above the mid at signal (the executor's IOC limit is mid + 50 bp by default) |

The trade review is always included in the report (it does not alert):

- **Orders (24 h):** HL fills grouped by order id (public `userFillsByTime`), plus failed HL orders from the run
  report; BX orders from today's `/api/bx/day` and open positions entered in the window.
- **Realised P&L (24 h):** HL = sum of `closedPnl − fee` over fills in the window; BX = sum of `pnl_usd` over
  `closed_recent` exits in the window, plus the cumulative `realized_pnl_usd`.
- **Rule compliance and slippage per fill:** fill vs mid at signal (bp), SL distance, and where the fill sits
  against the pending zone (`in_zone` / `above_filter +x%` / `below_lower`).
- **Missed entries:** pendings that expired or were cancelled in the window (with reason), and executor /
  pending / BX skips caused by the 1.5% minimum SL distance check.
- **Exit efficiency (HL):** for each round trip closed in the window (flat → open → flat), MFE = highest 1h
  high between entry and exit, and `efficiency = (exit − entry) / (MFE high − entry)`. 1.0 = sold at the top,
  0 = gave back the whole move, < 0 = exited below entry.

By default the audit alerts only on `problem`. Set `OPS_AUDIT_SEND_OK=1` to also send the summary every day.

## Output and persistence

- **stdout:** exactly one JSON line per run (the full report: `check`, `run_id`, `run_at`, `status`, `summary`,
  `problems`, `info`, check-specific sections, `sources`, `alert`, `persisted`).
- **stderr:** the markdown summary, plus one `[OPS_ALERT] {"subject": …, "text": …}` line whenever an alert is
  due, even when no sink is configured.
- **Postgres (optional):** when `BRAIN_DSN` is set, one `INSERT` into `raw.ops_check_run` per run. The script never
  runs DDL, UPDATE or DELETE. Create the table once with [`schema.sql`](schema.sql):

| Column | Type | Meaning |
|---|---|---|
| `id` | `bigserial` PK | |
| `run_id` | `text` unique | uuid4 of the run |
| `check_name` | `text` | `exit_monitor` / `daily_audit` |
| `run_at` | `timestamptz` | check time (UTC) |
| `status` | `text` | `ok` / `problem` |
| `n_problems` | `int` | |
| `summary` | `text` | one line (also used in the alert subject) |
| `alerted` | `bool` | a webhook / email was sent this run |
| `report` | `jsonb` | full report (same as the stdout line) |
| `inserted_at` | `timestamptz` | default `now()` |

If the insert fails, the error goes into `persisted` and stderr; the DSN is never printed. The run still exits 0.

## Environment variables

All configuration comes from env vars; nothing secret is in the code. Set them on the two Railway cron services
only.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `COCKPIT_URL` | yes | — | cockpit base URL, e.g. `https://<cockpit domain>` |
| `COCKPIT_AI_KEY` | yes | — | the cockpit `AI_DECISION_KEY` (sent as the `X-AI-Key` header) |
| `HL_ADDRESS` | yes | — | HL main wallet address (public; used for the public info API) |
| `HL_INFO_URL` | no | `https://api.hyperliquid.xyz/info` | HL public info endpoint |
| `OPS_HTTP_TIMEOUT_S` | no | `20` | per-request timeout; a timeout is `DATA_UNAVAILABLE` |
| `OPS_BX_ENABLED` | no | `1` | `0` skips every BX source and rule |
| `OPS_LOOKBACK_MIN` | no | `65` | exit monitor "new closes since last run" window |
| `OPS_DAILY_SUMMARY_HOUR_HKT` | no | `20` | hour (HKT) whose exit-monitor run sends the daily OK line |
| `OPS_AUDIT_WINDOW_H` | no | `24` | audit look-back window |
| `OPS_SLIPPAGE_MAX_BP` | no | `60` | `SLIPPAGE_HIGH` threshold |
| `OPS_EXPECT_BX_LIVE` | no | unset | `1`: `BX_LIVE=0` is a problem in the audit; `0`/unset: not judged |
| `OPS_BX_EXPECTED_COUNTRY` | no | `SG` | egress country the audit expects |
| `OPS_BX_EXPECTED_REGION_PREFIX` | no | `asia-southeast1` | bx-exec Railway region prefix the audit expects |
| `OPS_AUDIT_SEND_OK` | no | `0` | `1`: send the audit summary even when OK |
| `ALERT_WEBHOOK_URL` | no | — | POST `{"text": "..."}` (same payload as `failsafe_exit_worker` / `BX_ALERT_WEBHOOK`; Slack-compatible) |
| `SMTP_HOST` | no | — | email sink (with `ALERT_EMAIL_TO`) |
| `SMTP_PORT` | no | `587` (`465` if `SMTP_SSL=1`) | |
| `SMTP_USER` / `SMTP_PASSWORD` | no | — | SMTP login (skipped if `SMTP_USER` is empty) |
| `SMTP_SSL` | no | `0` | `1` = implicit TLS (port 465); otherwise STARTTLS |
| `SMTP_STARTTLS` | no | `1` | `0` disables STARTTLS (plain SMTP; not recommended) |
| `ALERT_EMAIL_FROM` | no | `SMTP_USER` | sender address |
| `ALERT_EMAIL_TO` | no | — | comma-separated recipients |
| `BRAIN_DSN` | no | — | Postgres DSN; when set, one insert-only row per run into `raw.ops_check_run` |

**Alert sinks.** The repo's only existing alert mechanism is a JSON webhook (`ALERT_WEBHOOK_URL` in
`failsafe_exit_worker.py`, `BX_ALERT_WEBHOOK` in bx-exec, payload `{"text": ...}`). Email was sent by the AI
routine itself. So ops_cron reuses the webhook payload and adds SMTP for email. With neither set, alerts are only
`[OPS_ALERT]` log lines in Railway. Railway allows outbound SMTP only on Pro plans; on other plans, use the webhook
(for example a Slack incoming webhook or an email-relay webhook).

## Run locally

```bash
# offline, from fixtures (no network, nothing sent)
python -m ops_cron exit-monitor --dry-run --fixture ops_cron/tests/fixtures/exit_ok.json
python -m ops_cron daily-audit  --dry-run --fixture ops_cron/tests/fixtures/audit_ok.json

# live reads, nothing sent / written (needs COCKPIT_URL, COCKPIT_AI_KEY, HL_ADDRESS)
python -m ops_cron exit-monitor --dry-run
python -m ops_cron daily-audit  --dry-run --now 2026-10-04T01:22:00Z

# tests
python -m pytest -q ops_cron            # or: python -m unittest discover -s ops_cron/tests -t .
```

`--dry-run` prints the markdown report and the pretty JSON report. It sends no alert and writes no row.
Exit code: `0` for `ok` and `problem`. `1` only if the script itself crashed; it then tries to send an
`OPS_CRASH` alert.

## Deploy as a Railway cron service

Do this twice, once per check, in the same project as the cockpit:

1. **New service → GitHub repo** (this repo, branch `main`). Name it `ops-exit-monitor` (or `ops-daily-audit`).
   Any region works: it only calls the cockpit and the public HL API, never Bitunix.
2. **Settings → Config-as-code → Railway config file path:** `/ops_cron/railway.exit-monitor.json`
   (or `/ops_cron/railway.daily-audit.json`). This sets the Dockerfile builder (`ops_cron/Dockerfile`), the start
   command, the cron schedule (`12 * * * *` / `22 1 * * *`), `restartPolicyType: NEVER` and
   `watchPatterns: ["ops_cron/**"]`. Do not attach a volume, public domain or healthcheck.
3. **Variables:** `COCKPIT_URL`, `COCKPIT_AI_KEY`, `HL_ADDRESS`, plus at least one sink (`ALERT_WEBHOOK_URL`, or
   `SMTP_HOST` + `SMTP_USER` + `SMTP_PASSWORD` + `ALERT_EMAIL_TO`) and optionally `BRAIN_DSN`.
   To reference the cockpit key without copying it: `COCKPIT_AI_KEY=${{cockpit.AI_DECISION_KEY}}`.
4. If `BRAIN_DSN` is set: run `ops_cron/schema.sql` once against that database (it can also create an
   insert-only role).
5. Deploy. Check the first run in the service's logs: one JSON line with `"status":"ok"` (or the problems).
   Use **Run now** in Railway (or `python -m ops_cron exit-monitor --dry-run` locally with the same vars) to
   test right away.
6. When both services have run cleanly for a day, turn off the two bot routines (EXIT_DESK hourly, 09:22 audit).

## Known gaps

- **BX slippage and BX exit efficiency are not computed.** bx-exec does not expose the mid at signal, the entry
  for closed trades, or BX candles through a read endpoint. ops_cron does not call Bitunix directly, so these
  fields stay empty. HL has both.
- **The BX daily entry cap counts open positions only.** A BX position opened and closed on the same day is not
  counted, because `closed_recent` has no entry time.
- **BX job freshness resets when bx-exec restarts** (job status is in memory). Until each job runs again it is
  `BX_JOB_UNKNOWN` (info), not a problem.
- **Repeat alerts.** Runs keep no state, so a problem that persists is alerted every hour.
- **Key scope.** `COCKPIT_AI_KEY` is the cockpit `AI_DECISION_KEY`, which also authorises `POST /api/ai/decision`
  and `POST /api/exec/run`. ops_cron only sends GETs (enforced in code and tested), but a separate read-only key
  on the cockpit would be safer.
