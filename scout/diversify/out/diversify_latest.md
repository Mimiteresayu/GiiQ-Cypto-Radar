# GiiQ Crypto Diversification: Backtest v2 Results

**Generated:** 2026-10-05T03:48:19.009553  
**Version:** v2_fixed  
**Earliest Data:** 2020-08-19  
**Avg Days per Coin:** 1200  

---

## Sanity Test: Buy & Hold BTC

✅ **PASS** (error: 0.00000000)
- Calculated return: 6.333962
- Expected return: 6.333962
- Days: 2239

---

## Strategy Comparison

| Strategy | Total Ret | CAGR | Sharpe | MDD | PF | Trades | Underpowered | Corr GiiQ BO | Corr BTC |
|----------|-----------|------|--------|-----|----|----|--------------|-------------|----------|
| reversal_with_costs | -73.82% | -22.56% | -0.93 | 84.61% | 0.85 | 189 | ✅ | 0.059 | 0.091 |
| reversal_no_costs | -65.60% | -18.42% | -0.72 | 81.51% | 0.89 | 189 | ✅ | 0.059 | 0.091 |
| funding | ERROR | Insufficient historical fundin | - | - | - | - | - | - | - |
| pairs_with_costs | -88.92% | -22.21% | -1.10 | 91.30% | 0.67 | 2792 | ✅ | 0.030 | 0.106 |
| pairs_no_costs | -57.99% | -9.42% | -0.38 | 72.31% | 0.87 | 2792 | ✅ | 0.029 | 0.107 |

---

## IS/OOS Breakdown

| Strategy | IS Return (≤2023) | IS Sharpe | OOS Return (2024+) | OOS Sharpe |
|----------|------------------|-----------|-------------------|------------|
| reversal_with_costs | 18.26% | 0.76 | -77.86% | -1.36 |
| reversal_no_costs | 26.52% | 1.02 | -72.81% | -1.16 |
| pairs_with_costs | -79.05% | -1.17 | -47.14% | -1.09 |
| pairs_no_costs | -57.71% | -0.60 | -0.66% | 0.05 |

---

## Strategy Details

### reversal_with_costs

**Strategy:** Cross-Sectional Reversal (with_costs)  
**Total Return:** -73.82%  
**CAGR:** -22.56%  
**Sharpe:** -0.93  
**Max Drawdown:** 84.61%  
**Profit Factor:** 0.85  
**Trades:** 189  
**Days:** 1321  

#### Year-by-Year Performance

| Year | Return | Sharpe | MDD | Trades |
|------|--------|--------|-----|--------|
| 2023 | 18.26% | 0.76 | 13.72% | 0 |
| 2024 | -58.23% | -2.04 | 59.53% | 0 |
| 2025 | -50.89% | -1.86 | 52.27% | 0 |
| 2026 | 7.92% | 0.42 | 25.60% | 0 |

#### Sample Trades (first 10)

| Entry Date | Exit Date | Pair/Assets | Direction | Entry Z/Signal | Exit Z/Signal | PnL Gross | PnL Net | Reason |
|------------|-----------|-------------|-----------|----------------|--------------|-----------|---------|--------|
| 2023-02-22 | - | Long: FET, LTC, ADA, AAVE, Short: XLM, ZEC, LINK, ICP | Rebalance | - | - | - | - | Turnover: 1.00, Cost: 0.095% |
| 2023-03-01 | - | Long: AVAX, ICP, SAND, NEAR, Short: INJ, ETH, LTC, FET | Rebalance | - | - | - | - | Turnover: 2.00, Cost: 0.190% |
| 2023-03-08 | - | Long: FET, INJ, NEAR, ZEC, Short: ETH, XLM, BNB, XRP | Rebalance | - | - | - | - | Turnover: 1.50, Cost: 0.143% |
| 2023-03-15 | - | Long: XRP, LTC, ZEC, UNI, Short: ETH, BTC, FET, INJ | Rebalance | - | - | - | - | Turnover: 1.50, Cost: 0.143% |
| 2023-03-22 | - | Long: FET, ICP, NEAR, AAVE, Short: SOL, BTC, LTC, XRP | Rebalance | - | - | - | - | Turnover: 1.75, Cost: 0.166% |
| 2023-03-29 | - | Long: AAVE, BNB, UNI, SOL, Short: ADA, ZEC, XLM, XRP | Rebalance | - | - | - | - | Turnover: 1.50, Cost: 0.143% |
| 2023-04-05 | - | Long: XRP, FET, ICP, LINK, Short: CRV, AAVE, DOGE, INJ | Rebalance | - | - | - | - | Turnover: 2.00, Cost: 0.190% |
| 2023-04-12 | - | Long: DOGE, FET, UNI, ARB, Short: ICP, NEAR, SOL, INJ | Rebalance | - | - | - | - | Turnover: 1.50, Cost: 0.143% |
| 2023-04-19 | - | Long: CRV, AAVE, XLM, SOL, Short: FET, ICP, ARB, INJ | Rebalance | - | - | - | - | Turnover: 1.50, Cost: 0.143% |
| 2023-04-26 | - | Long: ICP, NEAR, DOGE, FET, Short: LTC, BTC, INJ, BNB | Rebalance | - | - | - | - | Turnover: 1.75, Cost: 0.166% |

### reversal_no_costs

**Strategy:** Cross-Sectional Reversal (no_costs)  
**Total Return:** -65.60%  
**CAGR:** -18.42%  
**Sharpe:** -0.72  
**Max Drawdown:** 81.51%  
**Profit Factor:** 0.89  
**Trades:** 189  
**Days:** 1321  

#### Year-by-Year Performance

| Year | Return | Sharpe | MDD | Trades |
|------|--------|--------|-----|--------|
| 2023 | 26.52% | 1.02 | 13.42% | 0 |
| 2024 | -55.00% | -1.86 | 56.59% | 0 |
| 2025 | -47.15% | -1.65 | 48.67% | 0 |
| 2026 | 14.32% | 0.65 | 24.13% | 0 |

#### Sample Trades (first 10)

| Entry Date | Exit Date | Pair/Assets | Direction | Entry Z/Signal | Exit Z/Signal | PnL Gross | PnL Net | Reason |
|------------|-----------|-------------|-----------|----------------|--------------|-----------|---------|--------|
| 2023-02-22 | - | Long: FET, LTC, ADA, AAVE, Short: XLM, ZEC, LINK, ICP | Rebalance | - | - | - | - | Turnover: 1.00, Cost: 0.000% |
| 2023-03-01 | - | Long: AVAX, ICP, SAND, NEAR, Short: INJ, ETH, LTC, FET | Rebalance | - | - | - | - | Turnover: 2.00, Cost: 0.000% |
| 2023-03-08 | - | Long: FET, INJ, NEAR, ZEC, Short: ETH, XLM, BNB, XRP | Rebalance | - | - | - | - | Turnover: 1.50, Cost: 0.000% |
| 2023-03-15 | - | Long: XRP, LTC, ZEC, UNI, Short: ETH, BTC, FET, INJ | Rebalance | - | - | - | - | Turnover: 1.50, Cost: 0.000% |
| 2023-03-22 | - | Long: FET, ICP, NEAR, AAVE, Short: SOL, BTC, LTC, XRP | Rebalance | - | - | - | - | Turnover: 1.75, Cost: 0.000% |
| 2023-03-29 | - | Long: AAVE, BNB, UNI, SOL, Short: ADA, ZEC, XLM, XRP | Rebalance | - | - | - | - | Turnover: 1.50, Cost: 0.000% |
| 2023-04-05 | - | Long: XRP, FET, ICP, LINK, Short: CRV, AAVE, DOGE, INJ | Rebalance | - | - | - | - | Turnover: 2.00, Cost: 0.000% |
| 2023-04-12 | - | Long: DOGE, FET, UNI, ARB, Short: ICP, NEAR, SOL, INJ | Rebalance | - | - | - | - | Turnover: 1.50, Cost: 0.000% |
| 2023-04-19 | - | Long: CRV, AAVE, XLM, SOL, Short: FET, ICP, ARB, INJ | Rebalance | - | - | - | - | Turnover: 1.50, Cost: 0.000% |
| 2023-04-26 | - | Long: ICP, NEAR, DOGE, FET, Short: LTC, BTC, INJ, BNB | Rebalance | - | - | - | - | Turnover: 1.75, Cost: 0.000% |

### funding

**ERROR:** Insufficient historical funding data (HL limitation)

### pairs_with_costs

**Strategy:** Pairs Mean Reversion (with_costs)  
**Total Return:** -88.92%  
**CAGR:** -22.21%  
**Sharpe:** -1.10  
**Max Drawdown:** 91.30%  
**Profit Factor:** 0.67  
**Trades:** 2792  
**Days:** 2208  

#### Per-Pair Trade Counts

- **BTC/ETH**: 358 entries, 358 exits
- **BTC/SOL**: 373 entries, 373 exits
- **ETH/SOL**: 414 entries, 414 exits
- **SOL/ARB**: 251 entries, 251 exits

#### Year-by-Year Performance

| Year | Return | Sharpe | MDD | Trades |
|------|--------|--------|-----|--------|
| 2020 | -15.42% | -1.79 | 22.57% | 0 |
| 2021 | -13.23% | -0.29 | 32.07% | 0 |
| 2022 | -52.80% | -1.52 | 53.59% | 0 |
| 2023 | -39.51% | -1.57 | 50.02% | 0 |
| 2024 | -35.44% | -1.93 | 37.07% | 0 |
| 2025 | -19.89% | -1.15 | 26.74% | 0 |
| 2026 | 2.22% | 0.21 | 19.89% | 0 |

#### Sample Trades (first 10)

| Entry Date | Exit Date | Pair/Assets | Direction | Entry Z/Signal | Exit Z/Signal | PnL Gross | PnL Net | Reason |
|------------|-----------|-------------|-----------|----------------|--------------|-----------|---------|--------|
| 2020-09-23 | 2020-09-24 | BTC/ETH | SHORT | 2.262 | 1.326 | 0.94% | 0.85% | zero_cross |
| 2020-10-14 | 2020-10-15 | ETH/SOL | SHORT | 2.011 | 1.959 | -0.38% | -0.48% | zero_cross |
| 2020-10-16 | 2020-10-17 | BTC/SOL | SHORT | 2.114 | 1.732 | 0.67% | 0.58% | zero_cross |
| 2020-10-19 | 2020-10-20 | BTC/SOL | SHORT | 2.173 | 2.583 | -2.45% | -2.54% | zero_cross |
| 2020-10-19 | 2020-10-20 | ETH/SOL | SHORT | 2.095 | 2.253 | -1.4% | -1.5% | zero_cross |
| 2020-10-20 | 2020-10-21 | BTC/ETH | SHORT | 3.581 | 3.837 | -0.35% | -0.45% | zero_cross |
| 2020-10-20 | 2020-10-21 | BTC/SOL | SHORT | 2.583 | 2.674 | -1.78% | -1.87% | zero_cross |
| 2020-10-20 | 2020-10-21 | ETH/SOL | SHORT | 2.253 | 2.356 | -1.43% | -1.52% | zero_cross |
| 2020-10-21 | 2020-10-22 | BTC/ETH | SHORT | 3.837 | 0.977 | 1.12% | 1.02% | zero_cross |
| 2020-10-21 | 2020-10-22 | BTC/SOL | SHORT | 2.674 | 2.205 | 0.55% | 0.46% | zero_cross |

### pairs_no_costs

**Strategy:** Pairs Mean Reversion (no_costs)  
**Total Return:** -57.99%  
**CAGR:** -9.42%  
**Sharpe:** -0.38  
**Max Drawdown:** 72.31%  
**Profit Factor:** 0.87  
**Trades:** 2792  
**Days:** 2208  

#### Per-Pair Trade Counts

- **BTC/ETH**: 358 entries, 358 exits
- **BTC/SOL**: 373 entries, 373 exits
- **ETH/SOL**: 414 entries, 414 exits
- **SOL/ARB**: 251 entries, 251 exits

#### Year-by-Year Performance

| Year | Return | Sharpe | MDD | Trades |
|------|--------|--------|-----|--------|
| 2020 | -10.16% | -1.14 | 18.71% | 0 |
| 2021 | 3.06% | 0.21 | 22.72% | 0 |
| 2022 | -41.45% | -1.05 | 43.02% | 0 |
| 2023 | -21.99% | -0.74 | 38.71% | 0 |
| 2024 | -18.67% | -0.90 | 23.43% | 0 |
| 2025 | 0.29% | 0.08 | 15.42% | 0 |
| 2026 | 21.78% | 1.43 | 7.52% | 0 |

#### Sample Trades (first 10)

| Entry Date | Exit Date | Pair/Assets | Direction | Entry Z/Signal | Exit Z/Signal | PnL Gross | PnL Net | Reason |
|------------|-----------|-------------|-----------|----------------|--------------|-----------|---------|--------|
| 2020-09-23 | 2020-09-24 | BTC/ETH | SHORT | 2.262 | 1.326 | 0.94% | 0.94% | zero_cross |
| 2020-10-14 | 2020-10-15 | ETH/SOL | SHORT | 2.011 | 1.959 | -0.38% | -0.38% | zero_cross |
| 2020-10-16 | 2020-10-17 | BTC/SOL | SHORT | 2.114 | 1.732 | 0.67% | 0.67% | zero_cross |
| 2020-10-19 | 2020-10-20 | BTC/SOL | SHORT | 2.173 | 2.583 | -2.45% | -2.45% | zero_cross |
| 2020-10-19 | 2020-10-20 | ETH/SOL | SHORT | 2.095 | 2.253 | -1.4% | -1.4% | zero_cross |
| 2020-10-20 | 2020-10-21 | BTC/ETH | SHORT | 3.581 | 3.837 | -0.35% | -0.35% | zero_cross |
| 2020-10-20 | 2020-10-21 | BTC/SOL | SHORT | 2.583 | 2.674 | -1.78% | -1.78% | zero_cross |
| 2020-10-20 | 2020-10-21 | ETH/SOL | SHORT | 2.253 | 2.356 | -1.43% | -1.43% | zero_cross |
| 2020-10-21 | 2020-10-22 | BTC/ETH | SHORT | 3.837 | 0.977 | 1.12% | 1.12% | zero_cross |
| 2020-10-21 | 2020-10-22 | BTC/SOL | SHORT | 2.674 | 2.205 | 0.55% | 0.55% | zero_cross |

---

## Specification Summary

1. **Cross-Sectional Reversal**: Weekly rebalance, point-in-time top-50 perps by trailing 30d volume, long bottom quintile (0.5 weight) / short top quintile (0.5 weight) of 7-day return, 7-day hold.

2. **Funding Cross-Section**: Weekly, long lowest-funding quintile / short highest-funding quintile (market-neutral), 7-day hold, paginated HL `fundingHistory`. ⚠️ Insufficient historical data (HL API limitation).

3. **Pairs Mean Reversion**: BTC/ETH, BTC/SOL, ETH/SOL, SOL/ARB log-spread z-score (30d lookback, enter |z|>2, exit z=0, stop |z|>4), 25% capital per pair (12.5% per leg, dollar neutral, 1:1 log hedge ratio, short spread when z>2, long spread when z<-2).

4. **GiiQ BO Proxy**: Long-only 20-day Donchian breakout, exit at 10-day low.

5. **Costs**: Taker 0.045% + slippage 0.05% per side per leg when applicable.

6. **Data**: Hyperliquid Public API `candleSnapshot` (up to 5000 candles per coin, startTime: 0).

---

*Report generated 2026-10-05 03:48:23 UTC*