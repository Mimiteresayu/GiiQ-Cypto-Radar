# Tier exits LOCKED (no Armed, no 組)

| Tier | Mcap | Primary | Hard SL |
|------|------|---------|---------|
| Mega | ≥$50B | 4H close < 4H Filter | **4H Lower** (period 72) |
| Large | $2B–<$50B | 4H close < 4H Filter | **4H Lower** (period 72) |
| Small | $200M–<$2B | 1H close < 1H Lower | 4H Filter (mid, period 72) |
| Tiny | <$200M | 1H close < 1H Lower | 4H Filter (mid, period 72) |

Hard SL = reduce-only stop-market trigger; code SoT: `exec_common.hard_sl_for_tier`
(supersedes the 2026-09-21 "4H Filter for all tiers" note; updated 2026-09-27).

Size: P 4–8% · P+N 8–12% · P+CR 8–12% · P+N+CR 10–15% · Lev 1–5 · Liq < Hard SL.
Universe expanded: MAX~280, dayNtl≥$75k.
