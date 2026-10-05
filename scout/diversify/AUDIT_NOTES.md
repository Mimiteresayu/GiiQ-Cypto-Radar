# Backtest Audit Notes

## Issues Found

### 1. Funding Pagination
**Bug:** HL `fundingHistory` returns max 500 records per call
**Fix:** Paginate by advancing startTime to last record + 1ms until end_ms
**Status:** ✅ FIXED (committed f2d3cc2)

### 2. Pairs Strategy - Multiple Bugs
**Bug a:** Sizing incorrect - was using MAX_POSITION_PER_PAIR (50%) per pair with 4 pairs = 200% gross
**Fix:** 25% per pair, 12.5% per leg, dollar-neutral
**Status:** 🔄 IN PROGRESS

**Bug b:** Spread direction unclear
**Fix:** SHORT spread when z>2 (sell expensive coin1, buy cheap coin2)
       LONG spread when z<-2 (buy cheap coin1, sell expensive coin2)
**Status:** 🔄 IN PROGRESS

**Bug c:** Hedge ratio not specified
**Fix:** Use 1:1 in log space (log-spread z-score)
**Status:** ✅ IMPLEMENTED in pairs_fixed.py

**Bug d:** Look-ahead check needed
**Fix:** z computed at EOD including today's price, filled at today's close = OK
**Status:** ✅ VERIFIED

**Bug e:** Entry/exit counting
**Fix:** Track separately in detailed_trades
**Status:** 🔄 IN PROGRESS

### 3. Reversal Strategy - Multiple Bugs
**Bug:** Year table starts 2023, IS only 313 days (should start 2020)
**Fix:** Point-in-time universe (top-50 by trailing 30d volume at each date)
**Status:** ⏳ TODO

**Bug:** Position sizing unclear, -84% year suggests leverage/sign error  
**Fix:** Market-neutral, 0.5 long + 0.5 short, equal weight within each
**Status:** ⏳ TODO

### 4. Missing Features
- Cost-free versions for both strategies
- Sample trades with details (10 trades)
- Sanity test (buy & hold BTC)

## Next Steps
1. Complete pairs_fixed.py integration into backtest.py
2. Fix reversal strategy
3. Add sanity test
4. Run with/without costs
5. Test in Actions

