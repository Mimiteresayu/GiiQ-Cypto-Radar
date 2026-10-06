"""Pure parsers for public Hyperliquid info payloads. No network."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def _usdc_balance(state: Any) -> Optional[dict]:
    if not isinstance(state, dict):
        return None
    balances = state.get("balances")
    if not isinstance(balances, list):
        return None
    for row in balances:
        if isinstance(row, dict) and str(row.get("coin") or "").upper() == "USDC":
            return row
    return None


def spot_usdc(state: Any) -> Optional[float]:
    """Spot USDC `total` from spotClearinghouseState. None when the row is absent (not a guessed 0)."""
    row = _usdc_balance(state)
    return num(row.get("total")) if row else None


def spot_usdc_hold(state: Any) -> Optional[float]:
    """Spot USDC `hold`. 0 when the USDC row is present and hold is blank. None when the row is absent."""
    row = _usdc_balance(state)
    if not row:
        return None
    if row.get("hold") in (None, ""):
        return 0.0
    return num(row.get("hold"))


NAV_UNVERIFIED = "NAV: 未核實"
# NAV = (spot USDC total - spot USDC hold) + perp marginSummary.accountValue.
# On a unified account the spot total already includes position margin (hold), and
# accountValue is that same margin, so adding the raw total double-counts.


def portfolio_nav(perp_state: Any, spot_state: Any, *, perp_ok: bool, spot_ok: bool) -> dict:
    """NAV used by exit-monitor, daily-audit, Harbor, the BO report, and the C48 scoreboard.

    NAV = (spot USDC total - spot USDC hold) + perp accountValue. Both reads must succeed.
    A blank hold on a present USDC row is 0 (perp-only cash). A failed spot read, or a
    missing USDC total, leaves nav None (`NAV: 未核實`). Margin% uses this same NAV and
    is suppressed while it is unverified. Do not add totalRawUsd.
    """
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

    def _d(v: Any) -> Optional[Decimal]:
        if v is None or v == "":
            return None
        try:
            return Decimal(str(v))
        except (InvalidOperation, ValueError):
            return None

    perp = _d(account_value(perp_state)) if perp_ok else None
    row = _usdc_balance(spot_state) if spot_ok else None
    spot = _d(row.get("total")) if row else None
    if row and row.get("hold") in (None, ""):
        hold = Decimal("0")
    else:
        hold = _d(row.get("hold")) if row else None
    if perp is not None and spot is not None and hold is not None:
        nav = float((spot - hold + perp).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        from . import report
        return {"nav": nav, "perp": float(perp), "spot": float(spot), "hold": float(hold),
                "label": f"NAV: {report.usd(nav)}", "warning": None}
    warning = "NAV: 未核實 (spot USDC or perp accountValue unreadable; margin% not computed)"
    return {"nav": None, "perp": float(perp) if perp is not None else None,
            "spot": float(spot) if spot is not None else None,
            "hold": float(hold) if hold is not None else None,
            "label": NAV_UNVERIFIED, "warning": warning}


def nav_from_envelopes(state_res: Any, spot_res: Any) -> dict:
    """`portfolio_nav` for the `{ok, data}` envelopes the jobs already fetch."""
    state_res = state_res if isinstance(state_res, dict) else {}
    spot_res = spot_res if isinstance(spot_res, dict) else {}
    return portfolio_nav(
        state_res.get("data") if state_res.get("ok") else None,
        spot_res.get("data") if spot_res.get("ok") else None,
        perp_ok=bool(state_res.get("ok")),
        spot_ok=bool(spot_res.get("ok")),
    )


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


# An HL fill is not evidence that Railway placed the order.
MATCH_WINDOW_MS = 15 * 60 * 1000
_LONG = {"b", "buy", "bid", "long", "open long"}
_SHORT = {"a", "sell", "ask", "short", "open short"}


def _ms(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return int(v.timestamp() * 1000)
    if isinstance(v, (int, float)):
        n = float(v)
        if n < 10_000_000_000:
            n *= 1000.0
        return int(n)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


def _record_ms(rec: dict) -> Optional[int]:
    for key in ("time", "timestamp", "ts", "run_ts"):
        ms = _ms(rec.get(key))
        if ms is not None:
            return ms
    return None


def _norm_side(v: Any) -> Optional[str]:
    s = str(v or "").strip().lower()
    if s in _LONG or s.startswith("open long"):
        return "long"
    if s in _SHORT or s.startswith("open short"):
        return "short"
    return None


def _open_side(fill: dict) -> Optional[str]:
    d = str(fill.get("dir") or "")
    if d.startswith("Open Long"):
        return "long"
    if d.startswith("Open Short"):
        return "short"
    return _norm_side(fill.get("side"))


def _record_side(rec: dict) -> Optional[str]:
    for key in ("side", "dir", "kind"):
        side = _norm_side(rec.get(key))
        if side:
            return side
    return None


def executed_records(*payloads: Any) -> List[dict]:
    """Executed rows from cockpit run-report envelopes. Dry-runs are not placed orders."""
    out = []
    for res in payloads:
        if not isinstance(res, dict) or not res.get("ok"):
            continue
        data = res.get("data")
        if not isinstance(data, dict):
            continue
        for item in data.get("executed") or []:
            if isinstance(item, dict) and not item.get("dry_run"):
                out.append(item)
    return out


def order_actor(fill: dict, executed: Any) -> str:
    """`Railway` only when this open fill matches an executed record: same coin, ±15 min, same side."""
    if not str(fill.get("dir") or "").startswith("Open"):
        return "unknown"
    coin = str(fill.get("coin") or "").upper()
    fms = _ms(fill.get("time"))
    side = _open_side(fill)
    if not coin or fms is None or not side:
        return "unknown"
    for rec in executed or []:
        if not isinstance(rec, dict):
            continue
        if str(rec.get("symbol") or rec.get("coin") or "").upper() != coin:
            continue
        rms = _record_ms(rec)
        rside = _record_side(rec)
        if rms is None or rside is None:
            continue
        if rside == side and abs(fms - rms) <= MATCH_WINDOW_MS:
            return "Railway"
    return "unknown"


def _says_stop(fill: dict) -> bool:
    blob = " ".join(str(fill.get(k) or "") for k in ("dir", "orderType", "order_type", "triggerCondition")).lower()
    if "stop" in blob or "trigger" in blob:
        return True
    return bool(fill.get("isTrigger"))


def _stop_oids(orders: Any) -> set:
    oids = set()
    for o in orders or []:
        if not isinstance(o, dict) or o.get("oid") in (None, ""):
            continue
        otype = str(o.get("orderType") or "")
        is_trigger = bool(o.get("isTrigger")) or "stop" in otype.lower() or "trigger" in otype.lower()
        protective = bool(o.get("reduceOnly")) or bool(o.get("isPositionTpsl"))
        if is_trigger and protective:
            oids.add(str(o.get("oid")))
    return oids


def _matches_exit_job(fill: dict, actions: Any) -> bool:
    """True only when an already-fetched 1H exit-job record matches. No record source → False."""
    if not actions:
        return False
    coin = str(fill.get("coin") or "").upper()
    fms = _ms(fill.get("time"))
    oid = str(fill.get("oid") or "")
    for a in actions:
        if not isinstance(a, dict):
            continue
        job = str(a.get("job") or a.get("run_job") or "").lower()
        action = str(a.get("action") or a.get("kind") or "").lower()
        marked = ("1h" in job and "exit" in job) or action in {"exit", "1h_exit", "exit_job_1h"}
        if not marked:
            continue
        ac = str(a.get("coin") or a.get("symbol") or "").upper()
        if ac != coin:
            continue
        if oid and str(a.get("oid") or "") == oid:
            return True
        ams = _record_ms(a)
        if fms is not None and ams is not None and abs(fms - ams) <= MATCH_WINDOW_MS:
            return True
    return False


def close_actor(fill: dict, orders: Any = None, exit_actions: Any = None, historical_orders: Any = None) -> str:
    """One of `hard_sl`, `exit_job_1h`, `unknown`. Never Railway."""
    d = str(fill.get("dir") or "")
    if not (d.startswith("Close") or "liquidat" in d.lower()):
        return "unknown"
    oid = str(fill.get("oid") or "")
    if _says_stop(fill) or (oid and oid in _stop_oids(orders)) or (oid and oid in _stop_oids(historical_orders)):
        return "hard_sl"
    if _matches_exit_job(fill, exit_actions):
        return "exit_job_1h"
    return "unknown"


def fill_key(fill: dict) -> Optional[str]:
    """HL fill id used to skip a re-insert. tid, else hash. None when neither is present."""
    if fill.get("tid") not in (None, ""):
        return f"tid:{fill.get('tid')}"
    if fill.get("hash") not in (None, ""):
        return f"hash:{fill.get('hash')}"
    return None
