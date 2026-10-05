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
- Tunable via env (chase_params; defaults = the rules above, invalid values -> default + [PENDING_CFG] warning):
  CHASE_PENDING_MODE (filter|lower|upper: band line bar N's low must touch), CHASE_PENDING_OFFSET_PCT (touch
  level this % below that line), CHASE_PENDING_TTL_DAYS, CHASE_MAX_CHASE_PCT (unset = no cap: N+1 waits if the
  live mid is more than this % above the 1D Upper), CHASE_FILL_SLIPPAGE_PCT (HL pending IOC slippage; unset =
  EXEC_ENTRY_SLIPPAGE_PCT).
- Idempotent: one record per (symbol, kind, HKT decision date); filled/cancelled records are
  never re-armed; a CONTINUATION whose coin is already held is cancelled; an ADD_ON whose base
  position is gone is cancelled.

Storage: JSON list at out/pending_entries.json (Railway volume), override with PENDING_PATH.
"""
from __future__ import annotations

import json
import math
import os
import secrets
import sys
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

CHASE_PENDING_MODES = {"filter": "Filter", "lower": "Lower", "upper": "Upper"}
# name -> (default, low, high, low_inclusive); default None = feature off / inherit
_CHASE_NUM = {
    "CHASE_PENDING_OFFSET_PCT": (0.0, 0.0, 50.0, True),
    "CHASE_PENDING_TTL_DAYS": (PENDING_TTL_DAYS, 0.0, 30.0, False),
    "CHASE_MAX_CHASE_PCT": (None, 0.0, 100.0, True),
    "CHASE_FILL_SLIPPAGE_PCT": (None, 0.0, 2.0, True),
}
_warned: set = set()


def _cfg_warn(msg: str) -> None:
    if msg not in _warned:
        _warned.add(msg)
        sys.stderr.write(f"[PENDING_CFG] {msg}\n")
        sys.stderr.flush()


def _env_num(env: Any, name: str) -> Optional[float]:
    default, lo, hi, lo_incl = _CHASE_NUM[name]
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        v = math.nan
    if not math.isfinite(v) or v > hi or v < lo or (v == lo and not lo_incl):
        rng = f"{'[' if lo_incl else '('}{lo:g}, {hi:g}]"
        _cfg_warn(f"{name}={raw[:32]!r} invalid (need a number in {rng}) -> default {default}")
        return default
    return v


def chase_params(env: Any = None) -> Dict[str, Any]:
    """Chase pending placement settings from env. Defaults reproduce the MMT 2026-09-28 rules exactly."""
    env = os.environ if env is None else env
    mode = (env.get("CHASE_PENDING_MODE") or "").strip().lower() or "filter"
    if mode not in CHASE_PENDING_MODES:
        _cfg_warn(f"CHASE_PENDING_MODE={mode[:32]!r} invalid (need one of {'|'.join(CHASE_PENDING_MODES)}) "
                  f"-> default 'filter'")
        mode = "filter"
    return {"mode": mode,
            "offset_pct": _env_num(env, "CHASE_PENDING_OFFSET_PCT"),
            "ttl_days": _env_num(env, "CHASE_PENDING_TTL_DAYS"),
            "max_chase_pct": _env_num(env, "CHASE_MAX_CHASE_PCT"),
            "fill_slippage_pct": _env_num(env, "CHASE_FILL_SLIPPAGE_PCT")}


def touch_level(bnd: Dict[str, Any], params: Dict[str, Any]) -> Tuple[Optional[float], str]:
    """Level bar N's low must reach (<=) -> (level, label). Default: the band Filter, label 'Filter'."""
    ref = _f(bnd.get(params["mode"]))
    label = CHASE_PENDING_MODES[params["mode"]]
    off = params["offset_pct"] or 0.0
    if not off or ref is None:
        return ref, label
    return ref * (1 - off / 100.0), f"{label} -{off:g}%"


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
            "close": _f(row.get("close")), "trend": row.get("trend"), "bar_time": row.get("bar_time"),
            "upper": _f(row.get("upper")), "upper_1d": _f((row_1d or {}).get("upper"))}


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
        "expires_at": (now + timedelta(days=chase_params()["ttl_days"])).isoformat(),
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
             held_long: set, bar: Optional[dict] = None,
             params: Optional[Dict[str, Any]] = None) -> Tuple[str, str, Dict[str, Any]]:
    """N / N+1 pullback state machine for one pending record.

    bnd = band-TF radar values of the latest CLOSED bar (lower/filter/close/trend/bar_time; upper and
    upper_1d for CHASE_PENDING_MODE=upper / CHASE_MAX_CHASE_PCT).
    bar = latest CLOSED band-TF candle from HL {"t","l","c"}.
    params = chase_params() (read from env when None).
    -> (action, reason, updates); action in expire|cancel|wait|trigger; `updates` are record
    fields (setup / last_bar_t) the caller persists in LIVE mode. Missing/misaligned data -> wait
    without consuming the bar (fail-closed, retried next 4H run)."""
    p = params or chase_params()
    exp = parse_ts(rec.get("expires_at"))
    if exp and now >= exp:
        c_at = parse_ts(rec.get("created_at"))
        ttl = (exp - c_at).total_seconds() / 86400.0 if c_at else p["ttl_days"]
        return "expire", f"expired after {ttl:g} days", {}
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
    touch, t_label = touch_level(bnd, p)
    if not lo or not fi or not touch:
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
            cap = p["max_chase_pct"]
            if cap is not None:
                up1 = _f(bnd.get("upper_1d"))
                if not up1:
                    return "wait", f"N+1 confirmed but no 1D Upper for the max-chase check ({cap:g}%)", upd
                ext = (mid / up1 - 1) * 100.0
                if ext > cap:
                    return "wait", (f"N+1 confirmed but live mid {mid:.6g} is {ext:.2f}% above 1D Upper "
                                    f"{up1:.6g} (max chase {cap:g}%)"), upd
            return "trigger", (f"N+1 confirmed: {tf} close {b_close:.6g} > Lower {lo:.6g} and > N close "
                               f"{n_close:.6g}; enter at live mid {mid:.6g}"), upd
        why_n1 = (f"N+1 not confirmed ({tf} close {b_close:.6g} vs N close {n_close:.6g}, Lower {lo:.6g}, "
                  f"trend {tr})")
    else:
        why_n1 = ""
    # evaluate this bar as a (new) bar N
    if b_low <= touch and b_close > lo:
        upd["setup"] = {"t": b_t, "l": b_low, "c": b_close, "filter": fi, "lower": lo}
        if touch != fi:
            upd["setup"]["touch"] = touch
        return "wait", ((why_n1 + "; ") if why_n1 else "") + (
            f"bar N set: {tf} low {b_low:.6g} <= {t_label} {touch:.6g}, close {b_close:.6g} > Lower {lo:.6g}; "
            f"awaiting N+1 close > {max(lo, b_close):.6g}"), upd
    upd["setup"] = None
    return "wait", ((why_n1 + "; ") if why_n1 else "") + (
        f"no pullback: {tf} low {b_low:.6g} > {t_label} {touch:.6g}"), upd


def summary(entries: List[dict], rows_1d: Dict[str, dict], rows_4h: Dict[str, dict],
            mids: Dict[str, float]) -> List[dict]:
    """Active pendings with live trigger zone (for cockpit / preflight / results)."""
    out = []
    p = chase_params()
    for e in active(entries):
        b = band(e["kind"], rows_1d.get(e["symbol"]), rows_4h.get(e["symbol"]))
        mid = mids.get(e["symbol"]) if mids else None
        in_zone = bool(mid and b["lower"] and b["filter"] and b["lower"] <= mid <= b["filter"])
        st = e.get("setup")
        tf = (b["tf"] or "").upper()
        if st:
            trig = f"bar N set ({tf} low {st.get('l')}, close {st.get('c')}): enter if next {tf} close > {max(float(st.get('c') or 0), float(b['lower'] or 0)):.6g}"
        else:
            touch, t_label = touch_level(b, p)
            trig = f"waiting for {tf} bar N: low <= {t_label} {touch} and close > Lower {b['lower']}; then N+1 close > Lower and > N close"
        out.append({"id": e["id"], "symbol": e["symbol"], "kind": e["kind"], "band_tf": b["tf"],
                    "zone_lower": b["lower"], "zone_filter": b["filter"], "trend": b["trend"],
                    "setup": st, "trigger": trig,
                    "mid": mid, "in_zone": in_zone, "size_pct": e.get("size_pct"), "leverage": e.get("leverage"),
                    "created_at": e.get("created_at"), "expires_at": e.get("expires_at"),
                    "last_check": e.get("last_check")})
    return out
