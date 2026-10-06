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
    """Perp marginSummary.totalRawUsd. Not the spot USDC balance. Do not use this as cash."""
    if not isinstance(state, dict):
        return None
    ms = state.get("marginSummary") or {}
    return num(ms.get("totalRawUsd"))


def spot_usdc(state: Any) -> Optional[float]:
    """Spot USDC `total` from spotClearinghouseState. None when the row is absent (not a guessed 0)."""
    if not isinstance(state, dict):
        return None
    balances = state.get("balances")
    if not isinstance(balances, list):
        return None
    for row in balances:
        if isinstance(row, dict) and str(row.get("coin") or "").upper() == "USDC":
            return num(row.get("total"))
    return None


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


def _order_size(order: dict) -> Optional[float]:
    raw = order.get("sz")
    if raw in (None, ""):
        raw = order.get("origSz")
    return num(raw)


def _is_sell(order: dict) -> bool:
    return str(order.get("side") or "").strip().upper() in {"A", "SELL", "S", "ASK"}


def _is_buy(order: dict) -> bool:
    return str(order.get("side") or "").strip().upper() in {"B", "BUY", "BID"}


def hard_sl_status(orders: Any, coin: str, side: str, size: Optional[float], mark: Optional[float]) -> dict:
    """One Hard SL check for exit-monitor, daily-audit, Harbor, and the BO report.

    A Hard SL is a trigger/stop, reduce-only or position-TPSL, on this coin, on the opposite
    side of the position, with the trigger on the losing side of the mark (long: trigger < mark,
    short: trigger > mark), and size covering the position. A take-profit is not a Hard SL.
    """
    out = {"ok": False, "status": "NO", "trigger_px": None, "distance_pct": None, "order": None}
    try:
        need = abs(float(size)) if size is not None else None
        mk = float(mark) if mark is not None else None
    except (TypeError, ValueError):
        return out
    if not need or mk is None or mk == 0:
        return out
    long = str(side or "").upper() != "SHORT"
    best = None
    best_dist = None
    for o in orders or []:
        if not isinstance(o, dict) or str(o.get("coin") or "") != coin:
            continue
        otype = str(o.get("orderType") or "")
        is_trigger = bool(o.get("isTrigger")) or "stop" in otype.lower()
        protective = bool(o.get("reduceOnly")) or bool(o.get("isPositionTpsl"))
        if not (is_trigger and protective):
            continue
        if long and not _is_sell(o):
            continue
        if not long and not _is_buy(o):
            continue
        px = trigger_px(o)
        if px is None:
            continue
        if long and not px < mk:
            continue
        if not long and not px > mk:
            continue
        have = _order_size(o)
        if have is None or abs(have) + 1e-9 < need:
            continue
        dist = abs(mk - px) / abs(mk) * 100.0
        if best_dist is None or dist < best_dist:
            best, best_dist = o, dist
    if best is None:
        return out
    out.update(ok=True, status="YES", trigger_px=trigger_px(best), distance_pct=best_dist, order=best)
    return out


def fmt_hard_sl(status: dict) -> str:
    if not status or not status.get("ok"):
        return "Hard SL: NO"
    return f"Hard SL: YES {status['distance_pct']:.2f}% from mark"


def hard_sl_order(orders: Any, coin: str) -> Optional[dict]:
    """Deprecated loose match. Callers that decide 'Hard SL present' use hard_sl_status."""
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
