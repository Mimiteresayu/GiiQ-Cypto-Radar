# Scout Diversify

Automated strategy discovery and backtesting system for non-trend crypto strategies with low correlation to GiiQ BO (daily channel-breakout trend strategy).

## Overview

This system searches for and backtests mean-reversion, cross-sectional, funding-rate, pairs trading, and basis strategies that can diversify the GiiQ portfolio.

## Components

### 1. `search.py`
Discovers strategies via GitHub, arXiv, and SSRN APIs.

**Usage:**
```bash
python3 search.py --token <GITHUB_TOKEN> --top-n 10 --output out/search_results.json
```

**Searches for:**
- Mean-reversion strategies
- Cross-sectional reversal
- Funding-rate cross-section
- Pairs/statistical arbitrage
- Basis trading

**Output:** `out/search_results.json` with repos, papers, stars, backtest mentions

### 2. `backtest.py`
Backtests three fixed-spec strategies using Hyperliquid public API.

**Usage:**
```bash
python3 backtest.py --output out/backtest_results.json --days 730
```

**Strategies (fixed specs, no parameter tuning):**

**Strategy 1: Cross-Sectional Short-Term Reversal**
- Weekly rebalance
- Long bottom quintile / short top quintile of 7-day return
- Universe: top-50 liquid perps
- Hold: 7 days

**Strategy 2: Funding-Rate Cross-Section**
- Weekly rebalance
- Long lowest-funding / short highest-funding quintile (30-day avg)
- Market-neutral
- Hold: 7 days

**Strategy 3: Pairs Mean Reversion**
- Pairs: BTC/ETH, BTC/SOL, ETH/SOL, SOL/ARB
- 30-day z-score lookback
- Enter: |z| > 2
- Exit: z crosses 0
- Stop: |z| > 4

**Costs:** 0.045% taker + 0.05% slippage per side, funding included for strategy 2

**Metrics:** PF, CAGR, Sharpe, MDD, trades, year-by-year split, IS (≤2023) / OOS (2024–2026) split

**Output:** `out/backtest_results.json`

### 3. `generate_report.py`
Generates markdown summary report.

**Usage:**
```bash
python3 generate_report.py
```

**Output:** `out/diversify_latest.md` (1-page summary with metrics table, year splits, top search results)

## GitHub Actions Workflow

`.github/workflows/scout_diversify.yml`

- **Trigger:** Manual only (`workflow_dispatch`)
- **Timeout:** 30 minutes
- **Runs:** search.py → backtest.py → generate_report.py
- **Outputs:** Committed to `scout-out` branch (never main)
- **Dependencies:** Only stdlib + `requests`

**To run:**
1. Go to Actions tab in GitHub
2. Select "Scout Diversify" workflow
3. Click "Run workflow"
4. Check results in `scout-out` branch under `scout/diversify/out/`

## Data Sources

- **Primary:** Hyperliquid public info API
  - `candleSnapshot` (daily candles)
  - `metaAndAssetCtxs` (universe, current funding)
  - Note: Historical funding rates require user address; using current predicted funding as proxy

- **Future enhancement:** Binance data archive at data.binance.vision
  - Longer history
  - Delisted coins (survivorship bias mitigation)

## Constraints

✅ Only touches `scout/diversify/` and `.github/workflows/scout_diversify.yml`  
✅ Never touches main, Railway, cockpit, or bx-exec  
✅ No orders, keys, or deposits  
✅ GitHub runners geo-blocked from Binance/Bybit/OKX → uses HL public API  
✅ Manual trigger only

## Local Testing

```bash
# Search (needs GITHUB_TOKEN)
export GITHUB_TOKEN=ghp_...
python3 search.py --token $GITHUB_TOKEN --top-n 5

# Backtest (no auth needed)
python3 backtest.py --days 180

# Generate report
python3 generate_report.py

# View report
cat out/diversify_latest.md
```

## Next Steps

1. Trigger workflow manually (730-day backtest recommended)
2. Review results in `scout-out` branch
3. Extend with Binance data archive for longer history
4. Compute actual correlation with GiiQ BO daily returns
5. Select 1–2 candidates for Railway deployment

## Notes

- **Data limitation:** Hyperliquid public API provides limited history (~2 years for most coins)
- **Funding limitation:** Current predicted funding used as proxy; historical user-specific funding requires address
- **Year labels:** Approximate from day index; actual dates depend on start date
- **Correlation:** Requires GiiQ BO daily returns for proper calculation (not yet implemented)
- **No parameter tuning:** All specs are fixed to avoid overfitting; this is a screening tool, not optimization
