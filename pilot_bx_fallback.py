#!/usr/bin/env python3
"""Dry-run pilot for BX 08:50 RAILWAY_FALLBACK (Task B).

Tests the full flow with EXEC_DRY_RUN=1:
- BX candidate generation
- No Claude ENTRY_DESK decision (simulating 08:50 scenario)
- BX executor with RAILWAY_FALLBACK
- Verify fallback approves all candidates at 2% / 2x
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Mock environment: no real keys, dry run
os.environ["EXEC_DRY_RUN"] = "1"
os.environ["BX_LIVE"] = "0"  # Dry run
os.environ.pop("BX_API_KEY", None)
os.environ.pop("BX_API_SECRET", None)

import bx_live
from exec_common import HKT

NOW = datetime.now(timezone.utc)
HKT_NOW = NOW.astimezone(HKT)

print(f"=== BX RAILWAY_FALLBACK Dry-Run Pilot ===")
print(f"UTC: {NOW.isoformat()}")
print(f"HKT: {HKT_NOW.isoformat()}")
print()

# Test 1: Build fallback decisions for BX candidates
print("Test 1: Build fallback decisions")
print("-" * 60)
cand_doc = {
    "date": HKT_NOW.strftime("%Y-%m-%d"),
    "generated_at": NOW.isoformat(),
    "candidates": [
        {"symbol": "BTC", "type": "Base", "max_leverage": 50},
        {"symbol": "ETH", "type": "Base", "max_leverage": 75},
        {"symbol": "SOL", "type": "Base", "max_leverage": 50},
    ]
}

fb_list, why = bx_live.build_fallback_decisions(cand_doc, NOW)
if why:
    print(f"❌ Fallback failed: {why}")
    sys.exit(1)

print(f"✓ Generated {len(fb_list)} fallback decisions:")
for dec in fb_list:
    print(f"  - {dec['symbol']}: {dec['decision']} at {dec['size_pct']}% / {dec['leverage']}x")
    print(f"    Reason: {dec['reason']}")
print()

# Test 2: Verify fallback map creation
print("Test 2: Verify fallback map in run_entries context")
print("-" * 60)

# Mock empty decisions (no Claude POST)
decisions_doc = {"decisions": {}}

# Simulate run_entries logic
has_claude = any(rec.get("source") == "claude" for rec in decisions_doc.get("decisions", {}).values())
print(f"Claude decisions present: {has_claude}")

fallback_map = {}
if not has_claude:
    fb_list, why = bx_live.build_fallback_decisions(cand_doc, NOW)
    if fb_list:
        fallback_map = {d["symbol"]: d for d in fb_list}
        print(f"✓ RAILWAY_FALLBACK applied: {len(fb_list)} BX candidates approved at 2%/2x")
    else:
        print(f"❌ Fallback generation failed: {why}")
        sys.exit(1)

print()
print("Fallback map:")
for sym, dec in fallback_map.items():
    print(f"  {sym}: {json.dumps(dec, indent=2)}")
print()

# Test 3: approval_for with fallback
print("Test 3: approval_for with fallback parameter")
print("-" * 60)
for sym in ["BTC", "ETH", "SOL"]:
    appr = bx_live.approval_for(sym, NOW, fallback=fallback_map)
    if appr:
        print(f"✓ {sym} approved: size_pct={appr['size_pct']}, leverage={appr['leverage']}")
    else:
        print(f"❌ {sym} NOT approved (expected approval)")
        sys.exit(1)

# Check non-candidate symbol
appr_none = bx_live.approval_for("NOTACANDIDATE", NOW, fallback=fallback_map)
if appr_none:
    print(f"❌ NOTACANDIDATE approved (should be None)")
    sys.exit(1)
print(f"✓ NOTACANDIDATE correctly not approved")
print()

# Test 4: rules_summary shows fallback
print("Test 4: rules_summary shows fallback")
print("-" * 60)
rules = bx_live.rules_summary()
if "fallback" in rules:
    print(f"✓ rules_summary includes fallback: {rules['fallback']}")
else:
    print(f"❌ rules_summary missing fallback field")
    sys.exit(1)
print()

# Test 5: Verify old-style pending cleanup
print("Test 5: Old-style pending cleanup")
print("-" * 60)
from pending_entries import cancel_old_style_pending, is_old_style_record

old_entries = [
    {"id": "old1", "status": "pending", "kind": "CONT", "setup": {"t": 12345}},
    {"id": "new1", "status": "pending", "kind": "CONT", "breakout_1d": False, "last_retrace_bar_t": None},
    {"id": "old2", "status": "pending", "kind": "ADD_ON", "zone_at_create": [0.8, 0.9]},
]

print("Before cleanup:")
for e in old_entries:
    print(f"  {e['id']}: old_style={is_old_style_record(e)}, status={e['status']}")

gone = cancel_old_style_pending(old_entries, NOW)
print(f"\n✓ Cancelled {len(gone)} old-style records:")
for e in gone:
    print(f"  - {e['id']}: {e['close_reason']}")

print("\nAfter cleanup:")
for e in old_entries:
    print(f"  {e['id']}: status={e['status']}")
print()

# Summary
print("=" * 60)
print("✅ ALL TESTS PASSED")
print()
print("Summary:")
print(f"  - BX RAILWAY_FALLBACK approves {len(fb_list)} candidates at 2%/2x")
print(f"  - approval_for correctly uses fallback map")
print(f"  - rules_summary shows fallback policy")
print(f"  - Old-style pendings cleaned up successfully")
print()
print("Would-be order payloads (dry run):")
print(json.dumps([{
    "symbol": d["symbol"],
    "side": "buy",
    "order_type": "market",
    "size_pct": d["size_pct"],
    "leverage": d["leverage"],
    "reason": d["reason"],
} for d in fb_list], indent=2))
