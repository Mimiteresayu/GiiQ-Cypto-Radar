#!/usr/bin/env python3
"""Market sentiment view for the cockpit "Market" tab — pure functions, no network.

Built from what Railway already has on the volume:
  * gc_radar_{1h,4h,1d}.json  (closed-bar SoT rows: trend, close, filter, upper, flags, tier, ...)
  * candles_{1h,4h,1d}.json.gz ([t,o,h,l,c,v] per coin, incl. the live forming bar)

Outputs (all display-only, never a signal input):
  sentiment   one plain label + why ("Pullback inside an uptrend", "Risk-on", ...) from multi-TF breadth
  regime      BTC / ETH / SOL / HYPE: trend + close vs Filter per timeframe
  breadth     now (closed bars) + history per TF (% of coins whose GC Filter is rising, % above Upper)
  tiles       one tile per coin: 24h change, 1H/4H/1D trend, fresh crosses — for the heatmap
  feed        fresh cross-ups / cross-downs on the last few CLOSED bars per TF
  sectors     Narrative sectors: how many coins, % green per TF, average 24h change
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional

TF_BAR_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
HIST_POINTS = {"1h": 168, "4h": 180, "1d": 120}      # 7 days / 30 days / ~4 months
FEED_BARS = {"1h": 6, "4h": 3, "1d": 3}               # how many recent closed bars feed the "fresh" list
REGIME_COINS = ("BTC", "ETH", "SOL", "HYPE")


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def series(bars: List[list], period: int, now_ms: int, bar_ms: int, gc_fn: Callable) -> Optional[dict]:
    """Closed bars only -> {t, c, filter, upper, green, above} lists (index-aligned)."""
    rows = sorted((b for b in bars or [] if int(b[0]) + bar_ms <= now_ms), key=lambda b: b[0])
    if len(rows) < period + 5:
        return None
    h = [float(b[2]) for b in rows]
    lo = [float(b[3]) for b in rows]
    c = [float(b[4]) for b in rows]
    gc = gc_fn(h, lo, c, period=period)
    t = [int(b[0]) for b in rows]
    green = [False] + [gc[i]["filter"] > gc[i - 1]["filter"] for i in range(1, len(gc))]
    above = [c[i] > gc[i]["upper"] for i in range(len(gc))]
    return {"t": t, "c": c, "filter": [g["filter"] for g in gc], "upper": [g["upper"] for g in gc],
            "green": green, "above": above, "warm": period}


def breadth_history(sers: Dict[str, dict], points: int) -> List[dict]:
    """% of coins with a rising Filter (Green) and % closing above Upper, per closed bar time."""
    agg: Dict[int, List[int]] = {}
    for s in sers.values():
        for i in range(s["warm"], len(s["t"])):
            a = agg.setdefault(s["t"][i], [0, 0, 0])
            a[0] += 1
            a[1] += int(s["green"][i])
            a[2] += int(s["above"][i])
    out = []
    for t in sorted(agg)[-points:]:
        n, g, ab = agg[t]
        if n >= 10:
            out.append({"t": t, "n": n, "green_pct": round(g / n * 100, 1), "above_pct": round(ab / n * 100, 1)})
    return out


def fresh_events(sers: Dict[str, dict], tf: str, n_bars: int) -> List[dict]:
    """Cross up through Upper / cross down through Filter on the last n CLOSED bars."""
    ev = []
    for sym, s in sers.items():
        c, up, fl, t = s["c"], s["upper"], s["filter"], s["t"]
        for i in range(max(1, len(t) - n_bars), len(t)):
            if c[i] > up[i] and c[i - 1] <= up[i - 1]:
                ev.append({"tf": tf, "symbol": sym, "kind": "cross_up", "t": t[i], "close": c[i],
                           "green": s["green"][i]})
            elif c[i] < fl[i] and c[i - 1] >= fl[i - 1]:
                ev.append({"tf": tf, "symbol": sym, "kind": "cross_down_filter", "t": t[i], "close": c[i],
                           "green": s["green"][i]})
    ev.sort(key=lambda e: (-e["t"], e["kind"], e["symbol"]))
    return ev


def breadth_now(radar: dict) -> Optional[dict]:
    rows = [r for r in (radar or {}).get("rows") or [] if r.get("symbol")]
    if not rows:
        return None
    n = len(rows)
    g = sum(1 for r in rows if r.get("trend") == "Green")
    ab = sum(1 for r in rows if r.get("above_upper"))
    return {"n": n, "green_pct": round(g / n * 100, 1), "above_pct": round(ab / n * 100, 1),
            "cross_up": sum(1 for r in rows if r.get("dual_cross_up")),
            "cross_down": sum(1 for r in rows if r.get("dual_cross_down_filter"))}


def _delta(hist: List[dict], bars_back: int) -> Optional[float]:
    if len(hist) <= bars_back:
        return None
    return round(hist[-1]["green_pct"] - hist[-1 - bars_back]["green_pct"], 1)


def sentiment(now: Dict[str, Optional[dict]], hist_1h: List[dict]) -> dict:
    """One plain-language read of the market from multi-timeframe breadth (display only)."""
    g = {tf: (now.get(tf) or {}).get("green_pct") for tf in ("1h", "4h", "1d")}
    d6, d24 = _delta(hist_1h, 6), _delta(hist_1h, 24)
    if None in g.values():
        return {"label": "No data", "tone": "neutral", "why": "radar not loaded", "d6h": d6, "d24h": d24}
    h, f, d = g["1h"], g["4h"], g["1d"]
    if d >= 60 and f >= 60 and h >= 50:
        label, tone, why = "Risk-on", "up", "trend up on every timeframe"
    elif d >= 60 and (h < 35 or f < 50):
        label, tone, why = "Pullback in an uptrend", "warn", "daily trend strong, short-term (1H/4H) selling"
    elif d < 40 and f < 40:
        label, tone, why = "Risk-off", "down", "daily and 4H trends mostly down"
    elif d < 50 and h >= 60:
        label, tone, why = "Bounce in a downtrend", "warn", "short-term buying against a weak daily trend"
    else:
        label, tone, why = "Mixed", "neutral", "timeframes disagree"
    if d6 is not None and abs(d6) >= 15:
        why += f"; 1H breadth {'+' if d6 > 0 else ''}{d6:g} pts in 6h"
    return {"label": label, "tone": tone, "why": why, "d6h": d6, "d24h": d24, "green": g}


def regime(radars: Dict[str, dict]) -> List[dict]:
    out = []
    by_tf = {tf: {r.get("symbol"): r for r in (radars.get(tf) or {}).get("rows") or []} for tf in ("1h", "4h", "1d")}
    for coin in REGIME_COINS:
        row = {"symbol": coin}
        for tf in ("1h", "4h", "1d"):
            r = by_tf[tf].get(coin)
            if not r:
                continue
            c, fl = _f(r.get("close")), _f(r.get("filter"))
            row[tf] = {"trend": r.get("trend"), "above_upper": bool(r.get("above_upper")),
                       "vs_filter_pct": round((c / fl - 1) * 100, 2) if c and fl else None}
        if len(row) > 1:
            out.append(row)
    return out


def change_24h(bars_1h: Optional[List[list]], row_1d: Optional[dict]) -> Optional[float]:
    """Live price vs 24 hourly bars earlier (candle cache holds the forming bar); fallback 1D live vs close."""
    if bars_1h and len(bars_1h) >= 25:
        b = sorted(bars_1h, key=lambda x: x[0])
        now, then = _f(b[-1][4]), _f(b[-25][4])
        if now and then:
            return round((now / then - 1) * 100, 2)
    live = (row_1d or {}).get("live") or {}
    lc, c = _f(live.get("close")), _f((row_1d or {}).get("close"))
    return round((lc / c - 1) * 100, 2) if lc and c else None


def build(radars: Dict[str, dict], bars_by_tf: Dict[str, Dict[str, List[list]]], sectors: Dict[str, Optional[str]],
          periods: Dict[str, int], now_ms: int, gc_fn: Callable) -> dict:
    sers: Dict[str, Dict[str, dict]] = {}
    for tf in ("1h", "4h", "1d"):
        sers[tf] = {}
        for sym, bars in (bars_by_tf.get(tf) or {}).items():
            s = series(bars, periods[tf], now_ms, TF_BAR_MS[tf], gc_fn)
            if s:
                sers[tf][sym] = s
    hist = {tf: breadth_history(sers[tf], HIST_POINTS[tf]) for tf in sers}
    now = {tf: breadth_now(radars.get(tf) or {}) for tf in ("1h", "4h", "1d")}
    feed = []
    for tf in ("1d", "4h", "1h"):
        feed += fresh_events(sers[tf], tf, FEED_BARS[tf])
    feed.sort(key=lambda e: (-e["t"], e["kind"], e["symbol"]))      # newest first across timeframes

    rows = {tf: {r.get("symbol"): r for r in (radars.get(tf) or {}).get("rows") or [] if r.get("symbol")}
            for tf in ("1h", "4h", "1d")}
    fresh_up = {(e["tf"], e["symbol"]) for e in feed if e["kind"] == "cross_up"}
    tiles = []
    for sym, r1d in rows["1d"].items():
        chg = change_24h((bars_by_tf.get("1h") or {}).get(sym), r1d)
        tiles.append({
            "symbol": sym, "tier": r1d.get("tier") or "unknown", "sector": sectors.get(str(sym).upper()),
            "chg24": chg, "close": r1d.get("close"),
            "t1h": (rows["1h"].get(sym) or {}).get("trend"), "t4h": (rows["4h"].get(sym) or {}).get("trend"),
            "t1d": r1d.get("trend"), "above_1d": bool(r1d.get("above_upper")),
            "fresh": [tf for tf in ("1d", "4h", "1h") if (tf, sym) in fresh_up],
            "vol": r1d.get("day_ntl_vlm"), "rvol": r1d.get("rvol"),
        })
    tiles.sort(key=lambda x: -(x["vol"] or 0))

    sec: Dict[str, List[dict]] = {}
    for t in tiles:
        if t["sector"]:
            sec.setdefault(t["sector"], []).append(t)
    sector_rows = []
    for name, ts in sec.items():
        chgs = [t["chg24"] for t in ts if t["chg24"] is not None]
        sector_rows.append({
            "sector": name, "n": len(ts), "coins": [t["symbol"] for t in ts][:8],
            **{f"green_{tf}": round(sum(1 for t in ts if t[f"t{tf}"] == "Green") / len(ts) * 100)
               for tf in ("1h", "4h", "1d")},
            "avg_chg24": round(sum(chgs) / len(chgs), 2) if chgs else None})
    sector_rows.sort(key=lambda s: -(s["avg_chg24"] if s["avg_chg24"] is not None else -1e9))

    return {"sentiment": sentiment(now, hist.get("1h") or []), "regime": regime(radars),
            "breadth": {"now": now, "history": hist}, "tiles": tiles, "feed": feed[:80],
            "sectors": sector_rows, "radar_ts": {tf: (radars.get(tf) or {}).get("ts") for tf in ("1h", "4h", "1d")}}
