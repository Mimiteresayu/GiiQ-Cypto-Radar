# Railway self-refresh (no Harbor / Grok Bot credit)

Cockpit runs **one web service** (`cockpit`) that serves the UI and a background
scheduler. Scans write `out/gc_radar_{1d,4h,1h}.json` on the container filesystem.

## Volume (required)

Mount a persistent volume at **`/app/out`** on the `cockpit` service.

Without it, every redeploy wipes radar JSON. Harbor `/api/sync` also writes under
`/app/out` — same volume keeps desk/narrative and radar together.

Railway UI: Project → cockpit → Settings → Volumes → Add volume → Mount path `/app/out`.

Or via Railway MCP: `create-volume` with `mountPath=/app/out` and `serviceId=cockpit`.

## Schedules (UTC; HKT = UTC+8)

| TF | When (UTC) | HKT | Notes |
|----|------------|-----|-------|
| **1H + 4H** | every **15 minutes** (`:00/:15/:30/:45`) | +8h | Exit radar — replaces old hourly / 4h-bar schedules |
| **1D** | daily **00:05** | 08:05 | Daily bars; not on the 15m tick |

Logic lives in `serve.py` (`_scheduler_loop`). Enabled when `RAILWAY=1` or
`OTR_SCHEDULER=1`.

Env knobs:

- `OTR_SCHEDULER=1|0` — force on/off
- `OTR_SCAN_INTERVAL_MIN` — default **15** (1h+4h cadence)
- `OTR_SCAN_MAX` — default 280 (4h / 1d)
- `OTR_SCAN_MAX_1H` — default 200 (lighter on 15m ticks)
- `OTR_SCAN_CONCURRENCY` — default **2** (keep modest to avoid HL 429s)

On boot, if any `gc_radar_*.json` is missing, one background scan runs after ~15s.

**Why not all three every 15m?** 1d candles do not need sub-hour refresh; scanning
1h+4h @15m already covers exit monitoring. 1d stays daily @00:05 UTC.

## Image contents

Dockerfile bakes:

- `serve.py`, `ui.html`, `scan_gc_radar.py`, `mcap_tiers.py`
- `data/own_radar_candidates_base_v0.json` (optional secondary filter)
- `data/hl_volume_top100.json` (fallback if live dayNtlVlm is empty)
- `data/mcap_cache.json` (circulating mcap USD → Mega/Large/Small/Tiny)
- `data/narrative_watchlist.json` + `narrative/watchlist.json` (Narrative category)

Universe primary source remains live Hyperliquid `metaAndAssetCtxs` (stdlib urllib).

Row fields added each scan: `tier`, `mcap_usd`, `mcap_known`, `category`, `categories`.

## Harbor `/api/sync`

Still supported for desk_daily / narrative / optional radar override.
**Merge-only:** only paths present in the POST body are written; omitted
`gc_radar_*.json` files are **not** deleted. Scheduler and Harbor can coexist.

## Manual rescan

`POST /api/rescan` (password / Bearer same as UI):

```json
{"tf": "1d", "max": 280}
```

Returns 409 if a scan is already running.

## Fail-safe exit worker (DORMANT build; not enabled)

`failsafe_exit_worker.py` — deterministic, no-LLM exit worker that can run on Railway
inside the existing scheduler, so open positions stay managed when the LLM operator is offline.

**LOCKED rules** (from SIZE_TIER_EXIT_LOCKED.md / HARBOR_AUTOTRADE_PROMPT_v1.md):
- GC periods: 1D=144, 4H=72, 1H=48, Lag/Fast off, closed bars only
- Tiers by mcap: Mega/Large → 4H Filter cross-down; Small/Tiny → 1H Lower cross-down
- Hard SL: Mega/Large → 4H Lower; Small/Tiny → 4H Filter (mid); re-aligned each run
- Shorts: report-only (no exit logic locked yet)
- **Never opens positions** (reduce-only exits + SL orders only)

**Mode:**
- `FAILSAFE_ENABLE=1` + `HL_API_WALLET_KEY` present → **LIVE** (places orders via hyperliquid-python-sdk)
- Otherwise → **DRY_RUN** (writes `out/failsafe_last.json` only, no orders)

**Staleness guard:** If 1h/4h radar or candles can't be fetched or are >2h old, worker
does nothing destructive and logs error.

**Retry/backoff:** Retries HL API calls on 429 with exponential backoff (2s/4s/8s).

**Alert webhook:** If `ALERT_WEBHOOK_URL` is set, POSTs short text on any live action or error:
```json
{"text": "[failsafe] EXIT BTC EXIT_LONG_4H_FILTER_CROSS size=0.05"}
```

**Output:** Every run writes `out/failsafe_last.json` with:
```json
{
  "ts": "2026-09-25T14:52:00+00:00",
  "mode": "DRY_RUN",
  "errors": [],
  "actions": [{"action": "market_close", "coin": "BTC", "size": 0.05, "signal": "EXIT_LONG_4H_FILTER_CROSS"}],
  "positions": [...],
  "status": "ok"
}
```

**Schedule:** Intended to run hourly a few minutes after the hour close (e.g. `:05` UTC).
Not auto-enabled — integration into `serve.py` scheduler is a manual step if needed.

**Env vars to go live:**
- `FAILSAFE_ENABLE=1` — master enable switch
- `HL_ADDRESS` — wallet address (read positions)
- `HL_API_WALLET_KEY` — API wallet key (agent wallet, **not** the master key; place orders)
- `ALERT_WEBHOOK_URL` (optional) — webhook for live action/error alerts

**Tests:** Run `python test_failsafe_exit_worker.py` for unit tests of signal functions.
Dry-run smoke test (no keys) should not crash.

**IMPORTANT:** This worker is built but **DORMANT** (not enabled). Do not set `FAILSAFE_ENABLE=1`
or add `HL_API_WALLET_KEY` without explicit operator approval. Strategy parameters are locked
and must not be changed. Never add entry logic to this worker.

## Ops checks (separate cron services)

Read-only ops checks run as separate Railway **cron** services from `ops_cron/` (no LLM). One service per
schedule: exit monitor every 4h, 09:22 HKT daily audit, 08:30 HKT desk-missing, Harbor P&L, Cove BO report,
and River's three jobs. Schedules (HKT and UTC), env vars and deploy steps: [ops_cron/README.md](ops_cron/README.md).

## Narrative

**Narrative stays on Grok.ai / Harbor** — this cron does **not** refresh narrative
overlays. Only GC radar JSON is self-refreshed on Railway. Category labeling uses
the baked / synced watchlist when present.

## Deploy checklist

1. Volume mounted at `/app/out`
2. Push to `main` (Dockerfile builder via `railway.json`)
3. Confirm `/health` → `{"scheduler": true, "scanner": true}`
4. After next 15m tick (or boot missing-file scan), check `out/gc_radar_*.json` mtimes
   and that rows include `tier` + `category`

## Narrative prompt

Paste-ready Grok.ai automation prompt: [`GROK_AI_NARRATIVE_PROMPT.md`](./GROK_AI_NARRATIVE_PROMPT.md).
Suggested daily **07:30 HKT**. Output → `narrative/watchlist.json` → Harbor `/api/sync`.

## Bitunix shadow radar (display / shadow only, 2026-09-29)

APScheduler jobs in `serve.py` (HKT). Own lock `_bx_lock`, never the scan or executor lock; each run is a
subprocess with a hard timeout (`BX_TIMEOUT_S`, default 480 s) and without `HL_API_PRIVATE_KEY`.

| Job | HKT | What |
|-----|-----|------|
| `bx_daily` | 08:20 | Bitunix catalog + tickers, HL mids (BX↔HL match), CoinGecko markets, 1D + 4H candles, depth for vol ≥ $200K, shadow book |
| `bx_4h` | 00/04/12/16/20 :25 | 4H candles, Chase pending (N/N+1), 4H exits, 4H new-token signals |
| `bx_1h` | hourly :27 | 1H candles for 1H-GC new tokens and open shadow positions only |

Env: `BX_ENABLED` (default 1), `BX_TIMEOUT_S`, `BX_REQ_PER_S` (default 5), `COINGECKO_API_KEY` (Demo key; optional).
Endpoints: `GET /api/bx-ui` (password), `GET /api/bx/radar?tf=1d|4h|1h|tradfi`, `/api/bx/shadow`, `/api/bx/review` (AI key).
Manual run: `POST /api/jobs/run` with `bx`, `bx_4h` or `bx_1h`.

**Live pilot (2026-09-30):** when `BX_SERVICE_URL` is set on the cockpit, the jobs above do not run here; they run
in the Singapore `bx-exec` service (`python bx_service.py`) with the live pilot. See `docs/BX_LIVE_PILOT.md`.
