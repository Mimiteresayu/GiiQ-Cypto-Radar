#!/usr/bin/env python3
"""Pending pullback entries (ADD_ON / CONTINUATION) — storage + pure rules.

Rules (MMT, 2026-09-28):
- Base (fresh 1D dual-cross-up): NOT pending; executor enters at 08:55 if live mid > 1D Upper.
- ADD_ON: approved 4H Chase on a coin we already hold LONG. No 08:55 entry; zone =
  [4H Lower, 4H Filter] (latest closed 4H bar), 4H trend must be Green.
- CONTINUATION: approved Chase with no position. No 08:55 entry; zone = [1D Lower, 1D Filter]
  (latest closed 1D bar), 1D trend must be Green.
- Checked every 4H on closed 4H bars (Railway 4H :10 job). Trigger when the JUST-CLOSED 4H bar's
  low touched the zone (low <= zone Filter) AND that bar's close is still above the zone Lower;
  then enter at live mid only if mid is within [Lower, Filter * 1.01].
- Created only from AI-approved decisions (approved size_pct / leverage kept, re-clamped to
  SoT bands at fill time). Cancelled when the relevant TF (ADD_ON 4H / CONTINUATION 1D) CLOSES
  below its Lower, or after PENDING_TTL_DAYS (7).
- Idempotent: one record per (symbol, kind, HKT decision date); filled/cancelled records are
  never re-armed; a CONTINUATION whose coin is already held is cancelled; an ADD_ON whose base
  position is gone is cancelled.

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
ENTRY_FILTER_SLACK = 1.01  # enter only if live mid <= zone Filter * 1.01
ADD_ON = "ADD_ON"
CONTINUATION = "CONTINUATION"
BAND_TF = {ADD_ON: "4h", CONTINUATION: "1d"}
ACTIVE = "pending"


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
    return ADD_ON if symbol in held_long else CONTINUATION


def _f(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def band(kind: str, row_1d: Optional[dict], row_4h: Optional[dict]) -> Dict[str, Any]:
    """Trigger zone from the latest CLOSED bar of the band TF (top-level radar row fields)."""
    tf = BAND_TF[kind]
    row = (row_4h if tf == "4h" else row_1d) or {}
    return {"tf": tf, "lower": _f(row.get("lower")), "filter": _f(row.get("filter")),
            "close": _f(row.get("close")), "trend": row.get("trend"), "bar_time": row.get("bar_time")}


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
    rec = {
        "id": pid, "symbol": symbol, "kind": kind, "status": ACTIVE,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=PENDING_TTL_DAYS)).isoformat(),
        "decision_date": now.astimezone(HKT).strftime("%Y-%m-%d"),
        "size_pct": decision.get("size_pct"), "leverage": decision.get("leverage"),
        "reason": decision.get("reason", ""),
        "tier": cand.get("tier"), "signal_type": cand.get("type"),
        "band_tf": bands.get("tf"), "zone_at_create": {"lower": bands.get("lower"), "filter": bands.get("filter")},
        "last_check": None, "history": [],
    }
    entries.append(rec)
    return rec, True


def evaluate(rec: dict, bnd: Dict[str, Any], mid: Optional[float], now: datetime,
             held_long: set, bar4h: Optional[dict] = None) -> Tuple[str, str]:
    """-> (action, reason); action in expire|cancel|wait|trigger. Fail-closed: missing data -> wait.

    bar4h = the just-closed 4H candle {"t", "l", "c"} (floats) for this coin."""
    exp = parse_ts(rec.get("expires_at"))
    if exp and now >= exp:
        return "expire", f"expired after {PENDING_TTL_DAYS} days"
    sym, kind = rec.get("symbol"), rec.get("kind")
    if kind == CONTINUATION and sym in held_long:
        return "cancel", "coin already held (idempotent: not adding a CONTINUATION)"
    if kind == ADD_ON and sym not in held_long:
        return "cancel", "base position no longer held"
    lo, fi, cl, tr = bnd.get("lower"), bnd.get("filter"), bnd.get("close"), bnd.get("trend")
    tf = (bnd.get("tf") or "").upper()
    if not lo or not fi or not cl:
        return "wait", f"missing {tf} band values"
    if cl < lo:
        return "cancel", f"{tf} closed {cl:.6g} below {tf} Lower {lo:.6g}"
    if tr != "Green":
        return "wait", f"{tf} trend {tr} (need Green)"
    b_low, b_close = _f((bar4h or {}).get("l")), _f((bar4h or {}).get("c"))
    if not b_low or not b_close:
        return "wait", "no just-closed 4H bar"
    if b_low > fi:
        return "wait", f"4H bar low {b_low:.6g} did not touch zone (Filter {fi:.6g})"
    if b_close <= lo:
        return "wait", f"4H bar touched zone but closed {b_close:.6g} <= {tf} Lower {lo:.6g}"
    if not mid or mid <= 0:
        return "wait", "no live mid"
    hi = fi * ENTRY_FILTER_SLACK
    if lo <= mid <= hi:
        return "trigger", (f"4H low {b_low:.6g} touched zone, close {b_close:.6g} > Lower; live mid {mid:.6g} "
                           f"in [{tf} Lower {lo:.6g}, Filter*1.01 {hi:.6g}]")
    where = "above Filter*1.01" if mid > hi else "below Lower"
    return "wait", f"4H touched zone but live mid {mid:.6g} {where} ({lo:.6g}-{hi:.6g})"


def summary(entries: List[dict], rows_1d: Dict[str, dict], rows_4h: Dict[str, dict],
            mids: Dict[str, float]) -> List[dict]:
    """Active pendings with live trigger zone (for cockpit / preflight / results)."""
    out = []
    for e in active(entries):
        b = band(e["kind"], rows_1d.get(e["symbol"]), rows_4h.get(e["symbol"]))
        mid = mids.get(e["symbol"]) if mids else None
        in_zone = bool(mid and b["lower"] and b["filter"] and b["lower"] <= mid <= b["filter"] * ENTRY_FILTER_SLACK)
        out.append({"id": e["id"], "symbol": e["symbol"], "kind": e["kind"], "band_tf": b["tf"],
                    "zone_lower": b["lower"], "zone_filter": b["filter"], "trend": b["trend"],
                    "entry_max": round(b["filter"] * ENTRY_FILTER_SLACK, 10) if b["filter"] else None,
                    "trigger": "just-closed 4H low <= zone Filter AND 4H close > zone Lower; "
                               "then live mid in [Lower, Filter*1.01]",
                    "mid": mid, "in_zone": in_zone, "size_pct": e.get("size_pct"), "leverage": e.get("leverage"),
                    "created_at": e.get("created_at"), "expires_at": e.get("expires_at"),
                    "last_check": e.get("last_check")})
    return out
