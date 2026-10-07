#!/usr/bin/env python3
"""Dry-run pilot with REAL radar row shape (no prev fields, only dual_cross_up flag).

Tests that the 3-step rule works with production radar data structure:
- Real row shape: symbol, close, upper, lower, filter, trend, dual_cross_up, dual_cross_down, bar_time
- No prev_close or prev_upper fields (those were test-only)
- Uses dual_cross_up flag for step 1 and step 3 detection
- Step 1 accepts existing breakouts if still Green and above Upper
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

os.environ["EXEC_DRY_RUN"] = "1"
os.environ["PENDING_CONTINUATION_DISABLED"] = "0"

import pending_entries as pe
from exec_common import HKT

NOW = datetime.now(timezone.utc)
HKT_NOW = NOW.astimezone(HKT)

print(f"=== 3-Step Rule with REAL Radar Shape Pilot ===")
print(f"UTC: {NOW.isoformat()}")
print(f"HKT: {HKT_NOW.isoformat()}")
print()

def real_row(symbol, close, upper, lower, filter_val, trend, dual_cross_up):
    """Real production radar row shape (matches /api/ai/candidates DESK_DATA)."""
    return {
        "symbol": symbol,
        "close": close,
        "upper": upper,
        "lower": lower,
        "filter": filter_val,
        "trend": trend,
        "dual_cross_up": dual_cross_up,
        "dual_cross_down": False,
        "bar_time": int(NOW.timestamp() * 1000),
    }

# Test Case 1: TIA-like scenario (existing 1D breakout, waiting for 4H retrace)
print("Test 1: TIA (existing 1D breakout, price above Upper, Green)")
print("-" * 60)
row_1d_tia = real_row("TIA", close=7.5, upper=7.0, lower=6.5, filter_val=6.8, 
                      trend="Green", dual_cross_up=False)  # older breakout
row_4h_tia = real_row("TIA", close=7.4, upper=7.3, lower=7.0, filter_val=7.1, 
                      trend="Green", dual_cross_up=False)

bnd_tia = pe.band("CONT", row_1d_tia, row_4h_tia)
rec_tia = {
    "id": "TIA_CONT_20261007", "symbol": "TIA", "kind": "CONT", "status": "pending",
    "created_at": (NOW - timedelta(days=2)).isoformat(),
    "expires_at": (NOW + timedelta(days=5)).isoformat(),
    "breakout_1d": False,  # Not yet marked
    "retrace_touched": False,
    "last_retrace_bar_t": None,
}

action, reason, upd = pe.evaluate(rec_tia, bnd_tia, 7.45, NOW, set())
print(f"Action: {action}")
print(f"Reason: {reason}")
print(f"Updates: {json.dumps(upd, indent=2)}")
print()

if upd.get("breakout_1d"):
    print("✅ Step 1 PASSED: Accepted existing 1D breakout (close above Upper, Green)")
else:
    print("❌ Step 1 FAILED: Did not accept existing breakout")
    sys.exit(1)
print()

# Test Case 2: Fresh 1D cross detected via dual_cross_up flag
print("Test 2: Fresh 1D cross (dual_cross_up=True)")
print("-" * 60)
row_1d_fresh = real_row("BTC", close=45000, upper=44000, lower=42000, filter_val=43000,
                        trend="Green", dual_cross_up=True)  # Fresh cross
row_4h_fresh = real_row("BTC", close=44500, upper=44000, lower=43000, filter_val=43500,
                        trend="Green", dual_cross_up=False)

bnd_fresh = pe.band("CONT", row_1d_fresh, row_4h_fresh)
rec_fresh = {
    "id": "BTC_CONT_20261007", "symbol": "BTC", "kind": "CONT", "status": "pending",
    "created_at": (NOW - timedelta(days=1)).isoformat(),
    "expires_at": (NOW + timedelta(days=6)).isoformat(),
    "breakout_1d": False,
    "retrace_touched": False,
    "last_retrace_bar_t": None,
}

action, reason, upd = pe.evaluate(rec_fresh, bnd_fresh, 44800, NOW, set())
print(f"Action: {action}")
print(f"Reason: {reason}")
print(f"Updates: {json.dumps(upd, indent=2)}")
print()

if upd.get("breakout_1d"):
    print("✅ Step 1 PASSED: Detected fresh 1D cross via dual_cross_up flag")
else:
    print("❌ Step 1 FAILED: Did not detect fresh cross")
    sys.exit(1)
print()

# Test Case 3: 4H retrace detected
print("Test 3: 4H retrace (close <= Filter)")
print("-" * 60)
row_1d_retrace = real_row("ETH", close=2500, upper=2400, lower=2200, filter_val=2300,
                          trend="Green", dual_cross_up=False)
row_4h_retrace = real_row("ETH", close=2350, upper=2400, lower=2300, filter_val=2350,
                          trend="Green", dual_cross_up=False)

bnd_retrace = pe.band("CONT", row_1d_retrace, row_4h_retrace)
rec_retrace = {
    "id": "ETH_CONT_20261007", "symbol": "ETH", "kind": "CONT", "status": "pending",
    "created_at": (NOW - timedelta(days=3)).isoformat(),
    "expires_at": (NOW + timedelta(days=4)).isoformat(),
    "breakout_1d": True,  # Already completed step 1
    "breakout_date_1d": "2026-10-04",
    "retrace_touched": False,
    "last_retrace_bar_t": None,
}

action, reason, upd = pe.evaluate(rec_retrace, bnd_retrace, 2360, NOW, set())
print(f"Action: {action}")
print(f"Reason: {reason}")
print(f"Updates: {json.dumps(upd, indent=2)}")
print()

if upd.get("retrace_touched"):
    print("✅ Step 2 PASSED: Detected 4H retrace")
else:
    print("❌ Step 2 FAILED: Did not detect retrace")
    sys.exit(1)
print()

# Test Case 4: 4H breakout trigger via dual_cross_up
print("Test 4: 4H breakout trigger (dual_cross_up=True)")
print("-" * 60)
row_1d_trigger = real_row("SOL", close=150, upper=145, lower=140, filter_val=142,
                          trend="Green", dual_cross_up=False)
row_4h_trigger = real_row("SOL", close=148, upper=145, lower=142, filter_val=143,
                          trend="Green", dual_cross_up=True)  # Fresh 4H cross

bnd_trigger = pe.band("CONT", row_1d_trigger, row_4h_trigger)
rec_trigger = {
    "id": "SOL_CONT_20261007", "symbol": "SOL", "kind": "CONT", "status": "pending",
    "created_at": (NOW - timedelta(days=4)).isoformat(),
    "expires_at": (NOW + timedelta(days=3)).isoformat(),
    "breakout_1d": True,
    "breakout_date_1d": "2026-10-03",
    "retrace_touched": True,
    "last_retrace_bar_t": int((NOW - timedelta(hours=8)).timestamp() * 1000),
}

action, reason, upd = pe.evaluate(rec_trigger, bnd_trigger, 147, NOW, set())
print(f"Action: {action}")
print(f"Reason: {reason}")
print(f"Updates: {json.dumps(upd, indent=2)}")
print()

if action == "trigger":
    print("✅ Step 3 PASSED: 4H breakout triggered via dual_cross_up flag")
else:
    print(f"❌ Step 3 FAILED: Expected trigger, got {action}")
    sys.exit(1)
print()

# Test Case 5: No cross (dual_cross_up=False) waits
print("Test 5: No cross detected (dual_cross_up=False, close below Upper)")
print("-" * 60)
row_1d_wait = real_row("ADA", close=0.35, upper=0.38, lower=0.32, filter_val=0.34,
                       trend="Green", dual_cross_up=False)
row_4h_wait = real_row("ADA", close=0.36, upper=0.38, lower=0.33, filter_val=0.34,
                       trend="Green", dual_cross_up=False)

bnd_wait = pe.band("CONT", row_1d_wait, row_4h_wait)
rec_wait = {
    "id": "ADA_CONT_20261007", "symbol": "ADA", "kind": "CONT", "status": "pending",
    "created_at": (NOW - timedelta(days=1)).isoformat(),
    "expires_at": (NOW + timedelta(days=6)).isoformat(),
    "breakout_1d": False,
    "retrace_touched": False,
    "last_retrace_bar_t": None,
}

action, reason, upd = pe.evaluate(rec_wait, bnd_wait, 0.36, NOW, set())
print(f"Action: {action}")
print(f"Reason: {reason}")
print(f"Updates: {json.dumps(upd, indent=2)}")
print()

if action == "wait" and not upd:
    print("✅ Correctly waits when no cross detected")
else:
    print(f"❌ Expected wait with no updates, got action={action}, upd={upd}")
    sys.exit(1)
print()

# Summary
print("=" * 60)
print("✅ ALL TESTS PASSED")
print()
print("Summary:")
print("  1. Step 1 accepts existing 1D breakout (price > Upper, Green) ✅")
print("  2. Step 1 detects fresh 1D cross via dual_cross_up flag ✅")
print("  3. Step 2 detects 4H retrace (close <= Filter) ✅")
print("  4. Step 3 triggers on 4H breakout via dual_cross_up flag ✅")
print("  5. Correctly waits when no cross detected ✅")
print()
print("Current symbols that would arm/trigger:")
print("  - TIA: Would arm step 1 (price 7.5 > Upper 7.0, Green)")
print("  - Any symbol with dual_cross_up=True: Would detect fresh cross")
print()
print("Real radar shape works correctly (no prev fields needed)!")
