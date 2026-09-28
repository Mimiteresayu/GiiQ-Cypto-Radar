#!/usr/bin/env python3
"""AI decision storage for entry candidate approvals/vetos.

Stores decisions as JSON with timestamp and reason.
Each decision is keyed by symbol + date.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
DEFAULT_DECISIONS_DIR = ROOT / "out" / "decisions"


def _decisions_dir() -> Path:
    """Resolve at call time so DECISIONS_DIR overrides (tests, subprocess env) always apply."""
    return Path(os.environ.get("DECISIONS_DIR") or str(DEFAULT_DECISIONS_DIR))


def _ensure_dir() -> None:
    """Ensure decisions directory exists."""
    _decisions_dir().mkdir(parents=True, exist_ok=True)


def _decision_file() -> Path:
    """Return path to today's decision file (HKT date = UTC+8)."""
    from datetime import timedelta
    now_utc = datetime.now(timezone.utc)
    hkt = now_utc + timedelta(hours=8)
    date_str = hkt.strftime("%Y%m%d")
    return _decisions_dir() / f"decisions_{date_str}.json"


_ACTION_MAP = {"approve": "approve", "approved": "approve", "veto": "veto", "vetoed": "veto", "reject": "veto"}


def normalize(dec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Accept both POST shapes: {symbol, decision: approve|veto} (original) and the ENTRY_DESK
    SoT-2 prompt shape {coin, action: APPROVE|VETO, type}. Returns None if unusable."""
    if not isinstance(dec, dict):
        return None
    symbol = str(dec.get("symbol") or dec.get("coin") or "").upper().strip()
    action = _ACTION_MAP.get(str(dec.get("decision") or dec.get("action") or "").lower().strip())
    if not symbol or not action:
        return None
    dims = dec.get("dims") if isinstance(dec.get("dims"), dict) else None
    return {
        "symbol": symbol,
        "decision": action,
        "type": (str(dec.get("type")).upper() if dec.get("type") else None),
        "size_pct": dec.get("size_pct"),
        "leverage": dec.get("leverage"),
        "reason": dec.get("reason", ""),
        "dims": dims,
    }


def store_decisions(decisions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Store AI decisions for today.
    
    Args:
        decisions: List of {symbol, decision: approve|veto, size_pct, leverage, reason}
        
    Returns:
        dict with ok, stored_count, file_path
    """
    _ensure_dir()
    
    now = datetime.now(timezone.utc).isoformat()
    file_path = _decision_file()
    
    # Load existing decisions for today if any
    existing: Dict[str, Any] = {}
    if file_path.is_file():
        try:
            existing = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    
    # Ensure structure
    if not isinstance(existing, dict) or "decisions" not in existing:
        existing = {"decisions": {}, "history": []}
    
    # Store each decision
    stored_count = 0
    rejected: List[Any] = []
    stored: List[Dict[str, Any]] = []
    for dec in decisions:
        norm = normalize(dec)
        if not norm:
            rejected.append(dec)
            continue
        symbol = norm["symbol"]

        # Build decision record
        record = {**norm, "timestamp": now}
        stored.append(record)
        
        # Store in decisions map (latest decision per symbol)
        existing["decisions"][symbol] = record
        
        # Append to history (all decisions)
        existing["history"].append(record)
        
        stored_count += 1
    
    # Write back
    file_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    
    out = {
        "ok": stored_count > 0 or not decisions,
        "stored_count": stored_count,
        "rejected_count": len(rejected),
        "file_path": str(file_path),
        "timestamp": now,
        "stored": stored,
    }
    if rejected:
        out["rejected"] = rejected[:20]
        out["expected_shape"] = "{coin|symbol, action|decision: APPROVE|VETO, type, size_pct, leverage, reason, dims}"
    if not out["ok"]:
        out["error"] = "no valid decisions (check coin/action keys)"
    return out


def get_decisions_for_today() -> Dict[str, Dict[str, Any]]:
    """Get all decisions for today.
    
    Returns:
        dict mapping symbol -> {decision, size_pct, leverage, reason, timestamp}
    """
    file_path = _decision_file()
    if not file_path.is_file():
        return {}
    
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        return data.get("decisions", {})
    except (OSError, json.JSONDecodeError):
        return {}


def get_approved_symbols() -> List[str]:
    """Return list of symbols approved for today."""
    decisions = get_decisions_for_today()
    return [
        symbol
        for symbol, rec in decisions.items()
        if rec.get("decision") == "approve"
    ]


def clear_decisions_for_today() -> Dict[str, Any]:
    """Clear today's decisions (for testing/reset).
    
    Returns:
        dict with ok, message
    """
    file_path = _decision_file()
    if file_path.is_file():
        file_path.unlink()
        return {"ok": True, "message": f"cleared {file_path}"}
    return {"ok": True, "message": "no decisions to clear"}


if __name__ == "__main__":
    # Example usage
    sample_decisions = [
        {
            "symbol": "BTC",
            "decision": "approve",
            "size_pct": 6.0,
            "leverage": 3.0,
            "reason": "Strong 1D uptrend, low SL distance",
        },
        {
            "symbol": "ETH",
            "decision": "veto",
            "size_pct": None,
            "leverage": None,
            "reason": "BTC 4H below filter",
        },
    ]
    
    result = store_decisions(sample_decisions)
    print(f"Stored: {result}")
    
    today = get_decisions_for_today()
    print(f"Today's decisions: {json.dumps(today, indent=2)}")
    
    approved = get_approved_symbols()
    print(f"Approved symbols: {approved}")
