# Complete Fix Plan for scout/diversify

## Summary
The backtest.py file is in a mixed state after partial edits. Need clean comprehensive fixes.

## Critical Bugs to Fix

### 1. ✅ DONE: Funding Pagination
- Fixed: Paginate HL fundingHistory (max 500 records/call)
- Location: get_funding_history() function

### 2. 🔴 CRITICAL: Pairs Strategy Complete Rewrite
**Current Issues:**
- Sizing: Wrong (was 50% per pair × 4 = 200% gross)
- Position tracking: Mixed old float/new dict formats
- Missing detailed trade logging

**Required Implementation:**
```python
# Correct sizing:
CAPITAL_PER_PAIR = 0.25  # 4 pairs × 25% = 100%
LEG_SIZE = 0.125  # 12.5% per leg

# Correct spread direction:
if z > 2: entry_dir = -1  # SHORT spread (mean revert down)
if z < -2: entry_dir = 1  # LONG spread (mean revert up)

# Position tracking:
positions[pair] = {
    'dir': entry_dir,  # ±1
    'entry_day': day,
    'entry_z': z,
    'entry_price1': price1,
    'entry_price2': price2,
    'entry_cost': cost
}

# Daily P&L:
spread_prev = log(p1_prev) - log(p2_prev)
spread_curr = log(p1_curr) - log(p2_curr)
pnl = pos['dir'] * (spread_curr - spread_prev) * CAPITAL_PER_PAIR
```

**Solution:** Use pairs_fixed.py as template, integrate into backtest.py

### 3. 🔴 CRITICAL: Reversal Strategy Fixes
**Issues:**
- Universe: Static top-50, should be point-in-time by trailing 30d volume
- History: Starts 2023, should start 2020
- Sizing: Unclear, likely wrong (explains -84% year)

**Required:**
```python
# Point-in-time universe at each rebalance day:
def get_universe_at_date(day, price_data, volume_window=30):
    # Calculate trailing 30d volume for each coin
    # Return top 50 by volume
    pass

# Correct sizing:
NET_EXPOSURE = 1.0  # 100% net (50% long, 50% short)
long_coins = bottom_quintile  # 20% of universe
short_coins = top_quintile

weight_per_coin = 0.5 / len(long_coins)  # Equal weight within each side
```

### 4. ⏳ TODO: Add Sanity Test
```python
def sanity_test_buy_hold_btc(price_data):
    """Verify PnL accounting by matching BTC's actual return"""
    btc_prices = price_data['BTC']
    days = sorted(btc_prices.keys())
    
    equity = {days[0]: 1.0}
    for i in range(1, len(days)):
        ret = (btc_prices[days[i]] / btc_prices[days[i-1]]) - 1
        equity[days[i]] = equity[days[i-1]] * (1 + ret)
    
    total_return = equity[days[-1]] - 1
    expected_return = (btc_prices[days[-1]] / btc_prices[days[0]]) - 1
    
    assert abs(total_return - expected_return) < 0.0001, "PnL accounting broken!"
    return {
        'calculated': total_return,
        'expected': expected_return,
        'match': 'PASS'
    }
```

### 5. ⏳ TODO: Run With/Without Costs
- Add `with_costs` parameter to all strategies
- Run each strategy twice in main()
- Report both versions in output

### 6. ⏳ TODO: Enhanced Reporting
- 10 sample trades with full details for pairs
- Cost breakdown (gross PnL vs net PnL)
- Per-year metrics for all strategies

## Implementation Order
1. Create clean pairs strategy (use pairs_fixed.py)
2. Fix reversal strategy universe and sizing
3. Add sanity test
4. Update main() to run with/without costs
5. Test locally with short lookback
6. Push and run in GitHub Actions
7. Commit results to scout-out branch

## Files to Modify
- backtest.py (main fixes)
- generate_report.py (add detailed trades section)

## Testing Strategy
1. Local test with 90-day lookback
2. Verify sanity test passes
3. Check sample trades make sense
4. Push to GitHub Actions
5. Review full results

