#!/usr/bin/env python3
"""AI decision storage for entry candidate approvals/vetos.

Stores decisions as JSON with timestamp and reason.
Each decision is keyed by symbol + date.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
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


def _decision_file(now: Optional[datetime] = None) -> Path:
    """Return path to the decision file for `now`'s HKT date (default: today, UTC+8)."""
    now_utc = now or datetime.now(timezone.utc)
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


DECISION_SOURCES = ("claude", "fallback")
LATE_AFTER_HKT = (8, 50)  # decisions after 08:50 HKT miss the 08:55 run's preflight window


def store_decisions(decisions: List[Dict[str, Any]], source: str = "claude",
                    now: Optional[datetime] = None) -> Dict[str, Any]:
    """Store AI decisions for today.
    
    Args:
        decisions: List of {symbol, decision: approve|veto, size_pct, leverage, reason}
        
    Returns:
        dict with ok, stored_count, file_path
    """
    _ensure_dir()
    
    source = source if source in DECISION_SOURCES else "claude"
    now_dt = now or datetime.now(timezone.utc)
    now = now_dt.isoformat()
    file_path = _decision_file(now_dt)
    
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

    # Fallback (Harbor 08:40) only fills in when Claude's POST never arrived: never override Claude.
    if source == "fallback" and any(_rec_source(r) == "claude" for r in existing["decisions"].values()):
        return {"ok": False, "stored_count": 0, "rejected_count": len(decisions), "stored": [],
                "error": "claude decisions already stored today; fallback ignored", "timestamp": now}
    
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
        record = {**norm, "timestamp": now, "source": source}
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
    hkt = now_dt + timedelta(hours=8)
    if (hkt.hour, hkt.minute) >= LATE_AFTER_HKT and hkt.hour < 12:
        out["late"] = f"received {hkt.strftime('%H:%M')} HKT (after 08:50): may miss today's 08:55 run"
    out["source"] = source
    return out


def _rec_source(rec: Dict[str, Any]) -> str:
    """Records written before GIIQ-SoT-3 carry no source: they were Claude POSTs."""
    return str((rec or {}).get("source") or "claude")


def days_without_claude(now: Optional[datetime] = None, max_days: int = 7) -> int:
    """Consecutive HKT days, ending today, with NO Claude-sourced decision stored (0 = Claude posted today)."""
    now = now or datetime.now(timezone.utc)
    n = 0
    for i in range(max_days):
        f = _decision_file(now - timedelta(days=i))
        recs: Dict[str, Any] = {}
        if f.is_file():
            try:
                recs = (json.loads(f.read_text(encoding="utf-8")) or {}).get("decisions") or {}
            except (OSError, json.JSONDecodeError):
                recs = {}
        if any(_rec_source(r) == "claude" for r in recs.values()):
            break
        n += 1
    return n


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
