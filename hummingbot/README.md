# C48-2 — Hummingbot HL testnet perpetual market making (separate Railway service)

C48-2 replaces **C48-1 Passivbot, which is FAIL (Cove final sign-off). Do not deploy `passivbot/`.**

This folder runs the **official Hummingbot image** as its own Railway service (Root Directory = `hummingbot`), headless, on **Hyperliquid testnet**, using only strategy path 1, `perpetual_market_making`. It does not touch the cockpit/radar app, `bx_live`, the entry desk, or the root `Dockerfile`/`railway.json`. There is no UI.

- **Real money: deposit $0.** Testnet only. Mainnet, live trading, or anything over HK$500 needs Harbor plus MMT.
- **No merge-to-live without MMT.** Merging this PR deploys nothing by itself; MMT has to create the Railway service and set the secrets.

Source notes: [Cove gate](docs/c48_2_gate_2026-10-05.md), [Scout card](docs/C48-2_hummingbot_2026-10-05.md), [Forge locked-config notes](docs/locked_config_notes.md).

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | `FROM hummingbot/hummingbot:version-2.16.0@sha256:e222f070…cbd38f` (git tag `v2.16.0`, `8f19061`). Copies the locked config and guard, runs the lock check at build time. |
| `conf_perpetual_market_making_c48_2.yml` | Locked strategy config (Forge upload plus upstream `template_version: 6` and the remaining template keys at upstream defaults). |
| `giiq_entrypoint.py` | Startup guard. Refuses to start if any lock is broken, then execs upstream `bin/hummingbot_quickstart.py`. |
| `assert_locks.py` | Lock checker, usable by Prism/CI: `python3 assert_locks.py [conf.yml] [--nav USD --mid PRICE]`. |
| `test_hummingbot_locks.py` | Tests for the locks and the guard: `pip install pytest pyyaml && python3 -m pytest hummingbot/`. |
| `railway.json`, `docker-compose.yml`, `.env.example` | Railway service definition, local run, and env placeholders only. |

## Hard locks (Cove gate; enforced by `assert_locks.py`, the guard and the tests)

1. `strategy: perpetual_market_making` only. `SCRIPT_CONFIG` (V2 scripts such as `v2_funding_rate_arb`), any `*funding*`/`*arb*` strategy, a different `CONFIG_FILE_NAME`, and extra CLI args are all refused.
2. `leverage: 2` exactly (never above 2; the funding-arb script default of 20 can't be reached).
3. `order_levels: 1`, `order_level_amount: 0`, and `order_override` empty (otherwise it could add extra orders).
4. One order's notional is about **NAV 2%**, capped at **NAV 8%**. See the sizing section below.
5. `stop_loss_spread: 1.0` (must be > 0), `long_profit_taking_spread` and `short_profit_taking_spread` both `0.5` (must be > 0).
6. `derivative: hyperliquid_perpetual_testnet` only. Mainnet `hyperliquid_perpetual`, Binance, and Bitunix are refused.
7. `market: BTC-USD`, `position_mode: One-way`, `price_source: current_market`.
8. No vault and no withdraw-capable keys. The connector is written in `api_wallet` mode with `use_vault: false`, and `HL_USE_VAULT` is refused. The API wallet private key is removed from the environment before Hummingbot starts.
9. Kill switch: the guard writes `kill_switch_mode: {kill_switch_rate: -4.0}` into `conf/conf_client.yml`, so upstream stops the bot at −4% profitability.

### Upstream verification

- **Strategy keys:** checked against `hummingbot/strategy/perpetual_market_making/perpetual_market_making_config_map.py` and `hummingbot/templates/conf_perpetual_market_making_strategy_TEMPLATE.yml` at `v2.16.0` and master (`9af100d`, now tagged `v2.17.0`). Those files are identical in both versions, and every key in the Forge config is valid.
- **Connector name:** `hyperliquid_perpetual_testnet` is an official domain of the `hyperliquid_perpetual` connector (`OTHER_DOMAINS` in `hyperliquid_perpetual_utils.py`, REST `https://api.hyperliquid-testnet.xyz`). Its example pair is `BTC-USD`. Its credential fields are `hyperliquid_perpetual_testnet_mode` (`arb_wallet`/`api_wallet`), `use_vault`, `hyperliquid_perpetual_testnet_address` and `hyperliquid_perpetual_testnet_secret_key`. No extra testnet flag is needed.
- **Headless:** upstream `bin/hummingbot_quickstart.py` reads `HEADLESS_MODE`, `CONFIG_FILE_NAME` (a file in `conf/strategies/`), `CONFIG_PASSWORD` and `SCRIPT_CONFIG` from the environment, the same variables the Railway Hummingbot template uses.
- **Credentials:** the guard encrypts them the way the `connect` command does: `ETHKeyFileSecretManger(CONFIG_PASSWORD)` plus `save_to_yml(conf/connectors/hyperliquid_perpetual_testnet.yml)`. This path has **not been run inside the image yet** (no Docker in the authoring environment), so the first Railway boot log is the check.

## Sizing: `order_amount` is BASE size (BTC), not quote

```
order_amount = (NAV * 0.02) / mid          # target notional = NAV 2%
order_amount * mid <= NAV * 0.08           # hard cap, guard refuses above it
```

The committed value is `0.0005` BTC. At NAV $2,500 and BTC $100k that is $50 (2.0%). At the HL testnet mid of $86,274 on 2026-10-05 it is about $43 (1.7%), above HL's $10 minimum order. On every start the guard fetches the BTC mid from HL testnet `allMids` (or uses `GIIQ_MID_PRICE`), checks `GIIQ_NAV_USD`, logs the % of NAV, and refuses to start above 8%. To recalc without a code change, set `GIIQ_ORDER_AMOUNT`.

## Railway setup (MMT), in order

1. **Merge** this PR (MMT only).
2. **Create the service:** Railway project → New → GitHub Repo → this repo. In the service settings, set **Root Directory = `hummingbot`**. Railway picks up `hummingbot/railway.json` and the Dockerfile builder. Leave the existing cockpit service alone.
3. **Add a volume** to this service, mounted at **`/home/hummingbot/data`**.
4. **Set Variables (secrets)** on this service only:

   | Variable | Value |
   |---|---|
   | `CONFIG_PASSWORD` | Any strong random string (encrypts `conf/connectors/*.yml`) |
   | `HL_TESTNET_ADDRESS` | HL **testnet** main account address `0x…` |
   | `HL_TESTNET_API_WALLET_KEY` | Private key of an HL **testnet API wallet** created at https://app.hyperliquid-testnet.xyz/API (API wallets can trade but cannot withdraw) |
   | `GIIQ_NAV_USD` | Testnet account equity in USD, e.g. `2500` |
   | `GIIQ_ORDER_AMOUNT` | Optional: `(NAV*0.02)/mid` in BTC |
   | `GIIQ_MID_PRICE` | Optional: only if the `allMids` fetch is blocked |

   Do **not** set `SCRIPT_CONFIG`, `HL_USE_VAULT`, or any mainnet/Bitunix/Binance keys. `HEADLESS_MODE` and `CONFIG_FILE_NAME` are already set in the Dockerfile.
5. **Deploy / start.** The log should show `C48-2 sizing: …`, then `C48-2 locks OK; starting hummingbot headless …`. If you see `C48-2 refusing to start: …`, the message says which lock or variable is wrong.

Testnet funds: the testnet account needs USDC from the HL testnet faucet (https://app.hyperliquid-testnet.xyz/drip). Per HL docs, the faucet requires the same address to have a mainnet deposit history. Real deposit stays $0 from this PR.

## Getting testnet fills in 12–24h

- Spreads are 0.2% each side, refreshed every 30 s, so on testnet BTC-USD resting orders should fill within hours. After a fill, the position closes at the profit-taking spread (0.5%) or the stop loss (1%).
- Check for fills in the Railway logs (`Filled`, `BUY`/`SELL` order-filled lines) and in the HL testnet UI (Trade History).
- **If there are no fills after about 6h:** confirm that orders are resting in the HL testnet UI, then raise the issue to Cove. Do not loosen locks to force fills; narrowing spreads needs a new Cove/Forge config.
- Testnet wiring is the proven path. A mainnet fallback (`hyperliquid_perpetual`) is **not enabled**: it would need a code change in this folder, Harbor approval, and MMT.

## Stop conditions (ops): stop, flatten, report

Stop the service, flatten on HL, and write a report if any of these holds:
- equity drawdown **≥ 4%** (the kill switch also stops the bot at −4%, but it does not flatten)
- any single-coin notional **> NAV 8%**
- inventory net exposure **> the notional cap** (NAV 8%)

Also stop at 7 days or the end of the sprint, whichever comes first. Stopping the container (or the kill switch) cancels Hummingbot's orders but **leaves any open position on HL**, so close it in the HL testnet UI.

## Prism / River export paths (on the volume)

| What | Path |
|---|---|
| Trades/fills, orders, positions (SQLite) | `/home/hummingbot/data/conf_perpetual_market_making_c48_2.sqlite` (tables `TradeFill`, `Order`, `OrderStatus`, `Position`, …) |
| Logs | `/home/hummingbot/data/logs/logs_conf_perpetual_market_making_c48_2.log` (`/home/hummingbot/logs` is a symlink into the volume) |
| Guard / startup errors | Railway deploy logs (stdout/stderr are not redirected) |

Example pull: `railway ssh` into this service, then `sqlite3 /home/hummingbot/data/conf_perpetual_market_making_c48_2.sqlite "select * from TradeFill"`.

## Local run

```bash
cd hummingbot
cp .env.example .env    # fill locally with TESTNET values; never commit
docker compose up --build
```
