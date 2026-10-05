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

## Hard locks (Cove gate C48-1)

| Lock | Value | Enforced by guard |
|---|---|---|
| `live.leverage` | **2** (ceiling ≤ 3) | yes |
| `bot.long.risk.total_wallet_exposure_limit` | **0.02** (≈ NAV 2% notional; ceiling ≤ 0.08) | yes |
| `bot.long.hsl.enabled` / `red_threshold` | **true / 0.08** (`restart_after_red_policy=always`) | yes |
| `live.market_orders_allowed` | **false** | yes |
| `live.user` | **hyperliquid_01** (must equal `PB_USER`) | yes |
| Vault mode | **off** — `is_vault=false`, ordinary wallet only, no vault leader deposits | yes |
| Short side | `bot.short.risk.total_wallet_exposure_limit = 0` | yes |
| `coin_overrides` | empty | yes |

The guard also rejects extra CLI args and `PB_CONFIG_INLINE` / `PB_EXCHANGE` / `PB_API_KEY` /
`PB_API_SECRET`, since those could bypass the baked config. Hard SL / kill switch are not relaxed.

`n_positions = 3` and the canonical strategy parameters are unchanged from upstream.

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

## Known issue: min order size vs. 0.02 exposure

Passivbot sizes the first entry as `balance × WEL × (1 + allowance) × initial_qty_pct`. With the
locked config this is ≈ **0.0074% of NAV**. Hyperliquid's minimum order is $10, so with
`live.filter_by_min_effective_cost=true` (upstream default) every coin is filtered out unless
NAV ≳ **$135k**. The bot then logs `No long symbols are approved due to min effective cost too high`
and places no orders.

| n_positions | TWEL | NAV needed for a $10 first entry |
|---|---|---|
| 3 | 0.02 | ~$135k |
| 3 | 0.04 | ~$68k |
| 1 | 0.02 | ~$62k |
| 1 | 0.08 | ~$15k |

This needs a Cove/MMT decision. It was not changed here because it is outside the lock list.
The upstream option is `live.filter_by_min_effective_cost=false`, which bumps each entry up to
the $10 minimum. Initial entries are then larger than the template intends, so check the
single-coin 8% rule more often.

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

- `state.latest.json` — latest snapshot (balance, equity, positions, open orders)
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
