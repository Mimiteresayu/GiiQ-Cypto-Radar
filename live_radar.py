#!/usr/bin/env python3
"""LIVE radar refresh (display only) — runs every 10 min from serve.py.

Cost: ONE HL request (allMids) per cycle. Closed bars come from the candle cache
written by scan_gc_radar.py (out/candles_{tf}.json.gz); the forming bar is updated
with the live mid (close=mid, high=max, low=min; a new bar opens at o=h=l=c=mid when
a boundary passes). GC is recomputed and written to row["live"] in gc_radar_{tf}.json.

Top-level row fields (close/filter/upper/lower/trend/dual_cross_*) are the LAST CLOSED
bar and are NEVER modified here: they remain the only input for candidates, exits,
Hard SL and executor. The radar "ts" (closed-scan time) is left untouched too.

Limit: forming-bar high/low are sampled (10-min mids between scans), so live GC is a
close approximation; the official closed bar is re-fetched by the scanner after close.
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import scan_gc_radar as sgr

LIVE_TFS = ("1d", "4h", "1h")
NARRATIVE_ADD_MAX = 25  # max new narrative coins scanned per TF per cycle (HL rate limits)


def hl_perp_names(mids: Dict[str, float]) -> List[str]:
    """Perp names from allMids keys (spot pairs are '@123' or 'X/USDC')."""
    return [k for k in mids if not k.startswith("@") and "/" not in k]


def _recount(radar: dict) -> None:
    """Recompute closed-bar breadth/flags after rows were added (same fields as scan_tf)."""
    rows = radar.get("rows") or []
    g = sum(1 for r in rows if r.get("trend") == "Green")
    n = len(rows)
    radar["breadth"] = {"green_count": g, "red_count": sum(1 for r in rows if r.get("trend") == "Red"),
                        "green_pct": round(100.0 * g / n, 2) if n else 0.0, "n": n}
    radar["flags"] = {
        "dual_cross_up": [r["symbol"] for r in rows if r.get("dual_cross_up")],
        "dual_cross_down_filter": [r["symbol"] for r in rows if r.get("dual_cross_down_filter")],
        "above_upper": [r["symbol"] for r in rows if r.get("above_upper")],
    }
    radar["n_scanned"] = n


def sync_narrative(tf: str, radar: dict, cbars: dict, narrative_map: Dict[str, Optional[str]]) -> dict:
    """Add HL-listed narrative coins missing from this TF (same scan_symbol GC math, floor
    bypassed) and re-tag Narrative category on all rows from the CURRENT narrative list."""
    rows = radar.setdefault("rows", [])
    have = {r.get("symbol") for r in rows}
    want = [h for h in dict.fromkeys(narrative_map.values()) if h]
    cem = [c for c in sgr.load_cemetery_set() if c not in want] if tf != "1d" else []
    want += cem  # cemetery-only coins (from 1D) must be on 4H/1H too, same GC math
    # coins that failed recently (e.g. new listing, < period+20 bars) are retried every 6h only
    retry = radar.get("narrative_retry_after_ms") or {}
    now_ms = int(time.time() * 1000)
    missing = [h for h in want if h not in have and retry.get(h, 0) <= now_ms][:NARRATIVE_ADD_MAX]
    added, failed = [], []
    for coin in missing:
        try:
            row = sgr.scan_symbol(coin, tf)
        except Exception as e:
            row = None
            failed.append(f"{coin}: {e}")
        if not row:
            if not failed or not failed[-1].startswith(coin):
                failed.append(f"{coin}: insufficient history")
            retry[coin] = now_ms + 6 * 3600_000
            continue
        with sgr._BARS_LOCK:
            bars = sgr._BARS_CACHE.get((tf, coin)) or []
        cbars[coin] = [[b["t"], b["open"], b["high"], b["low"], b["close"], b["volume"]] for b in bars]
        row["narrative_forced"] = coin not in cem
        row["cemetery_forced"] = coin in cem
        row["in_floor"] = False
        rows.append(row)
        added.append(coin)
    narr = sgr.load_narrative_tickers()
    mcap_map = sgr.load_mcap_map() if getattr(sgr, "load_mcap_map", None) else {}
    cem_1d = set(sgr.load_cemetery_set()) if tf != "1d" else set()
    for r in rows:
        if tf != "1d":
            r["cemetery_1d"] = r.get("symbol") in cem_1d
        sgr.enrich_row_tier_category(r, narr, mcap_map)
    if added:
        _recount(radar)
    radar["narrative_retry_after_ms"] = {k: v for k, v in retry.items() if v > now_ms}
    radar["narrative_map"] = narrative_map
    radar["narrative_forced"] = sorted(set((radar.get("narrative_forced") or []) + added) & {r.get("symbol") for r in rows})
    radar["narrative_not_on_hl"] = sorted(t for t, h in narrative_map.items() if not h)
    return {"added": added, "failed": failed}


def fetch_all_mids() -> Dict[str, float]:
    raw = sgr.hl_post({"type": "allMids"})
    out: Dict[str, float] = {}
    for k, v in (raw or {}).items():
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _load_cache(path: str) -> dict:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _atomic_json(path: str, obj: dict, gz: bool = False, indent: Optional[int] = None) -> None:
    tmp = path + ".tmp"
    if gz:
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"))
    else:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=indent)
    os.replace(tmp, path)


def apply_mid(bars: List[list], mid: float, now_ms: int, bar_ms: int) -> List[list]:
    """Update/append the forming bar ([t,o,h,l,c,v]) with the live mid. Returns new list."""
    bars = [list(b) for b in bars]
    cur_open = now_ms - now_ms % bar_ms
    if bars and bars[-1][0] == cur_open:
        b = bars[-1]
        b[2] = max(b[2], mid)
        b[3] = min(b[3], mid)
        b[4] = mid
    elif not bars or bars[-1][0] < cur_open:
        bars.append([cur_open, mid, mid, mid, mid, 0.0])
    return bars


def refresh_tf(tf: str, mids: Dict[str, float], now_ms: Optional[int] = None,
               out_dir: Optional[str] = None, narrative_map: Optional[Dict[str, Optional[str]]] = None) -> dict:
    out_dir = out_dir or sgr.OUT_DIR
    now_ms = now_ms or int(time.time() * 1000)
    cfg = sgr.TF_CONFIG[tf]
    bar_ms = int(cfg["bar_ms"])
    period = sgr.gc_period_for_tf(tf)
    radar_path = os.path.join(out_dir, f"{cfg['out_stem']}.json")
    cache_path = os.path.join(out_dir, f"candles_{tf}.json.gz")
    try:
        with open(radar_path, encoding="utf-8") as f:
            radar = json.load(f)
    except (OSError, ValueError) as e:
        return {"tf": tf, "ok": False, "error": f"radar missing: {e}"}
    cache = _load_cache(cache_path)
    cbars = cache.get("bars") or {}
    if not cbars:
        return {"tf": tf, "ok": False, "error": "candle cache missing (run closed-bar scan first)"}

    nsync = sync_narrative(tf, radar, cbars, narrative_map) if narrative_map is not None else {}
    n_live = 0
    max_keep = int(cfg["n_bars"]) + 5
    for row in radar.get("rows") or []:
        sym = row.get("symbol")
        mid = mids.get(sym)
        raw = cbars.get(sym)
        if not raw or mid is None or mid <= 0:
            continue
        nb = apply_mid(raw, mid, now_ms, bar_ms)[-max_keep:]
        cbars[sym] = nb
        bars = [{"t": b[0], "open": b[1], "high": b[2], "low": b[3], "close": b[4], "volume": b[5]} for b in nb]
        gc = sgr.compute_gc([b["high"] for b in bars], [b["low"] for b in bars], [b["close"] for b in bars], period=period)
        if len(gc) < 2:
            continue
        row["live"] = sgr.live_values(bars, gc, len(gc) - 1, now_ms, bar_ms)
        n_live += 1

    rows = radar.get("rows") or []
    live_rows = [r["live"] for r in rows if isinstance(r.get("live"), dict)]
    g = sum(1 for lv in live_rows if lv.get("trend") == "Green")
    ts = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat()
    radar["live_ts"] = ts
    radar.update(sgr.radar_timing(tf, rows, now_ms))
    radar["live_breadth"] = {"green_count": g, "red_count": len(live_rows) - g,
                             "green_pct": round(100.0 * g / len(live_rows), 2) if live_rows else 0.0,
                             "n": len(live_rows)}
    radar["live_flags"] = {"cross_up": [r["symbol"] for r in rows if (r.get("live") or {}).get("cross_up")],
                           "above_upper": [r["symbol"] for r in rows if (r.get("live") or {}).get("above_upper")]}
    _atomic_json(radar_path, radar, indent=2)
    cache["bars"] = cbars
    cache["live_ts"] = ts
    _atomic_json(cache_path, cache, gz=True)
    return {"tf": tf, "ok": True, "n_live": n_live, "n_rows": len(rows), "live_ts": ts,
            "narrative_added": nsync.get("added", []), "narrative_failed": nsync.get("failed", [])}


def refresh_live(tfs=LIVE_TFS, mids: Optional[Dict[str, float]] = None, now_ms: Optional[int] = None,
                 out_dir: Optional[str] = None) -> dict:
    t0 = time.time()
    if mids is None:
        mids = fetch_all_mids()
    try:
        narrative_map = sgr.resolve_narrative(hl_perp_names(mids))
    except Exception:
        narrative_map = None
    res = {"ok": True, "n_mids": len(mids), "tfs": {},
           "narrative_on_hl": sorted(h for h in (narrative_map or {}).values() if h),
           "narrative_not_on_hl": sorted(t for t, h in (narrative_map or {}).items() if not h)}
    for tf in tfs:
        try:
            r = refresh_tf(tf, mids, now_ms=now_ms, out_dir=out_dir, narrative_map=narrative_map)
        except Exception as e:  # never break the loop on one TF
            r = {"tf": tf, "ok": False, "error": str(e)}
        res["tfs"][tf] = r
        res["ok"] = res["ok"] and r.get("ok", False)
    res["elapsed_s"] = round(time.time() - t0, 2)
    return res


if __name__ == "__main__":
    print(json.dumps(refresh_live(), indent=2))
    sys.exit(0)
