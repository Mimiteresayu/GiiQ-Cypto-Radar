#!/usr/bin/env python3
"""EXIT_DESK health checks (GIIQ-SoT-3) — pure function, report only, never trades.

Railway computes this every DESK_DATA cycle and serves it at GET /api/exit/health (keyed), so the
Claude EXIT_DESK task only has to read one object and email when `ok` is false (plus one daily
"OK · n 倉" line). Checks:

  NO_SL            open position without a reduce-only trigger (Hard SL) order on HL
  EXIT_NOT_DONE    tier exit rule triggered on a closed bar > grace ago but the position is still open
                   (Small/Tiny: 1H close < 1H Lower; Mega/Large: 4H close < 4H Filter)
  JOB_FAILED       a scheduler job's last run ended in error within 24h
  JOB_MISSED       hourly 1H exit job or 4H exit job has not succeeded on time
  MARGIN_HIGH      total margin used > 80% of NAV
  ORPHAN_SL        trigger/reduce-only order on a coin with no position
  PENDING_STALE    pending entry older than its 7-day TTL still active
  LEVERAGE_OFF     position leverage outside 3-5x or not isolated
  RADAR_STALE      1H radar > 2h old or 4H radar > 5h old
  HL_FETCH         positions / open orders could not be fetched (nothing else can be trusted)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

TF_MS = {"1h": 3_600_000, "4h": 14_400_000}
EXIT_GRACE_MIN = 25          # exit jobs run at :07 (1H) / :10 (4H) -> allow 25 min after the bar close
MARGIN_ALERT_PCT = 80.0
LEV_MIN, LEV_MAX = 3, 5
PENDING_TTL_DAYS = 7
RADAR_MAX_AGE_H = {"1h": 2.0, "4h": 5.0}
JOB_MAX_AGE_MIN = {"1h_scan_exits": 75, "4h_scan_exits": 4 * 60 + 25}
HKT = timezone(timedelta(hours=8))


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(v: Any) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _positions(perp: Any) -> List[dict]:
    out = []
    groups = perp.get("assetPositions") or [] if isinstance(perp, dict) else []
    for g in groups:
        p = (g or {}).get("position") or {}
        szi = _f(p.get("szi")) or 0.0
        if not szi or not p.get("coin"):
            continue
        lev = p.get("leverage") or {}
        out.append({"coin": str(p["coin"]), "side": "LONG" if szi > 0 else "SHORT",
                    "lev": _f(lev.get("value")), "lev_type": lev.get("type"),
                    "margin": _f(p.get("marginUsed")) or 0.0})
    return out


def check(*, perp: Any, open_orders: Any, nav: Optional[float], radar_1h: dict, radar_4h: dict,
          tier_for, job_status: Dict[str, dict], pending: List[dict], now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    problems: List[dict] = []

    def add(code: str, coin: Optional[str], msg: str) -> None:
        problems.append({"code": code, "coin": coin, "msg": msg})

    if not isinstance(perp, dict) or perp.get("error"):
        add("HL_FETCH", None, f"positions unavailable: {(perp or {}).get('error') if isinstance(perp, dict) else perp}")
        return _result(problems, 0, now)
    pos = _positions(perp)
    orders_ok = isinstance(open_orders, list)
    if not orders_ok:
        add("HL_FETCH", None, f"open orders unavailable: {(open_orders or {}).get('error') if isinstance(open_orders, dict) else open_orders}")
    orders = open_orders if orders_ok else []
    sl_coins = {o.get("coin") for o in orders if o.get("isTrigger") or o.get("reduceOnly") or o.get("isPositionTpsl")}
    held = {p["coin"] for p in pos}

    rows_1h = {r.get("symbol"): r for r in (radar_1h or {}).get("rows") or []}
    rows_4h = {r.get("symbol"): r for r in (radar_4h or {}).get("rows") or []}
    for p in pos:
        c = p["coin"]
        if orders_ok and c not in sl_coins:
            add("NO_SL", c, "no reduce-only / trigger order (Hard SL) on HL")
        if p["lev"] is not None and not (LEV_MIN <= p["lev"] <= LEV_MAX):
            add("LEVERAGE_OFF", c, f"leverage {p['lev']:g}x outside {LEV_MIN}-{LEV_MAX}x")
        if p["lev_type"] and p["lev_type"] != "isolated":
            add("LEVERAGE_OFF", c, f"margin mode {p['lev_type']} (expected isolated)")
        if p["side"] != "LONG":
            continue
        tier = (tier_for(c) if tier_for else "unknown") or "unknown"
        if tier in ("mega", "large"):
            tf, row = "4h", rows_4h.get(c)
            hit = row and _f(row.get("close")) is not None and _f(row.get("filter")) is not None \
                and _f(row["close"]) < _f(row["filter"])
            rule = "4H close < 4H Filter"
        else:
            tf, row = "1h", rows_1h.get(c)
            hit = row and _f(row.get("close")) is not None and _f(row.get("lower")) is not None \
                and _f(row["close"]) < _f(row["lower"])
            rule = "1H close < 1H Lower"
        if hit:
            close_ms = int(_f(row.get("bar_time")) or 0) + TF_MS[tf]
            if close_ms and now_ms - close_ms > EXIT_GRACE_MIN * 60_000:
                add("EXIT_NOT_DONE", c, f"{tier}: {rule} on bar closed "
                    f"{datetime.fromtimestamp(close_ms / 1000, HKT).strftime('%m-%d %H:%M')} HKT, position still open")

    for c in sorted({o.get("coin") for o in orders if (o.get("isTrigger") or o.get("reduceOnly"))} - held):
        if c:
            add("ORPHAN_SL", c, "trigger/reduce-only order but no position (cancel it)")

    used = sum(p["margin"] for p in pos) or _f(((perp or {}).get("marginSummary") or {}).get("totalMarginUsed")) or 0.0
    if nav and nav > 0 and used / nav * 100 > MARGIN_ALERT_PCT:
        add("MARGIN_HIGH", None, f"margin used {used / nav * 100:.0f}% of NAV > {MARGIN_ALERT_PCT:g}%")

    for tf, radar in (("1h", radar_1h), ("4h", radar_4h)):
        t = _ts((radar or {}).get("ts"))
        age = (now - t).total_seconds() / 3600 if t else None
        if age is None or age > RADAR_MAX_AGE_H[tf]:
            add("RADAR_STALE", None, f"{tf.upper()} radar age {'unknown' if age is None else f'{age:.1f}h'} "
                f"> {RADAR_MAX_AGE_H[tf]:g}h")

    for name, st in (job_status or {}).items():
        if name.startswith("manual_"):
            continue  # password-triggered DRY_RUN runs are not production jobs
        t = _ts((st or {}).get("last_run"))
        if st and st.get("status") == "error" and t and now - t < timedelta(hours=24):
            add("JOB_FAILED", None, f"{name} error at {t.astimezone(HKT).strftime('%m-%d %H:%M')} HKT: "
                f"{(st.get('error') or st.get('message') or '')[:160]}")
    for name, max_min in JOB_MAX_AGE_MIN.items():
        st = (job_status or {}).get(name) or {}
        t = _ts(st.get("last_run"))
        if not t or now - t > timedelta(minutes=max_min):
            add("JOB_MISSED", None, f"{name} last ran "
                f"{t.astimezone(HKT).strftime('%m-%d %H:%M') + ' HKT' if t else 'never'} (> {max_min} min)")

    for e in pending or []:
        if e.get("status") != "pending":
            continue
        t = _ts(e.get("created_at"))
        if t and now - t > timedelta(days=PENDING_TTL_DAYS, hours=6):
            add("PENDING_STALE", e.get("symbol"), f"{e.get('kind')} pending since {t.date()} (> {PENDING_TTL_DAYS}d)")

    return _result(problems, len(pos), now)


def _result(problems: List[dict], n_pos: int, now: datetime) -> Dict[str, Any]:
    hkt = now.astimezone(HKT).strftime("%H:%M HKT %d-%b")
    return {"ok": not problems, "n_positions": n_pos, "problems": problems, "checked_at": now.isoformat(),
            "summary": f"OK · {n_pos} 倉 · {hkt}" if not problems
            else f"{len(problems)} problem(s) · {n_pos} 倉 · {hkt}"}
