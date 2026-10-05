# C48-1 Passivbot — Hyperliquid paper / small-size service

Minimal scaffolding to run upstream [Passivbot](https://github.com/enarjord/passivbot)
(Unlicense) as a **separate Railway service** under the Cove C48-1 gate (2026-10-05).
It does not touch the cockpit/radar app (root `Dockerfile`, `bx_live`, entry desk, etc.).

- Upstream pinned at `4e15b572f06b354411fbcf377fd9b25b7feb024c` (master 2026-10-04, PR #1873).
  The `Dockerfile` mirrors upstream `Dockerfile_live` at that SHA; bump `PASSIVBOT_SHA` deliberately.
- Locked config: `configs/giiq_c48_1_paper.json` = upstream
  `configs/examples/default_trailing_martingale_long.json` at the pinned SHA with only the locks below changed.
- `giiq_entrypoint.py` runs before upstream `container/entrypoint.sh` and **refuses to start**
  if any lock is broken (also checked at image build time).

> **"Paper" means a real Hyperliquid wallet with small size.** Passivbot has no paper
> mode for Hyperliquid; its `fake` exchange is an offline scripted replay harness, not a
> live paper account. Fund a dedicated small HL wallet. Live or > HK$500 still needs MMT.

## HL demo / testnet: not available in this PR

MMT asked for a Hyperliquid demo/testnet run if possible. At the pinned upstream commit
`4e15b572` it is **not** a supported path:

- Passivbot's Hyperliquid adapter defaults to **mainnet `api.hyperliquid.xyz`** (ccxt default
  hostname). Upstream never calls ccxt `set_sandbox_mode`, and ccxt signs Hyperliquid actions
  as mainnet unless sandbox mode is on.
- The `api-keys.json` hyperliquid entry has **no first-class `is_testnet` / sandbox flag**.
  Its only fields are `wallet_address`, `private_key` and `is_vault`.
- Upstream has a generic `live.custom_endpoints_path` (REST domain/URL overrides). PR #42
  **does not wire HL testnet** through it. A URL rewrite alone would still sign as mainnet,
  so it is **unproven for this pilot**.

**Launch path:** a small-funds **mainnet** wallet with a non-withdraw HL **API wallet** key,
**BTC only** (`live.approved_coins.long = ["BTC"]`, `short = []`), WE 0.02, leverage 2.

## Hard locks (Cove gate C48-1)

| Lock | Value | Enforced by guard |
|---|---|---|
| `live.leverage` | **2** (ceiling ≤ 3) | yes |
| `bot.long.risk.total_wallet_exposure_limit` | **0.02** (≈ NAV 2% notional; ceiling ≤ 0.08) | yes |
| `bot.long.hsl.enabled` / `red_threshold` | **true / 0.08** (`restart_after_red_policy=always`) | yes |
| Worst-case single-coin WE = `TWEL / n_positions × (1 + allowance)` | **≤ 0.08** (now 0.02); allowance per `we_excess_allowance_mode` (`bounded` caps at TWEL, `legacy_raw` uncapped, unknown modes rejected) | yes |
| `live.market_orders_allowed` | **false** | yes |
| `live.user` | **hyperliquid_01** (must equal `PB_USER`) | yes |
| Vault mode | **off** — `is_vault=false`, ordinary wallet only, no vault leader deposits | yes |
| Short side | `bot.short.risk.total_wallet_exposure_limit = 0` | yes |
| `coin_overrides` | empty | yes |

The guard also rejects extra CLI args and `PB_CONFIG_INLINE` / `PB_EXCHANGE` / `PB_API_KEY` /
`PB_API_SECRET`, since those could bypass the baked config. Hard SL / kill switch are not relaxed.

Small-wallet settings (Cove confirmed 2026-10-05 20:03 HKT): `bot.long.risk.n_positions = 1`
and `live.filter_by_min_effective_cost = false`. Coins narrowed at Cove's request:
`live.approved_coins = {"long": ["BTC"], "short": []}`. Strategy parameters are otherwise the
upstream template's.

## Stop conditions (ops — check every spot-check)

Stop the service, flatten all positions on HL, and write a report if **any** of these holds:

1. Equity drawdown **≥ 4%** from the trial start (or the peak during the trial).
2. HSL **RED** (bot logs/monitor show the long HSL RED / panic close).
3. Any single-coin notional **> NAV 8%**.

Also stop at **7 days** or the end of the sprint, whichever comes first. HSL at 0.08 is the
in-bot backstop. The 4% drawdown and 8% single-coin rules are operator checks, not bot settings.

To stop: Railway → service → **Remove deployment** (or scale to 0). Then close the open
positions and cancel resting orders in the HL UI. Stopping the container alone leaves
resting limit orders and positions on the exchange.

## Sizing on a small wallet ($2.5k–$5k NAV)

Passivbot's raw first entry is `balance × WEL × initial_qty_pct` = NAV × 0.02 × 0.0081 ≈
**0.016% of NAV**, i.e. $0.41 at $2.5k and $0.81 at $5k. That is below Hyperliquid's $10
minimum. With upstream's default `filter_by_min_effective_cost=true`, every coin would be
skipped until NAV reaches about $62k, so that filter is now **off**. Upstream then raises the
entry to the exchange minimum: `calc_initial_entry_qty = max(min_entry_qty, raw)`.

Re-entries stop once WE ≥ 0.999 × WEL. An entry that would overshoot is cropped to WEL, but
never below the $10 minimum (`calc_cropped_reentry_qty`). The worst-case position is therefore
about `WEL × NAV` plus at most one minimum order.

These figures come from simulating upstream's own `passivbot_rust.calc_next_entry_long_py`
(pinned SHA) on a falling-price ladder until no entry is returned. It used the locked entry
params and live HL `szDecimals` and mark prices from 2026-10-05 for BTC, ETH, SOL, HYPE, XRP,
DOGE and LINK, with a $10 minimum cost.

| NAV | First order | Entries until capped | Max position notional (cost basis) | Single-coin limit |
|---|---|---|---|---|
| $2,500 | $10.02–$11.33 (0.40–0.45% NAV) | 4–5 | $50.02–$60.30 (**2.0–2.4% NAV**) | 8% → OK |
| $5,000 | $10.02–$11.33 (0.20–0.23% NAV) | 5–6 | $99.97–$110.11 (**2.0–2.2% NAV**) | 8% → OK |

**BTC (the only approved coin):** first order $10.31 at both NAVs. At $2.5k it takes 5
entries to reach a max of **$60.30 (2.41% NAV)**. At $5k it takes 5 entries to reach
**$100.34 (2.01% NAV)**. The other coins are kept in the table as a sizing cross-check only.

So the $10 minimum is **not** skipped, and the worst case stays far below 8% NAV. The
overshoot above 2% comes from the final $10-minimum entry; quantity-step rounding causes the
spread in first-order size. Margin used at 2x is about half the notional. These are sizing
figures, not a performance estimate.

## Run locally (analogue of upstream `docker compose --profile live`)

```bash
cd passivbot
cp api-keys.json.example api-keys.json     # fill with a small dedicated HL wallet; gitignored
docker compose --profile live up --build passivbot-live
```

Logs land in `passivbot/data/logs/`, monitor artifacts in `passivbot/data/monitor/`.
Check the locks without starting the bot: `python3 giiq_entrypoint.py --check-config configs/giiq_c48_1_paper.json`.
Tests: `python3 -m pytest passivbot/`.

## Railway (after MMT merges)

1. **New service** in the existing project → *GitHub repo* (this repo) → Settings →
   **Root Directory = `passivbot`**. Railway then uses `passivbot/railway.json` and
   `passivbot/Dockerfile`. The cockpit service keeps the root `Dockerfile`/`railway.json`.
2. **Volume**: attach one volume to this service, mount path **`/data`**. Logs go in
   `/data/logs`, monitor artifacts in `/data/monitor`. Config and keys are **not** on the volume.
3. **Variables** (service → Variables; mark the HL ones as secrets / sealed):

   | Var | Value |
   |---|---|
   | `HL_WALLET_ADDRESS` | HL main account address (secret) |
   | `HL_PRIVATE_KEY` | HL **API wallet** private key (secret, sealed) |
   | `PB_USER` | `hyperliquid_01` (image default) |
   | `PB_CONFIG_PATH` | `/app/giiq/configs/giiq_c48_1_paper.json` (image default, baked) |
   | `PB_LOG_DIR` | `/data/logs` (image default) |
   | `PB_MONITOR_ROOT` | `/data/monitor` (image default) |
   | `PB_API_KEYS_PATH` | leave unset; the guard renders `/run/passivbot/giiq-api-keys.json` from `HL_*` |
   | `PB_LOG_LEVEL` | optional, e.g. `info` |

   Use an HL **API wallet** (Hyperliquid → API → generate) so the key can trade but not withdraw.
   `wallet_address` must be the main account it acts for, **not** a vault address.
   Upstream's env-credential path (`PB_EXCHANGE`/`PB_API_KEY`/`PB_API_SECRET`) cannot express
   HL `wallet_address`/`private_key`, so the guard uses the `HL_*` variables instead.
4. No healthcheck or public domain is needed. The bot is a worker with no HTTP port.
5. **Only then** deploy, with a small dedicated HL wallet only.

## Spot-check / export for Prism & River (no UI)

Upstream's monitor publisher writes JSON to the volume under
`/data/monitor/hyperliquid/hyperliquid_01/`:

- `state.latest.json` — latest snapshot (`account` balance/equity, `positions`, `open_orders`, `hsl` state)
- `history/fills.current.ndjson` (+ rotated segments) — fills, retained 7 days
- `events/current.ndjson` — live events, including HSL state changes
- `/data/logs/hyperliquid_01.log` — rotating bot log (5 × 10 MB)

Quick checks from a Railway shell (`railway ssh` into the service):

```bash
python3 -c "import json;s=json.load(open('/data/monitor/hyperliquid/hyperliquid_01/state.latest.json'));print(json.dumps(s,indent=1)[:4000])"
tail -n 20 /data/monitor/hyperliquid/hyperliquid_01/history/fills.current.ndjson
grep -iE "hsl|RED|panic" /data/logs/hyperliquid_01.log | tail -n 20
```

Fills and equity can also be reconciled independently from the HL public API
(`POST https://api.hyperliquid.xyz/info` with `{"type":"userFills","user":"<address>"}` and
`{"type":"clearinghouseState","user":"<address>"}`), which needs no keys.

## Later

Bitunix (`bitunix_01`) is supported upstream but **not wired here**. The guard only allows
`hyperliquid`. Adding it needs a separate Cove review.
