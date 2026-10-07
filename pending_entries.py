#!/usr/bin/env python3
"""Pending CONT / ADD_ON entries — storage + 3-step trigger rules (MMT 2026-10-07).

Rules (MMT, 2026-10-07, replacing old N/N+1 pullback):
- Base (fresh 1D dual-cross-up): NOT pending; executor enters at 08:55 if live mid > 1D Upper.
- CONT (CONTINUATION): approved Chase with no existing Base position. Same 3-step rule as ADD_ON.
- ADD_ON: approved Chase on a coin we already hold LONG. Same 3-step rule as CONT.
  
3-step trigger (same for CONT and ADD_ON):
  1. 1D breakout: 1D dual cross up above 1D Upper (green) — marks breakout_date_1d.
  2. 4H retrace: 4H close down to 4H Filter or Lower — marks retrace_touched.
  3. 4H breakout: 4H dual cross up above 4H Upper — TRIGGER (enter immediately with IOC + Hard SL).
  - Evaluated on closed bars only (4H worker runs at :10 after 4H close).
  - Re-arm: after each new retrace (step 2), the next 4H cross-up (step 3) can trigger again,
    subject to existing position/size limits and all SoT checks.
  - NO resting/pending limit orders; when triggered, the 4H worker places an immediate IOC entry
    with Hard SL, using the same fail-closed checks as the Base executor.
  
Created only from AI-approved decisions (approved size_pct / leverage kept, re-clamped to SoT bands
at fill time). Cancelled when 1D close < 1D Lower or after PENDING_TTL_DAYS (7).
Idempotent: one record per (symbol, kind, HKT decision date); filled/cancelled records are never
re-armed; a CONT whose coin is already held is cancelled; an ADD_ON whose base position is gone is
cancelled.

DISABLED by default (flag PENDING_CONTINUATION_DISABLED, unset or 1 = disabled; 0/false/no/off = enabled):
no new CONT / ADD_ON pendings are created and active ones are cancelled (boot, executor, pending worker).
Chase approvals become "acknowledged, no entry". Base entries are not affected.

Storage: JSON list at out/pending_entries.json (Railway volume), override with PENDING_PATH.
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from exec_common import HKT, parse_ts

ROOT = Path(__file__).resolve().parent
PENDING_TTL_DAYS = 7
BAR_MS = {"4h": 4 * 3600 * 1000, "1d": 86400 * 1000}
ADD_ON = "ADD_ON"
CONTINUATION = "CONTINUATION"
# New: terminology CONT replaces CONTINUATION everywhere user-facing
CONT = "CONT"
ACTIVE = "pending"
PENDING_KINDS = (ADD_ON, CONTINUATION, CONT)
DISABLE_ENV = "PENDING_CONTINUATION_DISABLED"
DISABLED_REASON = "disabled by Cove HEALTH FAIL 2026-10-05"


def pending_disabled() -> bool:
    """CONT / ADD_ON pendings are OFF by default (Cove HEALTH FAIL 2026-10-05, formal disable
    until Cove re-signs CONTINUATION health). Only PENDING_CONTINUATION_DISABLED=0/false/no/off re-enables."""
    return (os.environ.get(DISABLE_ENV) or "").strip().lower() not in ("0", "false", "no", "off")


def cancel_active_pending(entries: List[dict], now: datetime, reason: str = DISABLED_REASON) -> List[dict]:
    """Mark every ACTIVE CONT / ADD_ON record cancelled (mutates `entries`). Returns those records."""
    gone = []
    for e in entries:
        if e.get("status") == ACTIVE and e.get("kind") in PENDING_KINDS:
            e.update(status="cancelled", closed_at=now.isoformat(), close_reason=reason)
            gone.append(e)
    return gone


def enforce_disabled(now: Optional[datetime] = None) -> Dict[str, Any]:
    """If disabled: cancel active CONT / ADD_ON in the store and save (boot hook, one-shot clear)."""
    if not pending_disabled():
        return {"disabled": False, "cancelled": []}
    now = now or datetime.now(timezone.utc)
    entries = load_pending()
    gone = cancel_active_pending(entries, now)
    if gone:
        save_pending(entries)
    return {"disabled": True, "reason": DISABLED_REASON, "cancelled": [e.get("id") for e in gone]}


def pending_path() -> Path:
    return Path(os.environ.get("PENDING_PATH") or str(ROOT / "out" / "pending_entries.json"))


def load_pending() -> List[dict]:
    p = pending_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("entries", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])


def save_pending(entries: List[dict]) -> None:
    p = pending_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp." + secrets.token_hex(4))
    tmp.write_text(json.dumps({"updated_at": datetime.now(timezone.utc).isoformat(), "entries": entries},
                              indent=2, default=str), encoding="utf-8")
    os.replace(tmp, p)


def active(entries: List[dict]) -> List[dict]:
    return [e for e in entries if e.get("status") == ACTIVE]


def classify_chase(symbol: str, held_long: set) -> str:
    """Classify a Chase candidate as CONT or ADD_ON based on existing position."""
    return ADD_ON if symbol in held_long else CONT


def _f(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def band(kind: str, row_1d: Optional[dict], row_4h: Optional[dict]) -> Dict[str, Any]:
    """Latest CLOSED bar values needed for the 3-step rule. Returns 1D and 4H bands."""
    # For the new rule, we always need both 1D and 4H data
    d1 = row_1d or {}
    d4 = row_4h or {}
    return {
        "1d": {
            "upper": _f(d1.get("upper")),
            "lower": _f(d1.get("lower")),
            "close": _f(d1.get("close")),
            "prev_close": _f(d1.get("prev_close")),
            "prev_upper": _f(d1.get("prev_upper")),
            "trend": d1.get("trend"),
            "bar_time": d1.get("bar_time"),
        },
        "4h": {
            "upper": _f(d4.get("upper")),
            "lower": _f(d4.get("lower")),
            "filter": _f(d4.get("filter")),
            "close": _f(d4.get("close")),
            "prev_close": _f(d4.get("prev_close")),
            "prev_upper": _f(d4.get("prev_upper")),
            "trend": d4.get("trend"),
            "bar_time": d4.get("bar_time"),
        }
    }


def create_pending(entries: List[dict], symbol: str, kind: str, decision: dict, cand: dict,
                   bands: Dict[str, Any], now: datetime) -> Tuple[dict, bool]:
    """Idempotent create. Returns (record, created)."""
    day = now.astimezone(HKT).strftime("%Y%m%d")
    pid = f"{symbol}_{kind}_{day}"
    for e in entries:
        if e.get("id") == pid:
            return e, False
        if e.get("symbol") == symbol and e.get("status") == ACTIVE:
            return e, False  # one active pending per coin
    
    # Initial state tracking for the 3-step rule
    d1d = bands.get("1d", {})
    d4h = bands.get("4h", {})
    
    # Check if 1D breakout has already happened (step 1)
    breakout_1d = False
    breakout_date = None
    if (d1d.get("close") and d1d.get("upper") and d1d.get("prev_close") and d1d.get("prev_upper") and
        d1d.get("close") > d1d.get("upper") and d1d.get("prev_close") <= d1d.get("prev_upper") and
        d1d.get("trend") == "Green"):
        breakout_1d = True
        breakout_date = now.astimezone(HKT).strftime("%Y-%m-%d")
    
    rec = {
        "id": pid, "symbol": symbol, "kind": kind, "status": ACTIVE,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=PENDING_TTL_DAYS)).isoformat(),
        "decision_date": now.astimezone(HKT).strftime("%Y-%m-%d"),
        "size_pct": decision.get("size_pct"), "leverage": decision.get("leverage"),
        "reason": decision.get("reason", ""),
        "tier": cand.get("tier"), "signal_type": cand.get("type"),
        # 3-step state tracking
        "breakout_1d": breakout_1d,
        "breakout_date_1d": breakout_date,
        "retrace_touched": False,
        "last_retrace_bar_t": None,
        "last_check": None,
        "history": [],
    }
    entries.append(rec)
    return rec, True


def evaluate(rec: dict, bnd: Dict[str, Any], mid: Optional[float], now: datetime,
             held_long: set) -> Tuple[str, str, Dict[str, Any]]:
    """3-step state machine for CONT / ADD_ON (MMT 2026-10-07).

    bnd = {"1d": {...}, "4h": {...}} with latest CLOSED bar values
    -> (action, reason, updates); action in expire|cancel|wait|trigger; `updates` are record
    fields the caller persists in LIVE mode. Missing/misaligned data -> wait without state change
    (fail-closed, retried next 4H run)."""
    
    exp = parse_ts(rec.get("expires_at"))
    if exp and now >= exp:
        return "expire", f"expired after {PENDING_TTL_DAYS} days", {}
    
    sym, kind = rec.get("symbol"), rec.get("kind")
    if kind in (CONTINUATION, CONT) and sym in held_long:
        return "cancel", "coin already held (not adding a CONT)", {}
    if kind == ADD_ON and sym not in held_long:
        return "cancel", "base position no longer held", {}
    
    d1d = bnd.get("1d", {})
    d4h = bnd.get("4h", {})
    
    # Check for 1D close below 1D Lower -> cancel
    if d1d.get("close") and d1d.get("lower") and d1d.get("close") < d1d.get("lower"):
        return "cancel", f"1D closed {d1d.get('close'):.6g} below 1D Lower {d1d.get('lower'):.6g}", {}
    
    # Need valid 4H bar time to proceed
    bar_t_4h = d4h.get("bar_time")
    if not bar_t_4h:
        return "wait", "no 4H bar time", {}
    bar_t_4h = int(bar_t_4h)
    
    created = parse_ts(rec.get("created_at"))
    if created and bar_t_4h + BAR_MS["4h"] <= int(created.timestamp() * 1000):
        return "wait", "latest 4H bar closed before the pending was created", {}
    
    upd: Dict[str, Any] = {}
    
    # Step 1: Check for 1D breakout if not yet marked
    if not rec.get("breakout_1d"):
        if not all([d1d.get("close"), d1d.get("upper"), d1d.get("prev_close"), d1d.get("prev_upper")]):
            return "wait", "waiting for 1D dual cross up above 1D Upper", {}
        
        if (d1d.get("close") > d1d.get("upper") and 
            d1d.get("prev_close") <= d1d.get("prev_upper") and 
            d1d.get("trend") == "Green"):
            upd["breakout_1d"] = True
            upd["breakout_date_1d"] = now.astimezone(HKT).strftime("%Y-%m-%d")
            return "wait", f"Step 1 complete: 1D breakout detected (close {d1d.get('close'):.6g} > Upper {d1d.get('upper'):.6g})", upd
        return "wait", "waiting for 1D dual cross up above 1D Upper", {}
    
    # Step 2: Check for 4H retrace if not yet touched or if we've had a new 4H bar since last retrace
    if not d4h.get("close") or not d4h.get("filter") or not d4h.get("lower"):
        return "wait", "missing 4H band values", {}
    
    # Detect new retrace (4H close down to 4H Filter or Lower)
    last_retrace_bar = rec.get("last_retrace_bar_t")
    if (d4h.get("close") <= d4h.get("filter") and 
        (not last_retrace_bar or bar_t_4h > int(last_retrace_bar))):
        upd["retrace_touched"] = True
        upd["last_retrace_bar_t"] = bar_t_4h
        return "wait", f"Step 2 complete: 4H retrace detected (close {d4h.get('close'):.6g} <= Filter {d4h.get('filter'):.6g})", upd
    
    # Step 3: Check for 4H breakout (dual cross up above 4H Upper) if we've had a retrace
    if not rec.get("retrace_touched"):
        return "wait", "waiting for 4H retrace down to 4H Filter or Lower", {}
    
    if not all([d4h.get("close"), d4h.get("upper"), d4h.get("prev_close"), d4h.get("prev_upper")]):
        return "wait", "waiting for 4H dual cross up data", {}
    
    # Check for 4H dual cross up
    if (d4h.get("close") > d4h.get("upper") and 
        d4h.get("prev_close") <= d4h.get("prev_upper") and 
        d4h.get("trend") == "Green"):
        if not mid or mid <= d4h.get("lower", 0):
            return "wait", f"4H cross-up confirmed but live mid {mid} not above 4H Lower {d4h.get('lower'):.6g}", {}
        # TRIGGER!
        return "trigger", (f"Step 3 complete: 4H breakout (close {d4h.get('close'):.6g} > Upper {d4h.get('upper'):.6g}); "
                          f"enter at live mid {mid:.6g}"), {}
    
    return "wait", "waiting for 4H dual cross up above 4H Upper", {}


def summary(entries: List[dict], rows_1d: Dict[str, dict], rows_4h: Dict[str, dict],
            mids: Dict[str, float]) -> List[dict]:
    """Active pendings with live 3-step state (for cockpit / preflight / results)."""
    out = []
    for e in active(entries):
        b = band(e["kind"], rows_1d.get(e["symbol"]), rows_4h.get(e["symbol"]))
        mid = mids.get(e["symbol"]) if mids else None
        d1d, d4h = b.get("1d", {}), b.get("4h", {})
        
        # Determine current step
        if not e.get("breakout_1d"):
            step = "Step 1: waiting for 1D breakout"
        elif not e.get("retrace_touched"):
            step = "Step 2: waiting for 4H retrace"
        else:
            step = "Step 3: waiting for 4H breakout"
        
        out.append({
            "id": e["id"], 
            "symbol": e["symbol"], 
            "kind": e["kind"],
            "breakout_1d": e.get("breakout_1d", False),
            "breakout_date_1d": e.get("breakout_date_1d"),
            "retrace_touched": e.get("retrace_touched", False),
            "current_step": step,
            "1d_upper": d1d.get("upper"),
            "1d_lower": d1d.get("lower"),
            "1d_close": d1d.get("close"),
            "1d_trend": d1d.get("trend"),
            "4h_upper": d4h.get("upper"),
            "4h_filter": d4h.get("filter"),
            "4h_lower": d4h.get("lower"),
            "4h_close": d4h.get("close"),
            "4h_trend": d4h.get("trend"),
            "mid": mid,
            "size_pct": e.get("size_pct"), 
            "leverage": e.get("leverage"),
            "created_at": e.get("created_at"), 
            "expires_at": e.get("expires_at"),
            "last_check": e.get("last_check")
        })
    return out
