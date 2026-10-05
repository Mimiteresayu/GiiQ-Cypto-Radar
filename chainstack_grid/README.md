# C48-3 Chainstack HL grid (HL TESTNET ONLY)

Thin Railway wrapper around the open-source
[chainstacklabs/hyperliquid-trading-bot](https://github.com/chainstacklabs/hyperliquid-trading-bot)
(Apache-2.0) BTC grid. **Real money $0. Mainnet is refused at every layer.** Same pattern as
`hummingbot/`: locked config + startup guard + lock tests.

| File | Role |
|---|---|
| `Dockerfile` | python:3.13-slim + uv (both pinned by digest), upstream at a pinned SHA, `uv sync --frozen`, patches, build-time lock check + `--validate` + tests. No `VOLUME`. |
| `bots/c48_3_btc_grid_locked.yaml` | upstream `bots/btc_conservative.yaml` with the Cove hard locks |
| `apply_patches.py` | B1-B3 exact-text patches on upstream (build fails if an anchor drifts) |
| `assert_locks.py` | yaml lock assertions (build + startup) |
| `giiq_entrypoint.py` | startup gates, leverage set/readback, real-NAV sizing, watchdog |
| `test_chainstack_locks.py` | unittest, no Docker/network |

## Upstream pin

`7e930aa6b0d296f029943834c2133beff6174d80` (main HEAD, 2026-06-29, "docs(claude): fix
inaccuracies..."). Chosen because it is the latest upstream commit when C48-3 was built, it ships
`uv.lock` (so `uv sync --frozen` reproduces deps incl. `hyperliquid-python-sdk==0.24.0`), and it
is the exact commit Prism spot-checked (B1-B4 below). The Dockerfile fetches that SHA and fails
the build if `git rev-parse HEAD` differs. Bump only together with a re-run of the patches/tests.

## Hard locks

| Lock | Value | Enforced by |
|---|---|---|
| `exchange.testnet` | `true` | assert_locks, patched `_convert_config` raises otherwise, entrypoint env guard, SDK client hardwired to `api.hyperliquid-testnet.xyz` |
| `account.max_allocation_pct` | 2 (<= 2) | assert_locks; entrypoint aborts if allocation / real equity > 2% |
| `stop_loss_enabled` / `stop_loss_pct` | true / 6 (4-8) | assert_locks; B2 patch passes it to upstream `StopLossRule` |
| `max_drawdown_pct` | 4 (<= 4) | assert_locks; B2 -> upstream `DrawdownRule`; watchdog (peak equity) |
| `max_position_size_pct` | 8 (<= 8) | assert_locks; B2 -> upstream `PositionSizeRule`; watchdog |
| `tpsl_mode` | polling | assert_locks (upstream runs `StopLossRule` only in polling mode) |
| symbol | BTC | assert_locks; watchdog stops on any non-BTC position/order |
| leverage | 2x isolated target, <= 3 hard | entrypoint `update_leverage` + readback at start; watchdog every poll |

Stop loss 6%: upstream measures SL as margin-relative ROE, so at 2x it fires at roughly a 3% adverse
price move from entry. That sits inside the upstream +/-5% grid range (normal grid oscillation
survives) but cuts a sustained break-out well before the 4% account DD stop matters. Levels (10),
range (+/-5%) and rebalance (12%) are unchanged from upstream.

## Upstream gaps found and patched (Prism B1-B4)

- **B1** `src/core/enhanced_config.py` requires `max_drawdown_pct >= 5` and `max_position_size_pct >= 10`,
  so DD 4 / pos 8 fail `from_yaml` and `--validate`. Patched minimums to 1.0; gate values unchanged.
- **B2** `src/run_bot.py::_convert_config` never put `risk_management` into the engine config, so
  `RiskManager` ran on code defaults (SL off, DD 15%, pos 30%) whatever the yaml said. Patched to
  pass SL/TP/tpsl_mode/DD/pos through.
- **B3** `_convert_config` sized the grid from a hardcoded `$1000` "NAV". Patched to read
  `GIIQ_NAV_USD`, which the entrypoint sets to the real testnet `marginSummary.accountValue`. The
  entrypoint logs NAV, allocation and NAV% and aborts if allocation > 2% NAV. Upstream also shrinks
  the grid so each level is >= $10.5 (HL $10 min notional + 5%); the entrypoint logs the effective
  level count and aborts below 2 levels (upstream's geometric spacing divides by `levels - 1`).
- **B4 / leverage.** Upstream has no leverage field and never calls `update_leverage` (it appears only
  in the endpoint routing table, `src/core/endpoint_router.py`). Orders are plain limit orders, so they
  trade at whatever leverage the HL account already has for BTC. An account that never set it reads
  back **cross 20x** on testnet (`activeAssetData`, checked 2026-10-05). Upstream only reads
  leverage back in `get_positions` (`position.leverage.value`) to scale TP/SL math. The entrypoint
  calls `update_leverage(2, "BTC", is_cross=False)` on testnet, reads back the BTC setting
  (`activeAssetData.leverage.value`) and any open position's `clearinghouseState` `leverage.value`,
  and exits non-zero if the max is > 3. The watchdog repeats the readback every poll.
- Upstream reads `HYPERLIQUID_TESTNET` in `_convert_config` but never uses it; network is decided
  only by yaml `exchange.testnet`. Hence the yaml lock + the patched refusal.

## Railway deploy

1. New service from this repo, **Root Directory = `chainstack_grid`** (builder: Dockerfile via `railway.json`).
2. Attach a **Railway Volume at `/data`**. Logs go to `/data/logs/c48_3_<UTC>.log`; a watchdog stop
   writes `/data/c48_3_HALTED`, which blocks restarts until a human deletes it.
3. Variables (names only, values in Railway, never in git):
   - `HYPERLIQUID_TESTNET=true`
   - `HYPERLIQUID_TESTNET_PRIVATE_KEY`: secret. Must be an **API / agent wallet** private key created at
     app.hyperliquid-testnet.xyz/API and approved for the master, **not the main/withdrawal key**.
     The entrypoint refuses to start if the key's address equals the master or is not in the master's
     `extraAgents`.
   - `TESTNET_WALLET_ADDRESS`: the master account address (holds the funds). Upstream reads it in
     `src/exchanges/__init__.py`; without it the bot would trade the empty agent address.
   - Optional: `GIIQ_WATCHDOG_SEC` (default 60).
   These are the only key/address names upstream reads for testnet (`src/core/key_manager.py`,
   `src/exchanges/__init__.py`). The entrypoint refuses anything mainnet or legacy:
   `*MAINNET*`, `HYPERLIQUID_PRIVATE_KEY(_FILE)`, `HYPERLIQUID_TESTNET_KEY_FILE`, endpoint overrides
   (`HYPERLIQUID_[TESTNET_]PUBLIC_*`, `HYPERLIQUID_[TESTNET_]CHAINSTACK_*`), and any value containing
   `hyperliquid.xyz`. `GIIQ_NAV_USD` is set by the entrypoint from measured equity (a preset value is ignored).
4. Fund the master from the testnet faucet with **>= $1,050 testnet USDC**: 2% must cover 2 grid levels x
   $10.5. At $1,451 the grid runs 2 levels (~$29 total); 10 levels need >= $5,250.
5. Restart policy is `NEVER`: any stop stays stopped until reviewed.

Startup order: env/testnet guard -> halt marker -> `assert_locks` -> upstream `run_bot.py --validate`
-> API-wallet check -> `update_leverage` + readback -> real-NAV sizing -> `run_bot.py <locked yaml>`.
Any failure exits 2 before the bot starts.

## Stop conditions (watchdog, every `GIIQ_WATCHDOG_SEC`)

Stop the bot (SIGTERM, so upstream also cancels its orders), cancel all BTC orders via the SDK,
write the halt marker, exit non-zero when any of:

- account drawdown >= 4% from peak equity since start;
- single-coin notional > 8% NAV, or any non-BTC position/order;
- BTC leverage (setting or position) > 3;
- reconciliation failure: 3 consecutive failed `clearinghouseState` reads, or more open orders than
  2 x grid levels;
- the bot process exits for any reason.

Positions are left open on stop (upstream's conservative shutdown); close them manually after review.

**Prism must spot-check fills (place/fill/cancel logs vs HL testnet `clearinghouseState`, lev <= 3,
notional <= 8% NAV, DD) before any YELLOW/PASS.**

## Tests

```bash
cd chainstack_grid
python3 -m unittest -v test_chainstack_locks          # locks, Dockerfile, env guard, leverage/watchdog mocks
# B1/B2/B3 against real upstream code (the Docker build runs these too):
GIIQ_UPSTREAM_DIR=/path/to/patched/upstream /path/to/patched/upstream/.venv/bin/python -m unittest -v test_chainstack_locks
```
