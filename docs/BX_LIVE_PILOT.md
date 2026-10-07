# Bitunix live pilot — runbook (MMT-approved 2026-09-30, for Harbor review)

Live Bitunix trading at pilot size, separate from HL GIIQ-SoT-3 (HL is unchanged). Everything Bitunix runs in a
new Railway service **bx-exec** in **Singapore** (`asia-southeast1-eqsg3a`). The US-region cockpit never calls
Bitunix once `BX_SERVICE_URL` is set; it only proxies read views and forwards ENTRY_DESK BX decisions.

## Hard prerequisites (fail closed — any one missing = no new live order)
| # | Prerequisite | Where it is enforced |
|---|---|---|
| 1 | Non-US egress: two geo sources both say non-US and agree; Railway region not `us-*`; IP = `BX_EXPECTED_EGRESS_IP` when set. Logged at startup as `[BX_EGRESS]`. | `bx_egress.evaluate`, `bx_live.live_gate` |
| 2 | `BX_API_KEY` / `BX_API_SECRET` present (Railway variables of bx-exec only); a signed account read succeeds (bad key or IP not whitelisted → refuse). Never logged. | `bx_trade`, `bx_live.run_entries`, `/api/bx/status` |
| 3 | `BX_ENABLED=1` and `BX_LIVE=1` (default **0**); circuit breaker not tripped | `bx_live.live_gate` |
| 4 | ENTRY_DESK approval (source=claude, today HKT, this symbol). No fallback for BX. | `bx_live.approval_for`, `check_entry` |

## Pilot rules
- **Universe:** BX-only crypto, 24h vol ≥ $200K (flag `low_vol_under_1M` under $1M; liquidity exit below $200K), spread < 10 bp (re-measured right before the order), position ≤ 0.5% of 24h vol. No watch tier, no 1H-GC signals, no stock / commodity / index contracts.
- **Signals:** Base (1D), Chase (as a simulated-then-live N/N+1 CONTINUATION pending, 7-day TTL), new tokens on a 4H GC. Only candidates present at 08:02 HKT can be approved; later intraday signals stay shadow-only.
- **Size:** isolated margin 1% of NAV, 3x. NAV = HL NAV (public HL API, same definition as the HL executor) + Bitunix futures equity. Downsized to 0.5% of 24h vol; skipped below the contract minimum.
- **Caps:** max 2 open BX positions; max 1 new BX entry per HKT day (pending fills count).
- **Hard SL:** tier rule on the 4H radar (Mega/Large 4H Lower, Small/Tiny 4H Filter), ≥ 1.5% below price, attached to the entry order (MARK_PRICE trigger, market). Estimated isolated liquidation (maintenance rate from the public position tiers) must sit below the Hard SL. After the fill the position, margin mode, leverage, exchange liq price and the SL order are verified; a missing SL is placed again; if that fails the position is closed at once.
- **Exits:** 4H close < 4H Filter; time cap 7 days (5 days for new tokens); liquidity exit when 24h vol < $200K or spread > 30 bp; the exchange Hard SL.
- **Circuit breaker:** live BX P&L (closed after fees and funding + open unrealized) ≤ −3% of the pilot NAV baseline (frozen at the first live check) → sticky `bx_breaker.json`, no new entries, `[BX_ALERT]`, **BX_BREAKER problem on the cockpit exit health (EXIT_DESK emails MMT)**, and `BX_LIVE=0` through the Railway API when `RAILWAY_API_TOKEN` is set. Reset only with `BX_ADMIN_KEY`.
- **Switches:** `BX_ENABLED=0` stops everything (exchange SL orders stay). `BX_LIVE=0` = shadow only: no new live orders; exits and SL repair of already-open live positions keep running (close-only).
- **Ledger:** every signal still goes to `bx_shadow_ledger.db`; live trades are rows with `mode='live'` plus order / position ids and USD P&L. The comparison report shows live separately.

## Schedule (bx-exec, HKT)
| Time | Job |
|---|---|
| 08:02 | BX radar daily → shadow book → **BX candidates** (ready before the 08:10 ENTRY_DESK) |
| 08:10–08:50 | ENTRY_DESK posts `bx_decisions` with its HL decisions (see ENTRY_DESK_BX.md) |
| 08:56 | **Live entries** (approved only) |
| every 4h :05 | 4H radar → shadow → live exits, SL repair, Chase pending fills, breaker |
| hourly :09 | 1H radar → shadow → live exits, SL repair, breaker |

## Setup after Harbor approves (MMT)
1. Merge PR #26, then this PR.
2. Railway → own-trend-radar → New service from the repo (`main`), name **bx-exec**:
   - Region **Southeast Asia (Singapore) `asia-southeast1-eqsg3a`**
   - Start command `python bx_service.py` (healthcheck `/health` from railway.json works)
   - Volume mounted at `/app/out` (a new one, in Singapore)
   - **Enable Static Outbound IPs** (Pro plan) and note the IP. The Bitunix whitelist needs a fixed IP; Railway egress is not fixed otherwise.
   - Generate a public domain (the cockpit calls it).
3. Variables on **bx-exec** (never on the cockpit): `BX_ENABLED=1`, `BX_LIVE=0`, `BX_SERVICE_KEY=<random>`,
   `BX_ADMIN_KEY=<random, MMT only>`, `HL_ADDRESS=<HL wallet address>`, `BX_EXPECTED_EGRESS_IP=<static IP(s), comma-separated for HA>`,
   optional `COINGECKO_API_KEY`, `RAILWAY_API_TOKEN` (lets the breaker set BX_LIVE=0), `BX_ALERT_WEBHOOK`.
   Do **not** set `HL_API_PRIVATE_KEY` here.
4. Variables on **cockpit**: `BX_SERVICE_URL=https://<bx-exec domain>`, `BX_SERVICE_KEY=<same>`. The cockpit then
   stops its own US-region BX jobs.
5. Check the bx-exec log: `[BX_EGRESS] ok=True ip=<static IP> countries={SG, SG} region=asia-southeast1-eqsg3a`.
6. MMT creates the Bitunix API key: **trade permission only, no withdrawal, IP whitelist = the static IP**.
   Set `BX_API_KEY` / `BX_API_SECRET` on bx-exec only.
7. `GET <cockpit>/api/bx/status` (AI key): `account_check.ok = true`, `live_blockers` = only `BX_LIVE=0`.
8. Add the ENTRY_DESK BX section (ENTRY_DESK_BX.md) to the 08:10 desk task.
9. MMT sets `BX_LIVE=1`. Worst case before the automatic stop: −3% NAV.

## Endpoints
- bx-exec: `GET /health` (open) · `X-BX-Key`: `GET /api/bx/status | candidates | bx-ui | radar?tf= | review | shadow`, `POST /api/bx/decision` · `X-BX-Admin-Key`: `POST /api/bx/breaker/reset`
- cockpit: `/api/ai/candidates` adds a separate `bx` section; `/api/ai/decision` accepts `bx_decisions` (forwarded, never stored with HL decisions); `/api/bx/*` and the Bitunix tab are proxied; `/api/exit/health` adds BX_BREAKER / BX_NO_SL / BX_EGRESS problems.

## Dry run
`python bx_live.py dry-run` prints the exact signed requests (headers redacted) for an example entry.
