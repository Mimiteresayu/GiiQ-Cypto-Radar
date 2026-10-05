# GiiQ Diversification Strategy Report

**Generated:** 2026-10-05 03:23 UTC

**Objective:** Identify 1–2 non-trend crypto strategies with low correlation to GiiQ BO (daily channel-breakout trend strategy).

## Data Limitations

- **Source:** Hyperliquid public API
- **Lookback:** 90 days
- **Universe:** 50 liquid perpetuals
- **Limitations:** Limited history available via public API; full history with delisted coins requires Binance data archive

## Backtest Results

| Strategy | PF | CAGR | Sharpe | MDD | Trades | IS Return | OOS Return |
|----------|-----|------|--------|-----|--------|-----------|------------|
| 1. Cross-Sectional Reversal | 1.36 | 108.78% | 1.88 | 13.33% | 12 | 27.44% | N/A |
| 2. Funding Cross-Section | 1.51 | 144.29% | 2.48 | 19.77% | 1 | 23.70% | N/A |
| 3. Pairs Mean Reversion | 3.16 | 448.51% | 3.55 | 10.90% | 74 | 50.98% | N/A |

**Costs:** 0.045% taker fee + 0.05% slippage per side; funding included for strategy 2.

### 1. Cross-Sectional Reversal

**Spec:** Weekly rebalance, long bottom quintile / short top quintile of 7-day return, top-50 liquid perps, 7-day hold.

- **Total Return:** 27.44%
- **CAGR:** 108.78%
- **Sharpe Ratio:** 1.88
- **Max Drawdown:** 13.33%
- **Profit Factor:** 1.36
- **Number of Trades:** 12
- **Days Traded:** 83

**Year-by-Year Returns:**

- 2020: 27.44%

**In-Sample (≤2023) vs Out-of-Sample (2024-2026):**

- IS Return: 27.44%
- OOS Return: N/A

**Correlations:**

- vs GiiQ BO: Not calculated (requires GiiQ BO proxy returns)
- vs BTC: Not calculated (requires aligned BTC returns)

### 2. Funding Cross-Section

**Spec:** Weekly rebalance, long lowest-funding / short highest-funding quintile (30-day avg), market-neutral, 7-day hold.

- **Total Return:** 23.70%
- **CAGR:** 144.29%
- **Sharpe Ratio:** 2.48
- **Max Drawdown:** 19.77%
- **Profit Factor:** 1.51
- **Number of Trades:** 1
- **Days Traded:** 60

**Year-by-Year Returns:**

- 2020: 23.70%

**In-Sample (≤2023) vs Out-of-Sample (2024-2026):**

- IS Return: 23.70%
- OOS Return: N/A

**Correlations:**

- vs GiiQ BO: Not calculated (requires GiiQ BO proxy returns)
- vs BTC: Not calculated (requires aligned BTC returns)

### 3. Pairs Mean Reversion

**Spec:** BTC/ETH, BTC/SOL, ETH/SOL, SOL/ARB pairs, 30-day z-score lookback, enter |z|>2, exit z=0, stop |z|>4.

- **Total Return:** 50.98%
- **CAGR:** 448.51%
- **Sharpe Ratio:** 3.55
- **Max Drawdown:** 10.90%
- **Profit Factor:** 3.16
- **Number of Trades:** 74
- **Days Traded:** 61

**Year-by-Year Returns:**

- 2020: 50.98%

**In-Sample (≤2023) vs Out-of-Sample (2024-2026):**

- IS Return: 50.98%
- OOS Return: N/A

**Correlations:**

- vs GiiQ BO: Not calculated (requires GiiQ BO proxy returns)
- vs BTC: Not calculated (requires aligned BTC returns)

## Strategy Discovery

---

**Next Steps:**

1. Review top strategies for low correlation with GiiQ BO
2. Extend backtests with Binance data archive for longer history + delisted coins
3. Compute actual correlation with GiiQ BO daily returns
4. Select 1–2 candidates for Railway deployment
