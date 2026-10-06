#!/usr/bin/env python3
"""Pending pullback entries (ADD_ON / CONTINUATION) — storage + pure rules.

Rules (MMT, 2026-09-28):
- Base (fresh 1D dual-cross-up): NOT pending; executor enters at 08:55 if live mid > 1D Upper.
- ADD_ON: approved 4H Chase on a coin we already hold LONG. No 08:55 entry; zone =
  [4H Lower, 4H Filter] (latest closed 4H bar), 4H trend must be Green.
- CONTINUATION: approved Chase with no position. No 08:55 entry; zone = [1D Lower, 1D Filter]
  (latest closed 1D bar), 1D trend must be Green.
- Checked in the Railway 4H :10 job (both kinds), with N / N+1 confirmation on the band TF:
  bar N   = a closed band-TF bar with low <= Filter AND close > Lower  -> stored as `setup`
  bar N+1 = the NEXT closed band-TF bar: close > Lower AND close > bar N close -> trigger; the entry
            is placed at the live mid right after N+1 closed (same fail-closed checks as the executor).
  If N+1 does not confirm, it is itself re-evaluated as a new bar N. ADD_ON uses 4H bars; for
  CONTINUATION the 4H job only acts when a new 1D bar has closed (first 4H run after 08:00 HKT).
  Only bars that close after the pending was created count; each bar is processed once.
- Created only from AI-approved decisions (approved size_pct / leverage kept, re-clamped to
  SoT bands at fill time). Cancelled when any band-TF close (ADD_ON 4H / CONTINUATION 1D) is
  below its Lower, or after PENDING_TTL_DAYS (7).
- Idempotent: one record per (symbol, kind, HKT decision date); filled/cancelled records are
  never re-armed; a CONTINUATION whose coin is already held is cancelled; an ADD_ON whose base
  position is gone is cancelled.

DISABLED (Cove HEALTH FAIL 2026-10-05): no new CONTINUATION / ADD_ON pendings are created and active
ones are cancelled (boot, executor, pending worker) unless PENDING_CONTINUATION_DISABLED=0 is set.
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
BAND_TF = {ADD_ON: "4h", CONTINUATION: "1d"}
ACTIVE = "pending"
PENDING_KINDS = (ADD_ON, CONTINUATION)
DISABLE_ENV = "PENDING_CONTINUATION_DISABLED"
DISABLED_REASON = "disabled by Cove HEALTH FAIL 2026-10-05"


def pending_disabled() -> bool:
    """CONTINUATION / ADD_ON pendings are OFF by default (Cove HEALTH FAIL 2026-10-05, formal disable
    until Cove re-signs CONTINUATION health). Only PENDING_CONTINUATION_DISABLED=0/false/no/off re-enables."""
    return (os.environ.get(DISABLE_ENV) or "").strip().lower() not in ("0", "false", "no", "off")


def cancel_active_pending(entries: List[dict], now: datetime, reason: str = DISABLED_REASON) -> List[dict]:
    """Mark every ACTIVE CONTINUATION / ADD_ON record cancelled (mutates `entries`). Returns those records."""
    gone = []
    for e in entries:
        if e.get("status") == ACTIVE and e.get("kind") in PENDING_KINDS:
            e.update(status="cancelled", closed_at=now.isoformat(), close_reason=reason)
            gone.append(e)
    return gone


def enforce_disabled(now: Optional[datetime] = None) -> Dict[str, Any]:
    """If disabled: cancel active CONTINUATION / ADD_ON in the store and save (boot hook, one-shot clear)."""
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
             held_long: set, bar: Optional[dict] = None) -> Tuple[str, str, Dict[str, Any]]:
    """N / N+1 pullback state machine for one pending record.

    bnd = band-TF radar values of the latest CLOSED bar (lower/filter/close/trend/bar_time).
    bar = latest CLOSED band-TF candle from HL {"t","l","c"}.
    -> (action, reason, updates); action in expire|cancel|wait|trigger; `updates` are record
    fields (setup / last_bar_t) the caller persists in LIVE mode. Missing/misaligned data -> wait
    without consuming the bar (fail-closed, retried next 4H run)."""
    exp = parse_ts(rec.get("expires_at"))
    if exp and now >= exp:
        return "expire", f"expired after {PENDING_TTL_DAYS} days", {}
    sym, kind = rec.get("symbol"), rec.get("kind")
    if kind == CONTINUATION and sym in held_long:
        return "cancel", "coin already held (idempotent: not adding a CONTINUATION)", {}
    if kind == ADD_ON and sym not in held_long:
        return "cancel", "base position no longer held", {}
    tfk = bnd.get("tf") or BAND_TF.get(kind, "4h")
    tf = tfk.upper()
    lo, fi, tr = bnd.get("lower"), bnd.get("filter"), bnd.get("trend")
    b_t, b_low, b_close = (bar or {}).get("t"), _f((bar or {}).get("l")), _f((bar or {}).get("c"))
    if not b_t or not b_low or not b_close:
        return "wait", f"no closed {tf} bar", {}
    b_t = int(b_t)
    if rec.get("last_bar_t") and int(rec["last_bar_t"]) >= b_t:
        return "wait", f"no new closed {tf} bar yet", {}
    created = parse_ts(rec.get("created_at"))
    if created and b_t + BAR_MS[tfk] <= int(created.timestamp() * 1000):
        return "wait", f"latest {tf} bar closed before the pending was created", {}
    if not lo or not fi:
        return "wait", f"missing {tf} band values", {}
    if bnd.get("bar_time") is not None and int(bnd["bar_time"]) != b_t:
        return "wait", f"{tf} radar band not yet for the latest closed bar", {}
    upd: Dict[str, Any] = {"last_bar_t": b_t}
    if b_close < lo:
        return "cancel", f"{tf} closed {b_close:.6g} below {tf} Lower {lo:.6g}", upd
    setup = rec.get("setup") or None
    if setup and int(setup.get("t", 0)) + BAR_MS[tfk] == b_t:
        # this bar is N+1
        n_close = float(setup.get("c"))
        if b_close > lo and b_close > n_close and tr == "Green":
            if not mid or mid <= lo:
                upd["setup"] = None
                return "wait", f"N+1 confirmed but live mid {mid} not above {tf} Lower {lo:.6g}", upd
            upd["setup"] = None
            return "trigger", (f"N+1 confirmed: {tf} close {b_close:.6g} > Lower {lo:.6g} and > N close "
                               f"{n_close:.6g}; enter at live mid {mid:.6g}"), upd
        why_n1 = (f"N+1 not confirmed ({tf} close {b_close:.6g} vs N close {n_close:.6g}, Lower {lo:.6g}, "
                  f"trend {tr})")
    else:
        why_n1 = ""
    # evaluate this bar as a (new) bar N
    if b_low <= fi and b_close > lo:
        upd["setup"] = {"t": b_t, "l": b_low, "c": b_close, "filter": fi, "lower": lo}
        return "wait", ((why_n1 + "; ") if why_n1 else "") + (
            f"bar N set: {tf} low {b_low:.6g} <= Filter {fi:.6g}, close {b_close:.6g} > Lower {lo:.6g}; "
            f"awaiting N+1 close > {max(lo, b_close):.6g}"), upd
    upd["setup"] = None
    return "wait", ((why_n1 + "; ") if why_n1 else "") + (
        f"no pullback: {tf} low {b_low:.6g} > Filter {fi:.6g}"), upd


def summary(entries: List[dict], rows_1d: Dict[str, dict], rows_4h: Dict[str, dict],
            mids: Dict[str, float]) -> List[dict]:
    """Active pendings with live trigger zone (for cockpit / preflight / results)."""
    out = []
    for e in active(entries):
        b = band(e["kind"], rows_1d.get(e["symbol"]), rows_4h.get(e["symbol"]))
        mid = mids.get(e["symbol"]) if mids else None
        in_zone = bool(mid and b["lower"] and b["filter"] and b["lower"] <= mid <= b["filter"])
        st = e.get("setup")
        tf = (b["tf"] or "").upper()
        if st:
            trig = f"bar N set ({tf} low {st.get('l')}, close {st.get('c')}): enter if next {tf} close > {max(float(st.get('c') or 0), float(b['lower'] or 0)):.6g}"
        else:
            trig = f"waiting for {tf} bar N: low <= Filter {b['filter']} and close > Lower {b['lower']}; then N+1 close > Lower and > N close"
        out.append({"id": e["id"], "symbol": e["symbol"], "kind": e["kind"], "band_tf": b["tf"],
                    "zone_lower": b["lower"], "zone_filter": b["filter"], "trend": b["trend"],
                    "setup": st, "trigger": trig,
                    "mid": mid, "in_zone": in_zone, "size_pct": e.get("size_pct"), "leverage": e.get("leverage"),
                    "created_at": e.get("created_at"), "expires_at": e.get("expires_at"),
                    "last_check": e.get("last_check")})
    return out
