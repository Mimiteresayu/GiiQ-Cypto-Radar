#!/usr/bin/env python3
"""Account view for the Cockpit — pure parsing of Hyperliquid info responses (no network).

  portfolio            -> PnL today / 7d / 30d / all-time + equity curve
  userFillsByTime      -> recent fills + 7-day realised PnL, fees, volume
  userFunding          -> funding paid/received (7 days) per coin
  frontendOpenOrders   -> Hard SL (trigger, reduce-only) vs limit orders, and whether each position is covered
  clearinghouseState   -> open risk: loss if every Hard SL is hit, from mark and from entry
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def parse_portfolio(raw: Any, max_points: int = 400) -> Dict[str, dict]:
    """[[window, {accountValueHistory, pnlHistory, vlm}], ...] -> {window: {av, pnl, pnl_total, vlm}}."""
    out: Dict[str, dict] = {}
    for item in raw or [] if isinstance(raw, list) else []:
        if not (isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict)):
            continue
        w, d = str(item[0]), item[1]
        av = [[int(t), _f(v)] for t, v in (d.get("accountValueHistory") or []) if _f(v) is not None]
        pnl = [[int(t), _f(v)] for t, v in (d.get("pnlHistory") or []) if _f(v) is not None]
        step = max(1, len(av) // max_points)
        out[w] = {"av": av[::step] + (av[-1:] if av and (len(av) - 1) % step else []),
                  "pnl_total": pnl[-1][1] - pnl[0][1] if len(pnl) >= 2 else (pnl[-1][1] if pnl else None),
                  "vlm": _f(d.get("vlm"))}
    return out


def pnl_windows(p: Dict[str, dict]) -> Dict[str, Optional[float]]:
    return {k: (p.get(k) or {}).get("pnl_total") for k in ("day", "week", "month", "allTime")}


def summarize_fills(fills: Any, now_ms: int, keep: int = 40) -> dict:
    rows = []
    for f in fills or [] if isinstance(fills, list) else []:
        t = int(f.get("time") or 0)
        px, sz = _f(f.get("px")), _f(f.get("sz"))
        rows.append({"t": t, "coin": f.get("coin"), "dir": f.get("dir") or ("Buy" if f.get("side") == "B" else "Sell"),
                     "px": px, "sz": sz, "ntl": round(px * sz, 2) if px and sz else None,
                     "closed_pnl": _f(f.get("closedPnl")), "fee": _f(f.get("fee")), "oid": f.get("oid")})
    rows.sort(key=lambda r: -r["t"])
    d7 = [r for r in rows if r["t"] >= now_ms - 7 * 86_400_000]
    return {"recent": rows[:keep],
            "d7": {"n": len(d7), "closed_pnl": round(sum(r["closed_pnl"] or 0 for r in d7), 2),
                   "fees": round(sum(r["fee"] or 0 for r in d7), 2),
                   "volume": round(sum(r["ntl"] or 0 for r in d7), 2)}}


def summarize_funding(rows: Any) -> dict:
    by: Dict[str, float] = {}
    for r in rows or [] if isinstance(rows, list) else []:
        d = (r or {}).get("delta") or {}
        if d.get("type") not in (None, "funding"):
            continue
        u = _f(d.get("usdc"))
        if u is None or not d.get("coin"):
            continue
        by[d["coin"]] = by.get(d["coin"], 0.0) + u
    return {"total": round(sum(by.values()), 4), "by_coin": {k: round(v, 4) for k, v in sorted(by.items())}}


def positions(perp: Any) -> List[dict]:
    out = []
    groups = perp.get("assetPositions") or [] if isinstance(perp, dict) else []
    for g in groups:
        p = (g or {}).get("position") or {}
        szi = _f(p.get("szi")) or 0.0
        if not szi or not p.get("coin"):
            continue
        val = _f(p.get("positionValue")) or 0.0
        out.append({"coin": p["coin"], "side": "LONG" if szi > 0 else "SHORT", "size": abs(szi),
                    "entry": _f(p.get("entryPx")), "mark": val / abs(szi) if szi else None, "value": val,
                    "upnl": _f(p.get("unrealizedPnl")), "liq": _f(p.get("liquidationPx")),
                    "margin": _f(p.get("marginUsed")),
                    "lev": _f((p.get("leverage") or {}).get("value")), "lev_type": (p.get("leverage") or {}).get("type")})
    return out


def classify_orders(orders: Any, pos: List[dict]) -> dict:
    """Split open orders into Hard SL (trigger + reduce-only) and the rest; mark coverage per position."""
    held = {p["coin"]: p for p in pos}
    sl, other = [], []
    for o in orders or [] if isinstance(orders, list) else []:
        trig = bool(o.get("isTrigger"))
        row = {"coin": o.get("coin"), "side": "Sell" if o.get("side") == "A" else "Buy",
               "type": o.get("orderType"), "sz": _f(o.get("sz")), "limit_px": _f(o.get("limitPx")),
               "trigger_px": _f(o.get("triggerPx")), "cond": o.get("triggerCondition"),
               "reduce_only": bool(o.get("reduceOnly")), "tpsl": bool(o.get("isPositionTpsl")),
               "t": o.get("timestamp"), "oid": o.get("oid")}
        p = held.get(row["coin"])
        if trig and (row["reduce_only"] or row["tpsl"]):
            px = row["trigger_px"]
            if p and px and p.get("mark"):
                row["dist_pct"] = round((px / p["mark"] - 1) * 100, 2)
                row["covers_pct"] = round((row["sz"] or 0) / p["size"] * 100, 1) if p["size"] else None
            row["orphan"] = p is None
            sl.append(row)
        else:
            other.append(row)
    covered = {o["coin"] for o in sl if not o.get("orphan")}
    return {"sl": sl, "other": other, "uncovered": sorted(set(held) - covered)}


def risk_to_sl(pos: List[dict], sl_orders: List[dict]) -> dict:
    """If every Hard SL fills at its trigger: loss from current mark and total result vs entry (longs)."""
    best: Dict[str, dict] = {}
    for o in sl_orders:
        if o.get("orphan") or not o.get("trigger_px"):
            continue
        cur = best.get(o["coin"])
        if cur is None or o["trigger_px"] > cur["trigger_px"]:
            best[o["coin"]] = o
    from_mark = vs_entry = 0.0
    per = []
    for p in pos:
        o = best.get(p["coin"])
        if not o or not p.get("mark") or not p.get("entry"):
            continue
        sgn = 1 if p["side"] == "LONG" else -1
        fm = sgn * (o["trigger_px"] - p["mark"]) * p["size"]
        ve = sgn * (o["trigger_px"] - p["entry"]) * p["size"]
        from_mark += fm
        vs_entry += ve
        per.append({"coin": p["coin"], "sl": o["trigger_px"], "from_mark": round(fm, 2), "vs_entry": round(ve, 2)})
    return {"from_mark": round(from_mark, 2), "vs_entry": round(vs_entry, 2), "per_coin": per}
