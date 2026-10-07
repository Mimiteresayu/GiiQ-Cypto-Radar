# Gaussian Channel (GC) research — hand-off from the trading session (2026-10-07)

Read this first in the new research session. Everything below was verified in the live-trading session unless marked
**UNVERIFIED**. The research project is **read-only**: it never places orders and never edits the live repo.

## 1. Goal (MMT)
Find the best Gaussian Channel settings, **trading timeframe** and **per-sector customised settings** for GiiQ.
Sectors to cover: large crypto, small crypto, RWA tokens (e.g. XAUT), commodities (XAU, XAG, CL, BZ on Bitunix), stocks / index ETFs,
other. Metric chosen by MMT: **Sharpe ratio** (also report win rate, worst trade/drawdown, number of trades).

## 2. How GiiQ's channel works today (live repo `Mimiteresayu/GiiQ-Cypto-Radar`, file `scan_gc_radar.py`)
- Gaussian Channel: poles = 4, multiplier = 1.414, source hlc3 (`compute_gc(highs, lows, closes, poles, period, mult, reduced_lag=False, fast_response=False)`).
- Period by timeframe (`TF_CONFIG`): **1D = 144, 4H = 72, 2H = 144, 1H = 48**. Lines: `upper`, `filter`, `lower`.
- **Trend Green** = filter > previous filter, else Red. (A "green flip" = trend changing Red -> Green.)
- `dual_cross_up` = close > upper AND previous close <= previous upper (closed bars only).
- `dual_cross_down_filter` = close < filter AND previous close >= previous filter.
- New info fields (2026-10-07): `cross_age_bars`, `prev_cross_age_bars` (information only).
- Signals use CLOSED bars only. The 1D bar closes 08:00 HKT; scan 08:05; orders 08:55 HKT.

## 3. GiiQ trading rules as they stand (all merged to `main`, PR #55, #56, #57)
- **Base** = fresh 1D `dual_cross_up` AND 1D trend Green (kept as is; MMT wants this tested, see section 6). A fresh cross is at most 1 day old.
- **Chase / "4H Breakout"** = 1D Green + 4H Green + 4H `dual_cross_up`. It is a signal, not an order. CONT / ADD_ON pullback pendings
  (3-step: 1D breakout -> 4H retrace to Filter/Lower -> next 4H breakout) are **disabled by default** and judged not useful (rarely arm; see section 6).
- At-entry guards: live price still above the 1D Upper; Hard SL exists (mega/large = 4H Lower, small/tiny = 4H Filter); stop distance >= 1.5%;
  liquidation beyond the Hard SL; margin caps (80% of NAV total, 20% of NAV notional per coin).
- **Size (flat by tier, margin % of NAV):** tiny 2 / small 3 / large 4 / mega 5. Leverage 2-5x (tiny max 3x; margin x leverage <= 20% NAV).
- **Exits (Railway):** small/tiny = 1H close below 1H Lower (checked :07); mega/large = 4H close below 4H Filter (checked 4-hourly :10); Hard SL always live.
  Bitunix: 4H close below 4H Filter, 7-day time cap, liquidity exit below $200K 24h volume or spread > 30 bp.
- **Bitunix liquidity tiers:** tradeable = 24h volume >= $200K and spread <= 10 bp (MMT lowered from $2M); flag `low_vol_under_1M`; only crypto is tradeable today.
- **Daily ADD_ON top-up (Signum style), OFF** (`DAILY_ADDON_ENABLED=1`): held long + closed 1D close above 1D Upper + price gain >= +10% + coin <= 20% NAV -> add 2% margin, once a day.

## 4. Signum's rules (MMT's prompt, bot 28747, TR-GC-Crypto-LS-15 + TR-GC-Stocks-4)
- Crypto long entry: closed 1D cross above upper, **no Green condition**; size 8% of NAV if `breakoutDate` within 25 days else 2%; leverage 3x.
- Crypto long exit: closed 1D close **below the upper band**. Top-up: close above upper + unrealized **ROE** >= +10% -> top up to 5.5% of NAV, free cash only.
- Crypto shorts: hedge (Red trend + close crosses down through filter, 3% NAV) and bear (BTC below filter + rejected bounce, 5% NAV); stop = close above filter; take profit at 0.65 x entry.
- Stocks long: entry on GREEN FLIP (trend state flips to Green), 4% NAV; exit on RED FLIP.
- `breakoutDate` is a Signum field that GiiQ does not have; its exact meaning is **UNVERIFIED**.

## 5. Verified: GiiQ's channel = Signum's channel
Compared on the last closed daily bar (2026-10-06) using Signum MCP `get-trendradar-daily` (detector gc, `indicatorFields=["data.gc"]`) against our scan:
BTC upper 78,249 vs 78,218; filter 75,065 vs 75,056; lower 71,882 vs 71,895. ETH and BNB match the same way. Differences 0.00-0.07%
(price source: Signum = Binance, GiiQ = Hyperliquid). Trend colours identical. Only 3 coins checked; other timeframes not compared.

## 6. Questions the research should answer (MMT's open points)
1. **Base: cross + Green vs cross only.** MMT believes the green flip usually comes *after* the momentum/breakout, so requiring Green may skip many crosses
   (orders dropped sharply). Test A = cross + Green, B = cross only, R = crosses while Red. Script ready: `scripts/bt_base_green_vs_cross.py` (+ `test_bt_base_green_vs_cross.py`).
2. Best **period / multiplier / timeframe** per sector (1D vs 4H vs 2H; period grid around 144/72/48).
3. **Exit rule:** Signum (1D close below upper) vs GiiQ (1H Lower / 4H Filter / Hard SL). MMT says a short-term continuation needs a tighter entry and exit.
4. **Top-up threshold:** price gain +10% vs ROE +10% (at 3x, ROE +10% is only ~+3.3% price).
5. **Size by tier** (2/3/4/5) vs Signum's 8%/2%; **breakout age** for sizing (GiiQ has none).
6. Commodities / RWA / stocks: does the same channel work? Market-hours gaps (weekend/holiday) can fake a cross; define a skip rule.
7. Shorts (Signum style) — signals only, report-only.

## 7. Method rules (MMT + lessons learned)
- Fit settings on one period and **test on a later period** (walk-forward). Include fees and slippage. Ignore results with < ~30 trades. Keep parameter grids small.
- Report Sharpe(B) - Sharpe(A) with a bootstrap interval, not a single number.
- Download candles once and save them (CSV/parquet) so tests run offline.
- Close-only signals; fill at the next bar's open (the 08:55 entry is just after the 08:00 daily close).

## 8. Environment notes
- Cloud sessions block most domains: add `api.hyperliquid.xyz` (crypto candles, `candleSnapshot`) and a stocks/commodities source (e.g. `query1.finance.yahoo.com`) to the environment network allow-list.
- Do not put any key in this project; public price data only.
- Live repo files worth copying: `scan_gc_radar.py` (`compute_gc`, `TF_CONFIG`, `cross_up_ages`), `bt_gc_ltf_period.py`, `scripts/bt_base_green_vs_cross.py`,
  `UNIVERSE.md`, `data/bx_asset_class.json` (BX commodity/stock/index seeds), `docs/SOT_CHANGELOG.md` (history of every rule change and why).
- Live-trading status: manual Bitunix route `POST /api/bx/run` exists; BRUSDT (Base) was not bought on 2026-10-07 because Bitunix had no fallback at 08:56 (added after).

## 9. Starter prompt for the research session
> This is a research project, not live trading. Read `docs/research/GC_RESEARCH_HANDOFF.md` (copy it into this repo first). Goal: best Gaussian Channel settings, timeframe and
> per-sector settings, judged by Sharpe ratio with walk-forward testing and fees. First download daily/4H candles for a sample of coins per sector, save them as files, then
> run the Base A/B test (cross + Green vs cross only), then the period/multiplier/timeframe grid per sector. Never touch the trading repo.
