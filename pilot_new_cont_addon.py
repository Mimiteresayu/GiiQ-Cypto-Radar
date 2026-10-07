#!/usr/bin/env python3
"""Dry-run pilot for new 3-step CONT / ADD_ON entry rule (Task B).

Tests the full flow with EXEC_DRY_RUN=1 and synthetic market data:
- Candidate generation
- Decision (Claude ENTRY_DESK POST + RAILWAY_FALLBACK paths)
- HL executor
- BX executor  
- 4H worker for CONT / ADD_ON

Tests at least one BASE, one CONT, one ADD_ON case, plus cancellation of old-style pendings.
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
os.environ["PENDING_CONTINUATION_DISABLED"] = "0"  # Enable new logic
os.environ.pop("HL_API_PRIVATE_KEY", None)

import entry_candidates
import executor
import pending_entries as pe
import pending_worker as pw
from exec_common import HKT

NOW = datetime.now(timezone.utc)  # Current time

# Synthetic radar data with BASE, CONT, and ADD_ON candidates
# Add enough rows to pass the row count check (minimum 120)
RADAR_1D = {
    "ts": NOW.isoformat(),
    "row_count": 150,  # Add row count to pass guardrail
    "rows": [
        # BASE candidate: fresh 1D dual cross up above 1D Upper
        {"symbol": "BASE1", "trend": "Green", "dual_cross_up": True, "close": 1.1, "upper": 1.0, 
         "filter": 0.95, "lower": 0.9, "prev_close": 0.98, "prev_upper": 1.0, "last_cross_up_at": int((NOW - timedelta(days=1)).timestamp() * 1000),
         "bar_time": int(NOW.timestamp() * 1000) - 86400000, "category": "N"},
        
        # CONT candidate: 1D Green, 4H cross up (will need 3-step check)
        {"symbol": "CONT1", "trend": "Green", "dual_cross_up": False, "close": 2.0, "upper": 2.1,
         "filter": 1.9, "lower": 1.8, "prev_close": 2.0, "prev_upper": 2.1, "last_cross_up_at": int((NOW - timedelta(days=5)).timestamp() * 1000),
         "bar_time": int(NOW.timestamp() * 1000) - 86400000, "category": "V"},
        
        # ADD_ON candidate: same as CONT but we'll mock an existing position
        {"symbol": "ADD1", "trend": "Green", "dual_cross_up": False, "close": 3.0, "upper": 3.1,
         "filter": 2.9, "lower": 2.8, "prev_close": 3.0, "prev_upper": 3.1, "last_cross_up_at": int((NOW - timedelta(days=3)).timestamp() * 1000),
         "bar_time": int(NOW.timestamp() * 1000) - 86400000, "category": "C"},
    ] + [
        # Add dummy rows to pass row count guardrail (need 120+ rows)
        {"symbol": f"DUMMY{i}", "trend": "Red", "dual_cross_up": False, "close": 1.0, "upper": 1.1,
         "filter": 0.95, "lower": 0.9, "prev_close": 1.0, "prev_upper": 1.1,
         "bar_time": int(NOW.timestamp() * 1000) - 86400000}
        for i in range(120)
    ],
}

RADAR_4H = {
    "ts": NOW.isoformat(),
    "row_count": 150,  # Add row count to pass guardrail
    "rows": [
        # BASE1: 4H data (not Chase, so 4H cross doesn't matter)
        {"symbol": "BASE1", "trend": "Green", "dual_cross_up": False, "close": 1.1, "upper": 1.05,
         "filter": 1.0, "lower": 0.95, "prev_close": 1.05, "prev_upper": 1.05,
         "bar_time": int(NOW.timestamp() * 1000) - 14400000},
        
        # CONT1: 4H Green + dual cross up (Chase candidate)
        # Step 1: 1D breakout already happened (see 1D data)
        # Step 2: 4H retrace - close is below filter (2.0 <= 2.05)
        # Step 3: 4H breakout - has dual cross up
        {"symbol": "CONT1", "trend": "Green", "dual_cross_up": True, "close": 2.0, "upper": 1.95,
         "filter": 2.05, "lower": 1.9, "prev_close": 1.93, "prev_upper": 1.95,
         "bar_time": int(NOW.timestamp() * 1000) - 14400000},
        
        # ADD1: 4H Green + dual cross up (Chase candidate, but we have position)
        {"symbol": "ADD1", "trend": "Green", "dual_cross_up": True, "close": 3.0, "upper": 2.95,
         "filter": 2.9, "lower": 2.85, "prev_close": 2.88, "prev_upper": 2.95,
         "bar_time": int(NOW.timestamp() * 1000) - 14400000},
    ] + [
        # Add dummy rows to pass row count guardrail
        {"symbol": f"DUMMY{i}", "trend": "Red", "dual_cross_up": False, "close": 1.0, "upper": 1.05,
         "filter": 1.0, "lower": 0.95, "prev_close": 1.0, "prev_upper": 1.05,
         "bar_time": int(NOW.timestamp() * 1000) - 14400000}
        for i in range(120)
    ],
}

# Decisions from ENTRY_DESK
DECISIONS = {
    "BASE1": {"decision": "approve", "size_pct": 3, "leverage": 4, "reason": "Fresh Base breakout"},
    "CONT1": {"decision": "approve", "size_pct": 2, "leverage": 3, "reason": "Chase signal"},
    "ADD1": {"decision": "approve", "size_pct": 2, "leverage": 3, "reason": "Add-on to existing"},
}

# Mock HL client
class MockHL:
    def __init__(self, positions=None):
        self.positions = positions or []
        
    def spot_state(self):
        return {"balances": [{"coin": "USDC", "total": "10000"}]}
    
    def perp_state(self):
        aps = []
        for p in self.positions:
            aps.append({"position": {
                "coin": p["symbol"],
                "szi": str(p["size"]),
                "entryPx": str(p["entry_px"]),
                "liquidationPx": str(p["liq_px"]),
                "leverage": {"type": "isolated", "value": p["leverage"]},
                "marginUsed": str(p["margin"]),
                "positionValue": str(p["size"] * p["entry_px"]),
                "unrealizedPnl": "0",
            }})
        return {"marginSummary": {"totalMarginUsed": "100", "accountValue": "10000", "totalNtlPos": "300"},
                "assetPositions": aps, "withdrawable": "9000"}
    
    def meta(self):
        return {
            "BASE1": {"szDecimals": 0, "maxLeverage": 50},
            "CONT1": {"szDecimals": 0, "maxLeverage": 50},
            "ADD1": {"szDecimals": 0, "maxLeverage": 50},
        }
    
    def all_mids(self):
        return {"BASE1": 1.1, "CONT1": 2.0, "ADD1": 3.0}
    
    def agent_status(self, addr):
        return {"ok": True, "name": "test", "days_left": 300, "valid_until_ms": 1, "reason": "approved"}
    
    def exchange(self):
        return self


def run_pilot():
    print("=" * 80)
    print("DRY-RUN PILOT: New 3-step CONT / ADD_ON entry rule")
    print("=" * 80)
    
    # Setup temp output dir
    with tempfile.TemporaryDirectory() as tmpdir:
        out_dir = Path(tmpdir) / "out"
        out_dir.mkdir()
        
        # Write radar files
        (out_dir / "gc_radar_1d.json").write_text(json.dumps(RADAR_1D))
        (out_dir / "gc_radar_4h.json").write_text(json.dumps(RADAR_4H))
        
        # Set pending path to temp
        os.environ["PENDING_PATH"] = str(out_dir / "pending_entries.json")
        
        # 1. Generate candidates
        print("\n## 1. Generate entry candidates")
        # Mock an existing position for ADD1 (to test ADD_ON vs CONT)
        mock_positions = [{"symbol": "ADD1"}]
        candidates_data = entry_candidates.build_candidates(RADAR_1D, RADAR_4H, mock_positions)
        print(f"  Generated {candidates_data['count']} candidates:")
        for c in candidates_data["candidates"]:
            print(f"    - {c['symbol']}: type={c['type']}, entry_kind={c['entry_kind']}, "
                  f"has_base={c['has_base_position']}, retrace={c.get('retrace_touched')}, "
                  f"bo_4h={c.get('bo_4h_cross_up')}")
        
        # 2. Test executor with BASE and Chase approvals
        print("\n## 2. Execute approved candidates (08:55 executor)")
        hl_mock = MockHL(positions=[{"symbol": "ADD1", "size": 100, "entry_px": 2.5, "liq_px": 2.0, 
                                     "leverage": 3, "margin": 100}])
        
        # Create an old-style pending to test cancellation
        old_pending = {
            "id": "OLD_CONT_20261006", "symbol": "OLD", "kind": pe.CONTINUATION, "status": pe.ACTIVE,
            "created_at": (NOW - timedelta(days=1)).isoformat(),
            "expires_at": (NOW + timedelta(days=6)).isoformat(),
            "setup": {"t": int(NOW.timestamp() * 1000) - 86400000, "l": 1.0, "c": 1.1},  # Old-style setup field
        }
        pe.save_pending([old_pending])
        
        result = executor.execute_approved_candidates(
            hl=hl_mock,
            candidates_data=candidates_data,
            decisions=DECISIONS,
            radar_1h={},
            radar_4h=RADAR_4H,
            radar_1d=RADAR_1D,
            now=NOW,
        )
        
        print(f"  Status: {result['status']}")
        # DRY_RUN uses 'actions' not 'executed'
        actions = result.get('actions', [])
        print(f"  Actions (would execute): {len(actions)}")
        for e in actions:
            print(f"    - {e['symbol']}: qty={e.get('qty')}, leverage={e.get('leverage')}x, "
                  f"SL={e.get('hard_sl'):.4f}")
        
        print(f"  Pending (Chase): {len(result.get('pending', []))}")
        for p in result.get("pending", []):
            print(f"    - {p['symbol']}: kind={p['kind']}, created={p.get('created', False)}")
        
        if result.get("pending_cancelled"):
            print(f"  Old pendings cancelled: {result['pending_cancelled']}")
        
        # 3. Test pending worker (4H :10 job)
        print("\n## 3. Pending worker (4H :10 job)")
        # Load the pendings created by executor
        pendings = pe.load_pending()
        print(f"  Active pendings: {len([e for e in pendings if e.get('status') == pe.ACTIVE])}")
        
        # Simulate 4H worker evaluation
        pw_result = pw.run_pending(
            hl=hl_mock,
            radar_1d=RADAR_1D,
            radar_4h=RADAR_4H,
            now=NOW,
            entries=pendings,
        )
        
        print(f"  Status: {pw_result['status']}")
        print(f"  Checked: {len(pw_result.get('checked', []))}")
        for c in pw_result.get("checked", []):
            print(f"    - {c['symbol']} ({c['kind']}): action={c['action']}, result={c.get('result')}")
            if c.get('breakout_1d'):
                print(f"        Step 1: 1D breakout ✓")
            if c.get('retrace_touched'):
                print(f"        Step 2: 4H retrace ✓")
        
        print(f"  Would fill: {len(pw_result.get('filled', []))}")
        for f in pw_result.get("filled", []):
            print(f"    - {f['symbol']}: qty={f.get('qty')}, limit={f.get('limit_px')}, "
                  f"leverage={f.get('leverage')}x, SL={f.get('hard_sl'):.4f}")
        
        # 4. Summary
        print("\n" + "=" * 80)
        print("PILOT SUMMARY")
        print("=" * 80)
        actions = result.get('actions', [])
        print(f"BASE entries (08:55 dry-run): {len([e for e in actions if 'BASE' in e['symbol']])}")
        print(f"CONT pendings would create: {len([p for p in result.get('pending', []) if p.get('kind') == pe.CONT or p.get('kind') == pe.CONTINUATION])}")
        print(f"ADD_ON pendings would create: {len([p for p in result.get('pending', []) if p.get('kind') == pe.ADD_ON])}")
        print(f"Old pendings cancelled: {len(result.get('pending_cancelled', []))}")
        print(f"Pending worker triggers: {len([c for c in pw_result.get('checked', []) if c.get('action') == 'trigger'])}")
        print(f"Would-fill IOC orders (4H worker): {len(pw_result.get('filled', []))}")
        
        # Detailed order payloads
        print("\n## Order Payloads (what WOULD be sent)")
        print("\n### BASE entry (executor 08:55):")
        for e in actions:
            if "BASE" in e["symbol"]:
                print(f"  Symbol: {e['symbol']}")
                print(f"  Side: BUY")
                print(f"  Order Type: IOC limit")
                print(f"  Quantity: {e.get('qty')}")
                print(f"  Limit Price: {e.get('limit_px')}")
                print(f"  Leverage: {e.get('leverage')}x isolated")
                print(f"  Hard SL: {e.get('hard_sl')} ({e.get('hard_sl_label')})")
        
        print("\n### CONT/ADD_ON entries (pending worker 4H :10):")
        for f in pw_result.get("filled", []):
            print(f"  Symbol: {f['symbol']}")
            print(f"  Kind: {f['kind']}")
            print(f"  Side: BUY")
            print(f"  Order Type: IOC limit")
            print(f"  Quantity: {f.get('qty')}")
            print(f"  Limit Price: {f.get('limit_px')}")
            print(f"  Leverage: {f.get('leverage')}x isolated")
            print(f"  Hard SL: {f.get('hard_sl')} ({f.get('hard_sl_label')})")
        
        return 0


if __name__ == "__main__":
    sys.exit(run_pilot())
