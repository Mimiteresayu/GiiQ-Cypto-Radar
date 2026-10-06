"""Pure parsers for public Hyperliquid info payloads. No network."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .stops import num


def positions(state: Any) -> List[dict]:
    out = []
    if not isinstance(state, dict):
        return out
    for g in state.get("assetPositions") or []:
        p = (g or {}).get("position") or {}
        szi = num(p.get("szi")) or 0.0
        if not szi or not p.get("coin"):
            continue
        lev = p.get("leverage") or {}
        out.append({
            "coin": str(p["coin"]),
            "side": "LONG" if szi > 0 else "SHORT",
            "szi": szi,
            "entry_px": num(p.get("entryPx")),
            "liq": num(p.get("liquidationPx")),
            "unrealized_pnl": num(p.get("unrealizedPnl")),
            "margin_used": num(p.get("marginUsed")),
            "position_value": num(p.get("positionValue")),
            "lev": num(lev.get("value")),
            "lev_type": lev.get("type"),
        })
    return out


def account_value(state: Any) -> Optional[float]:
    if not isinstance(state, dict):
        return None
    ms = state.get("marginSummary") or state.get("crossMarginSummary") or {}
    return num(ms.get("accountValue"))


def margin_used(state: Any) -> Optional[float]:
    if not isinstance(state, dict):
        return None
    ms = state.get("marginSummary") or {}
    return num(ms.get("totalMarginUsed"))


def raw_usdc(state: Any) -> Optional[float]:
    if not isinstance(state, dict):
        return None
    ms = state.get("marginSummary") or {}
    return num(ms.get("totalRawUsd"))


def fills(data: Any) -> List[dict]:
    if isinstance(data, dict):
        data = data.get("fills") or data.get("data") or []
    return [x for x in data or [] if isinstance(x, dict) and x.get("time") is not None]


def funding_usdc(data: Any) -> List[dict]:
    rows = data if isinstance(data, list) else []
    out = []
    for x in rows:
        if not isinstance(x, dict):
            continue
        delta = x.get("delta")
        usdc = None
        coin = x.get("coin")
        if isinstance(delta, dict):
            usdc = num(delta.get("usdc"))
            coin = coin or delta.get("coin")
        else:
            usdc = num(delta)
        if usdc is None:
            continue
        out.append({"time": int(x.get("time") or 0), "usdc": usdc, "coin": coin})
    return out


def mids(data: Any) -> Dict[str, float]:
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        px = num(v)
        if px:
            out[str(k)] = px
    return out


def hard_sl_order(orders: Any, coin: str) -> Optional[dict]:
    """Resting trigger / position-TPSL on this coin. A plain reduce-only limit is not a Hard SL."""
    for o in orders or []:
        if not isinstance(o, dict) or o.get("coin") != coin:
            continue
        if o.get("isTrigger") or o.get("isPositionTpsl"):
            return o
    return None


def trigger_px(order: Optional[dict]) -> Optional[float]:
    if not order:
        return None
    return num(order.get("triggerPx") if order.get("triggerPx") not in (None, "") else order.get("limitPx"))


def sum_pnl(rows: List[dict]) -> float:
    return sum((num(x.get("closedPnl")) or 0.0) - (num(x.get("fee")) or 0.0) for x in rows)


def in_window(rows: List[dict], start: datetime, end: datetime) -> List[dict]:
    a, b = start.timestamp() * 1000, end.timestamp() * 1000
    return [x for x in rows if a <= int(x["time"]) < b]


def hkt_midnight(now: datetime, days_ago: int = 0) -> datetime:
    from . import rules
    hk = now.astimezone(rules.HKT) - timedelta(days=days_ago)
    return hk.replace(hour=0, minute=0, second=0, microsecond=0)


def window_yesterday(now: datetime):
    """[yesterday 00:00 HKT, today 00:00 HKT)."""
    start = hkt_midnight(now, 1)
    return start, hkt_midnight(now, 0)


def window_today(now: datetime):
    """[today 00:00 HKT, now]."""
    return hkt_midnight(now, 0), now


def window_days(now: datetime, days: int):
    """[N HKT midnights ago, now]."""
    return hkt_midnight(now, days), now


def window_week(now: datetime):
    """[Monday 00:00 HKT of this week, now]."""
    from . import rules
    hk = now.astimezone(rules.HKT)
    monday = (hk - timedelta(days=hk.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday, now
