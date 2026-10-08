# Own Trend Radar — Multi-TF GC Scan + Auto-Execution

Lean DRY_RUN scanner for Hyperliquid perps using DonovanWall Gaussian Channel
math (Signum Strategy v3.3 params). Supports **1h / 4h / 1d** with auto-execution
for approved entry candidates.

## GC params (locked)

| Param | Value |
|-------|-------|
| Source | hlc3 |
| Poles | 4 |
| Period | 144 |
| Mult | 1.414 |
| Reduced Lag | ON |
| Fast Response | ON |

Ported from `/workspace/signum-compat-gc/gc.ts`.

## Universe & momentum

See **[UNIVERSE.md](UNIVERSE.md)**: default ~280 liquid HL names (`dayNtlVlm ≥ $75k`, soft OI>0).
Momentum columns (`rvol` / `vol_accel` / `mom_score`) are **observe/rank only** — GC `dual_cross_up` entry math is unchanged.
Each row also gets **tier** (mcap Mega/Large/Small/Tiny) and **category** (Narrative/Cemetery/Price). `$75k` floor = HL `dayNtlVlm` (24h notional volume USD). See [UNIVERSE.md](UNIVERSE.md) / [RAILWAY_CRON.md](RAILWAY_CRON.md) (1h+4h every 15m).

## Run scan

```bash
cd /workspace/own-trend-radar

# All three timeframes (default)
python scan_gc_radar.py

# Single TF
python scan_gc_radar.py --tf 1d
python scan_gc_radar.py --tf 4h
python scan_gc_radar.py --tf 1h

# Subset / smaller universe
python scan_gc_radar.py --tf 1h,4h --max 80

# Back-compat daily-only wrapper
python scan_daily_gc.py
```

No API keys. Uses public HL `info` endpoint only. No orders.

HL `candleSnapshot` intervals used: `1h`, `4h`, `1d` (all natively supported).

### Bar / warmup windows

| TF | Bars fetched | Notes |
|----|--------------|-------|
| 1d | ~280 | existing daily window |
| 4h | ~450 | period=144 × 4h ≈ 24d minimum |
| 1h | ~550 | 500–600 bar warmup |

Concurrency default **4**; TFs run **sequentially** in one command to avoid hammering HL.

## Live-tonight UI

```bash
cd /workspace/own-trend-radar
python serve.py
# open http://127.0.0.1:8787/
```

- TF tabs: **1H | 4H | 1D** — loads `out/gc_radar_{tf}.json`
- Auto-refresh every 60s
- Optional rescan: `POST /api/rescan` runs `scan_gc_radar.py --tf all` (30 min timeout)
- Manual rescan: `python scan_gc_radar.py` then refresh the page

### Offline snapshot (no server)

Open `ui_snapshot.html` in a browser (`file://`). It inlines available TF JSON
(preferably all three; at least 1d). Missing TFs need the live server.

```bash
python scan_gc_radar.py
python build_snapshot.py
```

## Universe (Signum-scale Top 100–150)

1. **Primary:** HL `metaAndAssetCtxs` ranked by `dayNtlVlm` (24h notional) — top **150**
2. **Fallback if vols are zero:** `/workspace/hl_volume_top100.json`, then pad from meta universe names to 150
3. **Secondary only:** `own_radar_candidates_base_v0.json` may filter/reorder; never the primary cap

Same universe is shared across all TFs in one run.

## Auto-Execution System (NEW)

### Overview

The auto-execution system allows an external AI to review daily entry candidates,
approve/veto them with size and leverage parameters, and automatically execute
approved trades via Hyperliquid API. The system enforces Source of Truth (SoT)
trading rules, including:

- Min notional $10, leverage 1-5x
- SL distance >= 1.5%
- Liquidation price must be beyond Hard SL
- Total margin utilization <= 80% equity
- BTC regime-aware sizing (4% per coin when BTC 4H close < 4H Filter)

**Default mode: DRY_RUN** (logs only, no live orders)
**Live mode: requires `EXEC_DRY_RUN=0` AND `HL_API_PRIVATE_KEY` set**

### AI Integration Endpoints

#### 1. GET `/api/ai/candidates?key=<AI_DECISION_KEY>`

Fetch today's entry candidates with enhanced data for AI decision-making.

**Auth:** `X-AI-Key` header (preferred) or query parameter `key` must match `AI_DECISION_KEY` env var

**Returns:**
```json
{
  "generated_at": "2026-09-26T08:00:00Z",
  "radar_1d_asof": "2026-09-26T00:05:00Z",
  "radar_4h_asof": "2026-09-26T08:05:00Z",
  "stale": false,
  "btc": {
    "trend_1d": "Green",
    "close_vs_filter_1d": 2.5,
    "trend_4h": "Green",
    "close_vs_filter_4h": 1.8
  },
  "count": 12,
  "candidates": [
    {
      "symbol": "BTC",
      "type": "Base",
      "tier": "mega",
      "trend_1d": "Green",
      "trend_4h": "Green",
      "close_1d": 60000,
      "upper_1d": 59000,
      "filter_4h": 58500,
      "lower_4h": 57000,
      "hard_sl_dist_pct": 5.0,
      "suggested_size_pct": 6.0,
      "suggested_leverage": 2.5,
      "estimated_liq_price": 56000,
      "already_held": false
    }
  ],
  "account": {
    "equity": 10000.0,
    "margin_used": 2000.0,
    "spot_usdc_free": 8000.0
  }
}
```

#### Narrative watchlist: GET/POST `/api/ai/narrative` (same key: `X-AI-Key` header or `?key=`)
GET returns the current list. POST body: `{"mode": "merge"|"replace", "items": [{"ticker": "WORM", "sector": "agent",
"venue": "HL", "narrative": "short note"}], "remove": ["OLD"]}` (merge = upsert by ticker keeping first_seen/notes;
replace = list becomes exactly `items`; max 300). Persisted to `out/narrative_watchlist.json` with
`managed_by: "api"`, which the scanner then uses as the ONLY narrative source (next scan / live cycle).

#### 2. POST `/api/ai/decision`

Submit approval/veto decisions for entry candidates.

**Auth:** Header `X-AI-Key` or query parameter `key` must match `AI_DECISION_KEY`

**Request Body:**
```json
{
  "decisions": [
    {
      "symbol": "BTC",
      "decision": "approve",
      "size_pct": 6.0,
      "leverage": 3.0,
      "reason": "Strong 1D uptrend, low SL distance, BTC regime bullish"
    },
    {
      "symbol": "ETH",
      "decision": "veto",
      "size_pct": null,
      "leverage": null,
      "reason": "Already at max position count"
    }
  ]
}
```

**Response:**
```json
{
  "ok": true,
  "stored_count": 2,
  "file_path": "/workspace/out/decisions/decisions_20260926.json",
  "timestamp": "2026-09-26T08:30:00Z"
}
```

**Notes:**
- Size and leverage are clamped server-side to SoT bands
- Decisions are keyed by symbol + date
- Only approved candidates will be executed by the executor cron

### Executor Cron

**In-process scheduler (Railway):** The executor runs automatically via APScheduler at 08:55 HKT daily when `SCHEDULER_ENABLED=1` (default on Railway).

**Manual run:**
```bash
python3 executor.py
```

**What it does:**
- Fetches approved decisions from today
- Enforces all SoT safety checks (min notional, SL distance, liq price, margin cap)
- **Entry limit price logic**:
  - **Base/Continuation**: limit at current mid price +0.2% (for fill), capped to stay above Hard SL with >= 1.5% SL distance
  - **Add-on**: limit at 4H Filter if price is above it, else skip
- Places limit entry orders + reduce-only Hard SL trigger orders (expiring next 08:40 HKT)
- Logs all trades to trade log
- **DRY_RUN mode:** logs intended orders only
- **LIVE mode:** executes via hyperliquid-python-sdk (when `EXEC_DRY_RUN=0` and `HL_API_PRIVATE_KEY` set)

### Entry kinds & pending pullback entries (2026-09-28)

- **Base** (fresh 1D dual-cross-up): executor enters at 08:55 HKT only if the live mid is above the 1D Upper.
- **Chase** approvals never enter at 08:55. They become pending records in `out/pending_entries.json`:
  - **ADD_ON** (coin already held LONG): zone = [4H Lower, 4H Filter], 4H trend Green.
  - **CONTINUATION** (no position): zone = [1D Lower, 1D Filter], 1D trend Green.
- `pending_worker.py` runs in the 4H :10 HKT job (after the 4H scan + exits) with N / N+1 confirmation
  on the band TF (ADD_ON: 4H bars; CONTINUATION: 1D bars, acted on at the first 4H run after the 08:00
  daily close). Bar N = closed bar with low <= Filter and close > Lower. Bar N+1 = the next closed bar
  with close > Lower and close > bar N close -> enter at the live mid. Fill time re-checks: radar
  freshness, Hard SL per tier, SL distance >= 1.5%, size band (Continuation/Add-on 2-4%), leverage
  1-5x / coin max (ADD_ON keeps existing isolated leverage), 80% cumulative margin, isolated liq beyond
  Hard SL. Cancelled on any band-TF close below Lower, after 30 days, or by idempotency rules
  (CONTINUATION already held / ADD_ON base closed). Records are written only in LIVE mode.
- Visible in the cockpit (blue pending bar), `GET /api/exec/pending` (X-AI-Key), executor results
  (`pending`, `pending_active`) and the 08:45 preflight (`pending:<SYM>` lines).

### Exit Worker Cron

**In-process scheduler (Railway):** Exit workers run automatically via APScheduler when `SCHEDULER_ENABLED=1` (default on Railway):
- **Hourly :05**: 1H scan + Small/Tiny exits
- **Every 4h :05**: 4H scan + Mega/Large exits

**Manual run:**
```bash
# Hourly (Small/Tiny: 1H close < 1H Lower)
python3 exit_worker.py hourly

# 4-hourly (Mega/Large: 4H close < 4H Filter)
python3 exit_worker.py 4h

# All tiers
python3 exit_worker.py all
```

**What it does:**
- Checks open positions for primary exit signals
- Mega/Large: 4H close < 4H Filter
- Small/Tiny: 1H close < 1H Lower
- Computes MAE/MFE/R multiple and logs exits
- **DRY_RUN mode:** logs intended exits only
- **LIVE mode:** places market sell orders

### Trade Log

All entries and exits are logged to track performance:

```bash
# View trades
python3 -c "from trade_log import get_all_trades; import json; print(json.dumps(get_all_trades(), indent=2))"
```

**Storage:** JSON or SQLite (configurable via `TRADE_LOG_PATH`)

**Trade record includes:**
- Entry: type, tier, 1D/4H colors, SL distance, entry price/size/leverage, AI decision reason
- Exit: exit price, exit reason, MAE/MFE %, R multiple, PnL USD
- Dry run flag

### Public Radar Endpoint

**GET `/api/public/radar`** (no auth required)

Trimmed radar feed for public consumption (e.g., giiqquant site):
- Returns: `gc_radar_1h`, `gc_radar_4h`, `gc_radar_1d`
- No positions, no account data, no keys required
- No strategy parameters (`gc_params`) or watchlist / universe lists (`universe_requested`, `narrative_map`, ...)

### Environment Variables (NEW)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AI_DECISION_KEY` | No | `ENTRY_READ_KEY` | Auth key for AI endpoints (`/api/ai/candidates`, `/api/ai/decision`) |
| `EXEC_DRY_RUN` | No | `1` | Execution mode: `1` = DRY_RUN (logs only), `0` = LIVE (requires `HL_API_PRIVATE_KEY`) |
| `HL_API_PRIVATE_KEY` | No | - | Hyperliquid API private key for wallet `0xb74a9E2EA3e12511aDfc34a0a8327FbE4bc4e4D0` (live execution only) |
| `SCHEDULER_ENABLED` | No | `1` (Railway) | Enable APScheduler in-process cron jobs (Asia/Hong_Kong timezone) |
| `DECISIONS_DIR` | No | `out/decisions` | Directory for AI decision storage |
| `TRADE_LOG_PATH` | No | `out/trades/trades.json` | Path for trade log (`.json` or `.db`/`.sqlite` for SQLite) |
| `HL_API_WALLET_ADDRESS` | No | `0xb74a9E2E…4D0` | Expected address of `HL_API_PRIVATE_KEY`; LIVE refuses to start on mismatch |
| `EXEC_ENTRY_SLIPPAGE_PCT` | No | `0.5` | IOC entry limit = live mid × (1 + x%) (max 2) |
| `EXEC_MAX_CANDIDATE_AGE_H` | No | `3` | Executor fails closed if candidates are older / not from today (HKT) / `stale` |
| `PENDING_CONTINUATION_DISABLED` | No | disabled | CONTINUATION / ADD_ON pendings are OFF (Cove HEALTH FAIL 2026-10-05): Chase approvals = no entry, active pendings cancelled at boot. `0` re-enables. See `docs/SOT_CHANGELOG.md` |
| `OTR_SCHED_LOCK_WAIT_S` | No | `1200` | Scheduled jobs wait this long for the scan lock instead of skipping |
| `DESK_DATA_CHUNK_BYTES` | No | `32000` | Max bytes per `[DESK_DATA]` log line before splitting into `[DESK_DATA i/n]` (32 KB lines verified intact in Railway logs) |
| `OTR_LIVE_MINUTES` | No | `3-59/10` | APScheduler cron minutes (HKT) for the LIVE radar loop |
| `OTR_LIVE_LOCK_WAIT_S` | No | `240` | LIVE loop waits this long for the scan lock, else skips one cycle |
| `DESK_FULL_EVERY_S` | No | `3300` | A live cycle logs the FULL DESK_DATA set if none was logged for this long |

**Existing variables:**
- `COCKPIT_PASSWORD`: Password gate for UI and `/api/desk-data`
- `ENTRY_READ_KEY`: Read-only key for `/api/entry-candidates`
- `HL_ADDRESS`: Main wallet address (0xcFCda0F8576a268BaA17935368081F4e687dB122)

### Automatic Scheduling (Railway)

**In-process scheduler (APScheduler):** When `SCHEDULER_ENABLED=1` (default on Railway), all cron jobs run automatically in the cockpit service process (Asia/Hong_Kong timezone):

- **08:05 HKT daily**: 1D + 4H scan + generate entry candidates → `[DESK_DATA]`
- **Hourly :07**: 1H scan + Small/Tiny exits + Hard SL align → `[DESK_DATA]`
- **Every 4h :10** (00, 04, 08, 12, 16, 20 HKT): 4H scan + Mega/Large exits + Hard SL align → `[DESK_DATA]`
- **08:55 HKT daily**: Auto-executor (executes approved candidates; fails closed without fresh candidates + approvals)
- **Every 10 min (:03/:13/:23/:33/:43/:53 HKT)**: LIVE radar 1D/4H/1H (`live_radar.py`) → rebuild entry candidates
  (ENTRY tab == `/api/ai/candidates`) → `[DESK_DATA]` (compact). First run ~30 s after boot; if a TF has no candle
  cache yet it runs one closed-bar scan for it first (scan only — no exits, no orders).

**LIVE vs CLOSED (SoT):** in `gc_radar_{tf}.json` the top-level row fields (`close/filter/upper/lower/trend/
above_upper/dual_cross_up/dual_cross_down_filter/bar_time`) are the **last closed bar** and are the ONLY signal input
(candidates Base/Chase, exits, Hard SL, executor). `row.live` = forming bar (`close/filter/upper/lower/trend/
above_upper/cross_up/bar_time/forming`) — display only. Radar-level: `ts` (closed scan), `live_ts`,
`closed_bar_open_ms`, `closed_bar_close_ms`, `forming_bar_open_ms`, `next_close_ms`, `live_breadth`, `live_flags`.
The live loop costs ONE HL request (`allMids`): closed bars come from `out/candles_{tf}.json.gz` (written by every
closed-bar scan) and the forming bar is updated with the mid (close=mid, high/low extended). Limit: forming-bar
high/low are sampled every 10 min, so live GC is a close approximation until the closed-bar scan re-fetches the bar.
The closed-bar scan jobs above are unchanged and also rebuild candidates after every 1H/4H scan.

**Narrative → radar (automatic):** every closed-bar scan force-includes each narrative ticker listed on HL perps
(bypassing the dayNtlVlm/OI floor and `--max`), and every 10-min live cycle adds any still-missing ones to each TF
(`scan_symbol`, same GC/tier logic; ≤25/TF/cycle; failures e.g. short history retried after 6h) and re-tags the
Narrative category on all rows from the current list (`out/narrative_watchlist.json`, written by `POST /api/ai/narrative`).
Name mapping (`resolve_hl_name`): exact → case-insensitive → alias (`BONK→kBONK, PEPE→kPEPE, SHIB→kSHIB, FLOKI→kFLOKI,
SPX6900→SPX`, …) → `k`+symbol. Radar JSON: `narrative_map` {ticker: HL name|null}, `narrative_forced`,
`narrative_not_on_hl` (spot-only; not an error). Rows added this way carry `narrative_forced: true`.

**Cemetery-only coins:** the 1D scan also probes every other live HL perp below the floor/max and keeps rows that are
Cemetery (≥70% below ATH) or Narrative; 4H/1H scans and the live loop force-include the last 1D cemetery set
(`cemetery_forced`, `cemetery_1d`). **CAT column** (`cat_tags` string = `category_label` = DESK_DATA `cat`; `reason_tags` list; display only): short codes,
fixed order, space-separated, blank if none: `N` (narrative watchlist), `C` (cemetery: ≥70% below the 1D ATH; 4H/1H use the
1D status), `V` (passes the universe volume rule: HL `day_ntl_vlm` ≥ $75k AND
open interest > 0 when known; coins added only as N/C below the floor, and volume-pad fillers, get no V), e.g. `N C V`. `category`/`categories` (Narrative/Cemetery/Price) are kept unchanged as
machine fields; "Price" is just the default (neither N nor C) and is not shown.

**Live execution (EXEC_DRY_RUN=0 + key):** isolated margin; set leverage (≤5x and ≤ coin maxLeverage) →
IOC limit buy at live mid + slippage → reduce-only stop-market Hard SL for the filled size
(Mega/Large 4H Lower, Small/Tiny 4H Filter). If the SL cannot be placed the fill is closed immediately.
Exits: reduce-only IOC close on primary exit, then SL triggers cancelled; held positions get their SL
re-aligned (new SL placed before the old one is cancelled; never at/below liquidation).

**`[DESK_DATA]` log line:** `[DESK_DATA] {json}` on stdout with `ts`, `event`, `kind` (`full`|`live`),
`timing.{1d,4h,1h}` (HKT ISO: `closed_scan_ts`, `last_closed_bar_open`, `last_closed_bar_close`, `live_ts`,
`forming_bar_open`, `next_close`, `next_closed_scan`, `next_live_update`), `candidates_meta`, `candidates` (today's,
with tier/type/is_base/is_chase/entry_ref/SL/size/lev/liq), `entry_tab.{base,chase}` (ENTRY tab lists),
`narrative` (NARRATIVE watchlist; compact on live), `hl_open_orders` (incl. `isTrigger/triggerPx/triggerCondition/
reduceOnly/isPositionTpsl` for Hard SL checks), `hl_perp`, `hl_spot`, `gc_radar_1d/4h/1h` (rows: closed
`close/filter/upper/lower/trend/dual_cross_up/dual_cross_down` + `live_close/live_filter/live_upper/live_lower/
live_trend/live_above_upper/live_cross_up`; radar `breadth`/`flags` = closed, `live_breadth`/`live_flags` = live).
**Volume:** `full` (all rows, ~185 KB → ~6–7 lines) after every closed-bar job (≥ hourly via 1H :07) and at least every
`DESK_FULL_EVERY_S`; other 10-min live cycles log `live` (~6–10 KB, 1 line): rows only for focus symbols
(candidates, positions, open-order coins, BTC/ETH, narrative tickers on HL; `focus_only: true`, `n_total`).
If larger than `DESK_DATA_CHUNK_BYTES` it is split into self-contained `[DESK_DATA i/n] {json}` chunks sharing `ts`
(big radars are split by rows with `rows_offset`; concatenate rows in part order).

**Manual trigger:** `POST /api/jobs/run {"job": "1d"|"1h"|"4h"|"executor"}` (password-gated, 202, background).
Executor / exit workers are always forced to DRY_RUN on manual runs. `POST /api/rescan` with `tf` containing `1d`
now also rebuilds candidates.

**Scheduler status:**
- `GET /api/scheduler/status`: cockpit login, or the AI key (`AI_DECISION_KEY` / `ENTRY_READ_KEY`) via `X-AI-Key`
  header or `?key=`; otherwise 401 (persisted to `out/scheduler_status.json`, survives restarts;
  executor `fail_closed` is reported as `fail_closed`, not `error`; exit-worker failures are `error`)
- Included in `GET /api/desk-data` under `scheduler` key
- `GET /api/exec/run-report?date=YYYY-MM-DD` (AI key, read-only): that HKT day's executor / pending run report
  (executed with fill px + mid at signal + pending zone, skipped / failed with reasons). Used by `ops_cron/`.

**Ops checks (no LLM):** `ops_cron/` is one Railway cron service per schedule (exit monitor every 4h, daily
audit, desk-missing, Harbor P&L, Cove BO report, River scoreboard / veto / journal). Read-only. See
[ops_cron/README.md](ops_cron/README.md).
- Shows last run time, status, message/error for each job

**Lock guards:** All jobs use lock files / timestamps to prevent double runs if a job is still executing when the next trigger fires.

**No manual cron setup required** — scheduler starts automatically with the web server when deployed to Railway.

### Manual Cron Schedule (Alternative)

If you prefer external cron (or `SCHEDULER_ENABLED=0`), use:

```bash
# Daily scan (1D) + generate entry candidates (00:05 UTC = 08:05 HKT)
05 00 * * * python3 scan_gc_radar.py --tf 1d && python3 entry_candidates.py

# Hourly scan (1H) + Small/Tiny exits
05 * * * * python3 scan_gc_radar.py --tf 1h && python3 exit_worker.py hourly

# 4-hourly scan (4H) + Mega/Large exits (UTC hours: 00, 04, 08, 12, 16, 20)
05 0,4,8,12,16,20 * * * python3 scan_gc_radar.py --tf 4h && python3 exit_worker.py 4h

# Executor (after AI decision window, 00:55 UTC = 08:55 HKT)
55 00 * * * python3 executor.py

# Failsafe (existing, unchanged)
10 * * * * python3 failsafe_exit_worker.py
```

**AI Workflow:**
1. At ~08:30 HKT (00:30 UTC), external AI calls `GET /api/ai/candidates?key=<key>`
2. AI reviews candidates, makes decisions (approve/veto with size/leverage)
3. AI submits decisions via `POST /api/ai/decision` with body `{"decisions": [...]}`
4. At 08:55 HKT (00:55 UTC), executor cron runs and executes approved candidates
5. Hourly/4-hourly exit crons check and close positions on primary exit signals

### Testing

Run all tests:
```bash
python3 -m pytest -q test_*.py      # all suites (HL mocked; no network, no orders)
```

Keep secrets out of commits. Set `HL_API_PRIVATE_KEY` in Railway environment variables only.

## Universe (Signum-scale Top 100–150)

1. **Primary:** HL `metaAndAssetCtxs` ranked by `dayNtlVlm` (24h notional) — top **150**
2. **Fallback if vols are zero:** `/workspace/hl_volume_top100.json`, then pad from meta universe names to 150
3. **Secondary only:** `own_radar_candidates_base_v0.json` may filter/reorder; never the primary cap

Same universe is shared across all TFs in one run.

## Outputs

| File | Description |
|------|-------------|
| `out/gc_radar_1h.json` / `.csv` | 1h scan |
| `out/gc_radar_4h.json` / `.csv` | 4h scan |
| `out/gc_radar_1d.json` / `.csv` | 1d scan |
| `out/daily_gc_radar.json` / `.csv` | **alias copy of 1d** (back-compat) |
| `ui.html` | live desk UI (needs `serve.py`) |
| `ui_snapshot.html` | self-contained snapshot for `file://` |

Each JSON includes `tf`, `ts`, `breadth`, `rows` (same row schema as before).

### Per-row fields

- `symbol`, `close`, `filter`, `upper`, `lower`
- `trend` — Green if filter > filter[1], else Red
- `above_upper` — close > upper
- `dual_cross_up` — long signal: close > upper and prev_close ≤ prev_upper
- `dual_cross_down_filter` — short hedge proxy: close crosses down through filter

### Breadth

`green_count`, `red_count`, `green_pct` over successfully scanned symbols (per TF).

## Dependencies

Python 3 stdlib + `requests` if available (falls back to `urllib`).

## Railway (phone / app)

Env: `COCKPIT_PASSWORD`, `PORT=8080`, `RAILWAY=1`.

Sync from Harbor after desk write:
```bash
curl -X POST "$URL/api/sync" \
  -H "Authorization: Bearer $COCKPIT_PASSWORD" \
  -H "Content-Type: application/json" \
  -d "{"files":{"out/desk_daily.json":$(jq -c . out/desk_daily.json | jq -Rs .)}}"
```
