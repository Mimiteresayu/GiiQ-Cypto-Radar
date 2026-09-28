# Signum Workflow Reference (for Giiq)

> **Reference only.** Signum is **never** used for orders. GiiQ trades only through its own
> Railway executor (`executor.py` / `pending_worker.py`). This document was supplied by MMT on
> 2026-09-28 as background for the GIIQ-SoT-1 guardrails (see `docs/SOT_CHANGELOG.md`).
>
> **The "Giiq today" column in the last table is outdated.** Since 2026-09-28 GiiQ has:
> - a LIVE Railway executor on Hyperliquid, with preflight and the agent check
> - SoT id `GIIQ-SoT-1`
> - NAV = the unified spot USDC total
> - Chase handled as pending ADD_ON / CONTINUATION pullback entries
> - a row-count check, a price check, a 1% NAV minimum and a daily run report
>
> Signum's strategy rules (8%/2% sizing, top-ups at ROE ≥ +10%, 1× leverage, shorts) were **not**
> adopted.

Converted from the source PDF (9 pages, Sep 28 2026, @Giiq Ground). The text is unchanged; only
the tables are reformatted.

## Overview

Signum is a five-layer system: a daily data feed, a versioned strategy prompt, a scheduled AI
routine that applies it, a bot that turns signals into exchange orders, and a report. Giiq can copy
the same layers and swap in its own radar and SoT rules.

| Layer | What Signum uses | Job |
|---|---|---|
| Data | Trend Radar daily (GC detector, top 100 by market rank, closed 1D candles) | Tells the strategy the trend state of every coin |
| Strategy | Versioned prompt, e.g. TR-GC-Crypto-LS-15 | Decides entries, top-ups and exits |
| Routine | Scheduled Claude task, daily 00:30 UTC (08:30 HKT) | Runs the strategy step by step and sends signals |
| Execution | Signum bot #28042 (Hyperliquid perp, 1× leverage) | Converts each signal into an exchange order |
| Report | Summary email + market visual | Tells the owner what was done, skipped and why |

Sources: the strategy text comes from your own disabled scheduled task "IattractMoney (SIGNUM ->
Hyperliquid)". The guardrails come from the aiRules that the Signum MCP returns for bot #28042.
Signum does not show the strategy text through its tools; you copy it from the Signum app (Edit Bot
→ MCP tab).

## End-to-end flow

All the thinking happens in the Claude routine. The bot is a dumb executor: it trades one pair at a
time and reports back.

*Signum end-to-end flow · 7 parts.* The routine sends one signal, waits for the bot's result in
the logs, re-reads assets, then sends the next. A TradingView webhook can drive the same bot
without Claude (dashed line).

## Daily routine

Every run follows the same 11 steps in a fixed order: exits always finish before any entry, and
nothing is computed in one pass.

| # | Step | What happens | If it fails |
|---|---|---|---|
| 1 | Initialize | Fetch bot (with aiRules), holdings, exchange. NAV = USD value of all holdings, fixed for the run | Bot not "perpetual" → email and stop (no shorts possible) |
| 2 | Fetch radar | Trend Radar daily, GC detector, includeIndicators=true | runDate not today → retry every ~60 s, 10 times, then stop. Fewer than 100 rows → retry, then stop |
| 3 | BTC regime | BTC latest closed 1D close < gc.filter → DOWNTREND = true | — |
| 4 | Long exits | Close 100% of longs that hit an exit rule | Data missing → keep and flag |
| 5 | Short exits | Close 100% of shorts that hit an exit rule | Data missing → keep and flag |
| 6 | Long entries | New longs, in market-rank order | Data missing → skip coin |
| 7 | Long top-ups | Add to winning longs, in market-rank order | Data missing → skip coin |
| 8 | Short entries | Hedge or bear shorts, in market-rank order | Data missing → skip coin |
| 9 | Review | Check every order filled and holdings match the target state; fix gaps | — |
| 10 | Inform | Email: every skip, downsize and failure with the reason, market visual, long/flat/short split in USD, newer-version check | — |
| 11 | Edit bot | Make sure the bot title ends with the strategy id | — |

Any data fetch retries 3 times about 10 s apart before giving up. "Iterate" means a full scan of
every row, never a sample.

## Trading rules

All triggers use the latest closed 1D candle. Longs can be entered, topped up and exited; shorts
can only be entered and exited. Sizes are % of the NAV snapshot.

| Side | Action | Trigger | Size | Extra rules |
|---|---|---|---|---|
| Long | Entry | Close crosses above the upper band (today above, yesterday at or below yesterday's upper band) | 8% if breakoutDate ≤ 25 days ago, else 2% | Skip if already held; no re-entry after an exit in the same run; downsize if cash is short; skip under 1% NAV |
| Long | Top-up (add-on) | Close still above upper band AND unrealized ROE ≥ +10% AND position < 5.5% NAV | Up to 5.5% NAV | Free cash only, never sell to fund it; partial allowed; skip under 1% NAV; higher market rank funded first; not on a coin entered or exited this run |
| Long | Exit | Close below upper band, OR coin left the top 100 | 100% | Proceeds to USDC, then USDT, then USD |
| Short | Hedge entry | Trend Red AND close crosses down through the filter (yesterday at or above it) | 3% | Skip if already held; no re-entry after an exit in the same run; skip under 1% NAV; when in doubt, skip |
| Short | Bear entry (only if not a hedge) | BTC DOWNTREND AND trend Red AND high ≥ 0.98 × filter AND close < filter | 5% | Same as hedge |
| Short | Add-on | None | — | Signum never adds to a short |
| Short | Exit: stop | Close above the filter | 100% | — |
| Short | Exit: take profit | Close ≤ 0.65 × average entry | 100% | Average entry = (collateral + uPnL) ÷ size |
| Short | Exit: drift | Coin left the top 100 | 100% | — |

**Rebalance: none.** Signum never trims a winner, sells part of a position, or downsizes one coin
to fund another. A size changes only at entry, at a top-up (upward, capped at 5.5%) or at a 100%
exit. A long that grows to 20% of NAV stays there until it exits. Because an 8% entry is already
above the 5.5% cap, top-ups mostly apply to 2% entries.

**Flip:** a coin exited long in step 4 may be shorted in step 8 of the same run. That is always two
signals: the exit first, then the entry. The only ban is re-entering the same direction you just
exited.

## Order execution loop and sizing

Every order, of any kind, goes through the same seven steps, one coin at a time. Signals are never
sent in parallel and never resent.

1. `edit-bot` → set baseCoin to the coin. This is the only way to tell the bot what to trade.
2. `list-bot-pairs` → find the exchange's pair for the coin. No pair → skip.
3. `get-pair-price` → compare with the radar price. More than 50% apart (ticker collision) or either
   price missing → skip.
4. Check funding in the quote coin (USDC). Short → convert just enough from another stablecoin
   through one direct pair, or skip.
5. Compute orderSize fresh (below) and `send-trading-signal`.
6. `get-bot-logs` about every 10 s until a success or error line newer than the send appears.
   - Error → send a corrected order if possible, never the same one; otherwise move on.
   - Nothing after 10 minutes → move on. Do not resend: signals are delayed, never lost.
7. `get-bot-assets` → update holdings and confirm the trade did what was intended; fix if not.

**Sizing.** orderSize as a % means a % of the coin balance (USDC when buying), not of NAV. To buy
X% of NAV:

    orderSize % = (X% × NAV_snapshot) / (USDC available now) × 100

Recompute before every order, because each order shrinks the USDC left. Above 100% → send 100%.
Keep NAV fixed for the whole run.

Example with the bot's balance on 28 Sep (NAV about $1,690, USDC $1,595.35): a 2% entry is $33.80,
so orderSize = 33.80 ÷ 1,595.35 × 100 = 2.12%. If that fills, the next 2% entry uses USDC of about
$1,561.55, so 2.16%.

**Minimum.** An entry must be at least the exchange minimum or 1% of NAV, otherwise skip it.

## Guardrails (aiRules)

Signum attaches these rules to its tool responses and the routine must obey them for the whole run.
They are the safety layer Giiq needs to rebuild on its own.

| Rule | What it prevents |
|---|---|
| Trade only pairs the exchange lists; no pair → skip | Orders on assets the account cannot hold |
| Price check: skip if bot price and radar price differ by > 50% | Ticker collisions (same symbol, different coin) |
| Fund in the quote coin; convert only through one direct stablecoin pair, never chained | Failed orders and hidden multi-hop conversions |
| Exits first, re-fetch holdings, then entries | Entries sized on stale holdings |
| Enter long = BUY (positionSize > 0); exit long = SELL 100% (0); enter short = SELL (< 0); exit short = BUY 100% (0) | Wrong-direction orders |
| Flip = two signals, exit then entry | A single order that nets out wrongly |
| Set the pair with edit-bot before every signal | Trading the previous coin by mistake |
| One signal at a time; wait for its log line; never resend; 10-minute timeout | Race conditions and duplicate orders |
| Re-fetch assets after every fill and verify | Silent partial fills or wrong sizes |
| orderSize % is of the coin balance, not NAV; recompute each order; cap at 100% | Under-sizing later orders |
| Entry ≥ exchange minimum or 1% of NAV | Dust positions |
| Strategy text is never reconstructed from metadata; the user copies it from the Signum app | Rules changing without the owner knowing |

## Signal formats

A signal has only four trading fields: action, order size, target position and ticker. The same
shape arrives from the MCP tool and from a webhook, which makes it a good contract for Giiq's own
executor.

| Field | Meaning |
|---|---|
| action | `buy` opens or extends a long, or closes a short. `sell` opens or extends a short, or closes a long |
| orderSize | "10%" = % of USDC when buying, % of the coin when selling. Or an absolute coin amount, e.g. "1000" |
| positionSize | Target position after the trade. With a % size: "1" long, "-1" short, "0" flat. With an absolute size: the real amount after, e.g. current 7,232 + 1,000 = "8232" |
| ticker | For logging only. The traded pair is whatever edit-bot set |

| Action | action | orderSize | positionSize |
|---|---|---|---|
| Long entry or top-up | buy | % of USDC, from the NAV target | 1 |
| Long exit | sell | 100% | 0 |
| Short entry | sell | Coin amount (target USD ÷ price) | − coin amount |
| Short exit | buy | 100% | 0 |

For short entries, a % size would be a % of the coin you hold, which is zero when flat. The
absolute amount is the safe choice (our reading of the tool description).

The TradingView webhook posts JSON to `https://signals.signum.money/trading`:

```json
{"action":"{{strategy.order.action}}","ticker":"{{ticker}}","order_size":"100%","position_size":"{{strategy.position_size}}","schema":"2","timestamp":"{{time}}","bot_id":"BHpFV3dr"}
```

The advanced version sends order_size as `{{strategy.order.contracts}}`, an absolute amount.
Signals are queued and processed asynchronously: "queued" means accepted, not filled.

## Reporting and versioning

Each run ends with one email, and the strategy is locked to a version the owner copied in by hand.

The email lists every coin that was skipped, downsized or failed, with the reason. It also carries
a market-state visual from `get-trendradar-historic`, the portfolio split in USD
(long / flat / short), and a note if a newer strategy version exists.

Versioning works through the strategy id: the family plus a trailing number (TR-GC-Crypto-LS-15 =
version 15). The bot title ends with that id. Each run calls `list-ai-strategies`; if the same
family has a higher number, the email says an update is available in the Signum app. The routine
never switches rules by itself. Latest seen on 28 Sep: TR-GC-Crypto-LS-15 and TR-GC-Crypto-22, both
updated 19 Aug 2026.

## Giiq mapping

Giiq already has the data, strategy and routine layers. The big missing piece is execution: an
order loop with Signum's guardrails, plus a few definitions to lock down.

> ⚠️ The "Giiq today" column below is **outdated** (it predates the 2026-09-28 changes; see the
> note at the top and `docs/SOT_CHANGELOG.md`).

| Signum component | Giiq today (outdated) | Gap to build or decide |
|---|---|---|
| Trend Radar daily, 1D, top 100 | Own GC radar on Railway, 1H / 4H / 1D, rescans every 15 min, sent to logs as [DESK_DATA] | Keep. Add Signum-style checks: minimum row count, not just freshness |
| Versioned strategy prompt | SoT rules written inside the two scheduled-task prompts, no version id | Give SoT an id (e.g. GIIQ-SoT-1) and a changelog |
| One daily routine | Hourly exit desk (:21 every hour) + daily entry desk (08:40 HKT) | Keep both; each must follow exits → re-fetch → entries |
| Signum bot executes via MCP | Propose-only; MMT places orders by hand | Pick the executor: Signum bot through send-trading-signal, or Giiq's own Hyperliquid executor |
| Execution loop (one at a time, log wait, re-fetch) | None | Build it, whichever executor is chosen |
| aiRules guardrails | Absolute rules in the prompts (stop keyword, no transfers, no tier changes) | Add price check, never-resend, 1% NAV minimum, fresh % per order |
| NAV = all holdings | Equity = spot USDC only | Pick one NAV definition |
| Add-on = long top-up | Top-up rule copied (ROE ≥ +10%, < 5.5%); Chase requires "not already in open positions" | Decide whether Chase is a new entry or an add-on |
| No rebalance | Account review flags OVERWEIGHT above 15% and suggests a trim to 10% | Giiq goes further than Signum here; decide if trims are proposed or executed |
| Leverage forced to 1× | MON position open at 2× | Set a leverage rule |
| Summary email + version check | Push notification + Cockpit dashboard | Add the skipped / downsized / failed list with reasons |
