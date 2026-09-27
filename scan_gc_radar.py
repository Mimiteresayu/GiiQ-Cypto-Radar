#!/usr/bin/env python3
"""
Own Trend Radar — multi-timeframe GC scan (DRY_RUN, no trading).

GC math: DonovanWall / Signum Strategy v3.3
  source=hlc3, poles=4, mult=1.414, Lag/Fast OFF; period per TF: 1d=144, 4h=72, 1h=48
Ported from /workspace/signum-compat-gc/gc.ts

Usage:
  python scan_gc_radar.py              # scan 2h + 4h + 1d
  python scan_gc_radar.py --tf 1d      # single TF
  python scan_gc_radar.py --tf 1h,4h   # subset
  python scan_gc_radar.py --max 80     # smaller universe (e.g. for 1h smoke)
  python scan_gc_radar.py --tf 1d --max 250  # expanded liquid universe smoke
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "out")
HL_INFO = "https://api.hyperliquid.xyz/info"


def _resolve_data_file(*candidates: str) -> str:
    """First existing path among candidates (abs or relative to ROOT)."""
    for c in candidates:
        path = c if os.path.isabs(c) else os.path.join(ROOT, c)
        if os.path.isfile(path):
            return path
    # Prefer first candidate as default (may be missing; loaders handle that)
    c0 = candidates[0]
    return c0 if os.path.isabs(c0) else os.path.join(ROOT, c0)


# Optional secondary filter + volume fallback (baked under data/ for Railway)
CANDIDATES_PATH = _resolve_data_file(
    "data/own_radar_candidates_base_v0.json",
    "own_radar_candidates_base_v0.json",
    "/workspace/own_radar_candidates_base_v0.json",
)
HL_VOLUME_JSON = _resolve_data_file(
    "data/hl_volume_top100.json",
    "hl_volume_top100.json",
    "/workspace/hl_volume_top100.json",
)

# Size tier (mcap) + category sleeves
try:
    from mcap_tiers import load_mcap_map, tier_from_mcap, mcap_usd as _mcap_usd_lookup
except ImportError:  # pragma: no cover
    load_mcap_map = None  # type: ignore
    tier_from_mcap = None  # type: ignore
    _mcap_usd_lookup = None  # type: ignore

CEMETERY_DROP_ATH_PCT = 70.0
NARRATIVE_PATHS = (
    "narrative/watchlist.json",
    "out/narrative_watchlist.json",
    "data/narrative_watchlist.json",
)


def _norm_sym(s: str) -> str:
    return str(s or "").strip().upper().lstrip("$")


def _sym_aliases(sym: str) -> set:
    """Match keys: symbol + stripped k-prefix (kPEPE→PEPE). Does not invent k-forms."""
    s = _norm_sym(sym)
    out = {s}
    if s.startswith("K") and len(s) > 2 and s[1:].isalnum():
        out.add(s[1:])
    return out


def load_narrative_tickers() -> set:
    """Union of tickers from narrative watchlist JSON files (graceful if missing)."""
    tickers: set = set()
    for rel in NARRATIVE_PATHS:
        path = rel if os.path.isabs(rel) else os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            continue
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items = []
        if isinstance(raw, dict):
            items = raw.get("items") or raw.get("watchlist") or raw.get("symbols") or []
        elif isinstance(raw, list):
            items = raw
        for it in items:
            if isinstance(it, str):
                t = it
            elif isinstance(it, dict):
                t = it.get("ticker") or it.get("symbol") or it.get("coin") or ""
            else:
                continue
            t = _norm_sym(t)
            if not t:
                continue
            tickers.add(t)
            # bare form so HL kPEPE matches watchlist PEPE
            if t.startswith("K") and len(t) > 2:
                tickers.add(t[1:])
    return tickers


def classify_category(symbol: str, drop_from_ath_pct, narrative_set: set) -> tuple:
    """Primary category + optional multi-label list. Priority: Narrative > Cemetery > Price."""
    syms = _sym_aliases(symbol)
    is_narr = bool(syms & narrative_set)
    try:
        drop = float(drop_from_ath_pct) if drop_from_ath_pct is not None else None
    except (TypeError, ValueError):
        drop = None
    is_cem = drop is not None and drop >= CEMETERY_DROP_ATH_PCT
    cats = []
    if is_narr:
        cats.append("Narrative")
    if is_cem:
        cats.append("Cemetery")
    if not cats:
        cats.append("Price")
    primary = "Narrative" if is_narr else ("Cemetery" if is_cem else "Price")
    return primary, cats


def enrich_row_tier_category(row: dict, narrative_set: set, mcap_map: dict | None = None) -> None:
    """Attach tier / mcap_usd / mcap_known / category / categories in-place."""
    sym = row.get("symbol") or ""
    usd = None
    if mcap_map is not None:
        for a in _sym_aliases(sym):
            if a in mcap_map:
                usd = mcap_map[a]
                break
    elif _mcap_usd_lookup is not None:
        usd = _mcap_usd_lookup(sym)
        if usd is None:
            # try stripped k-prefix
            for a in _sym_aliases(sym):
                usd = _mcap_usd_lookup(a)
                if usd is not None:
                    break
    if tier_from_mcap is not None:
        tier = tier_from_mcap(usd)
    else:
        tier = "tiny"
    row["tier"] = tier
    row["mcap_usd"] = round(float(usd), 2) if usd is not None else None
    row["mcap_known"] = usd is not None
    primary, cats = classify_category(sym, row.get("drop_from_ath_pct"), narrative_set)
    row["category"] = primary
    row["categories"] = cats


MAX_SYMBOLS = 280  # expanded liquid HL universe (~250–300; --max overrides)
# Universe liquidity floor: dayNtlVlm >= $75k (between $50k–$100k).
# Target ~200–400 names; exclude delisted / zero-vol dust. Soft: openInterest > 0.
MIN_DAY_NTL_VLM = 75_000.0
REQUIRE_OI_POSITIVE = True  # soft floor when OI present in assetCtxs
RVOL_LOOKBACK = 14  # closed days prior used for median volume (RVOL)
CONCURRENCY = 2  # polite; scanning ~280×TFs is heavy
REQUEST_PAUSE_S = 0.35  # polite rate limit between requests in a worker

GC_POLES = 4
GC_PERIOD = 144
GC_MULT = 1.414
GC_REDUCED_LAG = False
GC_FAST_RESPONSE = False

# HL candleSnapshot intervals (verified: 2h, 4h, 1d; Lag/Fast OFF = DW defaults)
VALID_TFS = ("1h", "2h", "4h", "1d")

# Bar fetch windows (enough warmup for period=144 + lag)
# 1h: ~400 bars (~16d) — positions-only scans; period=144 needs warmup
# 1d: ~280 days (existing)
# 4h: ~450 bars (~75 days) — period*4h ≈ 24d min; fetch 400-500
# 2h: ~500 bars (~40 days)
# Per-TF GC period (TV lock 2026-09-21): 1D=144, 4H=72, 1H=48; 2h keep 144 until locked
TF_CONFIG: Dict[str, Dict[str, Any]] = {
    "1h": {
        "interval": "1h",
        "bar_ms": 3600 * 1000,
        "n_bars": 400,
        "period": 48,
        "out_stem": "gc_radar_1h",
    },
    "2h": {
        "interval": "2h",
        "bar_ms": 2 * 3600 * 1000,
        "n_bars": 500,
        "period": 144,
        "out_stem": "gc_radar_2h",
    },
    "4h": {
        "interval": "4h",
        "bar_ms": 4 * 3600 * 1000,
        "n_bars": 450,
        "period": 72,
        "out_stem": "gc_radar_4h",
    },
    "1d": {
        "interval": "1d",
        "bar_ms": 86400 * 1000,
        "n_bars": 280,
        "period": 144,
        "out_stem": "gc_radar_1d",
        "alias_stem": "daily_gc_radar",  # back-compat
    },
}


def gc_period_for_tf(tf: str) -> int:
    """Per-TF GC period; falls back to GC_PERIOD (1D SoT=144)."""
    return int(TF_CONFIG.get(tf, {}).get("period") or GC_PERIOD)



# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def hl_post(body: dict, timeout: float = 30.0, retries: int = 4) -> Any:
    """POST to HL info with polite retries on 429/5xx."""
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            if requests is not None:
                r = requests.post(HL_INFO, json=body, timeout=timeout)
                if r.status_code == 429 or r.status_code >= 500:
                    raise RuntimeError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r.json()
            # stdlib fallback
            import urllib.request

            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                HL_INFO,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            sleep_s = 1.5 * (2 ** attempt)
            print(
                f"[retry] hl_post attempt={attempt+1}/{retries} sleep={sleep_s:.1f}s err={e}",
                file=sys.stderr,
            )
            time.sleep(sleep_s)
    assert last_err is not None
    raise last_err


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
def load_base_v0(path: str = CANDIDATES_PATH, max_n: int = MAX_SYMBOLS) -> Optional[List[str]]:
    """Optional secondary filter list (not primary universe)."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        syms = data.get("symbols") or []
        if not isinstance(syms, list) or len(syms) < 10:
            return None
        names: List[str] = []
        for s in syms:
            if isinstance(s, dict) and s.get("symbol"):
                names.append(str(s["symbol"]))
            elif isinstance(s, str):
                names.append(s)
        names = [n for n in names if n]
        if len(names) < 10:
            return None
        return names[:max_n]
    except Exception as e:
        print(f"[warn] base_v0 load failed: {e}", file=sys.stderr)
        return None


def fetch_meta_universe() -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Returns ([{name, day_ntl_vlm, open_interest}, ...] sorted desc by dayNtlVlm),
    and meta universe names in exchange order (non-delisted).
    Pulls openInterest from assetCtxs when present (soft OI > 0 gate).
    """
    payload = hl_post({"type": "metaAndAssetCtxs"})
    meta, ctxs = payload[0], payload[1]
    universe = meta.get("universe") or []
    rows: List[Dict[str, Any]] = []
    names_order: List[str] = []
    for i, u in enumerate(universe):
        name = u.get("name")
        if not name or u.get("isDelisted"):
            continue
        names_order.append(name)
        ctx = ctxs[i] if i < len(ctxs) else {}
        try:
            vol = float(ctx.get("dayNtlVlm") or 0)
        except (TypeError, ValueError):
            vol = 0.0
        oi_raw = ctx.get("openInterest")
        try:
            oi = float(oi_raw) if oi_raw is not None and oi_raw != "" else None
        except (TypeError, ValueError):
            oi = None
        rows.append({"name": name, "day_ntl_vlm": vol, "open_interest": oi})
    rows.sort(key=lambda x: x["day_ntl_vlm"], reverse=True)
    return rows, names_order


def load_hl_volume_json(path: str = HL_VOLUME_JSON, max_n: int = 100) -> List[str]:
    """Cached HL dayNtlVlm top100 file (fallback when live vols are zero)."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        top = data.get("top100") or []
        names: List[str] = []
        for row in top:
            if isinstance(row, dict) and row.get("symbol"):
                names.append(str(row["symbol"]))
            elif isinstance(row, str):
                names.append(row)
        return [n for n in names if n][:max_n]
    except Exception as e:
        print(f"[warn] hl_volume_top100.json load failed: {e}", file=sys.stderr)
        return []


def pad_from_meta(names: List[str], meta_names: List[str], max_n: int) -> List[str]:
    """Pad symbol list up to max_n using meta universe names (preserve order, unique)."""
    seen = set(names)
    out = list(names)
    for n in meta_names:
        if len(out) >= max_n:
            break
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out[:max_n]


def load_universe(max_n: int = MAX_SYMBOLS) -> Tuple[List[str], str, Dict[str, Any]]:
    """
    Primary: HL metaAndAssetCtxs ranked by dayNtlVlm, with liquidity floors.
    Floor: dayNtlVlm >= MIN_DAY_NTL_VLM ($75k) OR top-N by volume then pad;
    soft OI > 0 when openInterest present. Exclude delisted / zero.
    Target ~200–400 liquid HL names (default MAX_SYMBOLS=280).
    If live dayNtlVlm mostly zero: hl_volume_top100.json then pad from meta.
    base_v0 is secondary/filter only (prefer overlap; never primary).
    Returns (names, universe_source, meta) where meta has floors + per-symbol ctx.
    """
    vol_rows, meta_names = fetch_meta_universe()
    ctx_by: Dict[str, Dict[str, Any]] = {r["name"]: r for r in vol_rows}
    nonzero = sum(1 for r in vol_rows if r["day_ntl_vlm"] > 0)
    floors = {
        "min_day_ntl_vlm": MIN_DAY_NTL_VLM,
        "require_oi_positive": REQUIRE_OI_POSITIVE,
        "max_symbols": max_n,
    }

    if nonzero >= max(20, max_n // 5):
        liquid = [
            r for r in vol_rows
            if r["day_ntl_vlm"] >= MIN_DAY_NTL_VLM
            and (not REQUIRE_OI_POSITIVE or r.get("open_interest") is None or r["open_interest"] > 0)
        ]
        # If floor too strict for target size, fall back to top-N by volume (still exclude 0).
        if len(liquid) < min(max_n, 80):
            print(
                f"[warn] dayNtlVlm>={MIN_DAY_NTL_VLM:.0f} yielded only {len(liquid)}; "
                f"using top-{max_n} by volume (vol>0)",
                file=sys.stderr,
            )
            liquid = [r for r in vol_rows if r["day_ntl_vlm"] > 0]
            src = f"hl_dayNtlVlm_top{max_n}_volpad"
        else:
            src = f"hl_dayNtlVlm_ge{int(MIN_DAY_NTL_VLM)}"
        names = [r["name"] for r in liquid[:max_n]]
        # Pad with next-highest vol>0 if under max_n after OI soft filter
        if len(names) < max_n:
            names = pad_from_meta(names, [r["name"] for r in vol_rows if r["day_ntl_vlm"] > 0], max_n)
            src = src + "+vol_pad"
    else:
        print(
            f"[warn] dayNtlVlm mostly zero (nonzero={nonzero}); "
            f"falling back to {HL_VOLUME_JSON}",
            file=sys.stderr,
        )
        cached = load_hl_volume_json(max_n=min(100, max_n))
        if cached:
            names = pad_from_meta(cached, meta_names, max_n)
            src = "hl_volume_top100.json+meta_pad"
        else:
            names = meta_names[:max_n]
            src = "hl_meta_universe"

    base = load_base_v0(max_n=max_n * 2)
    if base:
        base_set = set(base)
        preferred = [n for n in names if n in base_set]
        rest = [n for n in names if n not in base_set]
        filtered = (preferred + rest)[:max_n]
        if len(preferred) >= 10:
            print(
                f"[info] base_v0 secondary filter: {len(preferred)}/{len(names)} overlap "
                f"(kept volume order, padded to {len(filtered)})",
                file=sys.stderr,
            )
            names = filtered
            src = src + "+base_v0_filter"

    meta = {
        "floors": floors,
        "n_meta_live": len(vol_rows),
        "n_nonzero_day_ntl": nonzero,
        "ctx_by_symbol": {
            n: {
                "day_ntl_vlm": (ctx_by.get(n) or {}).get("day_ntl_vlm"),
                "open_interest": (ctx_by.get(n) or {}).get("open_interest"),
            }
            for n in names
        },
    }
    return names, src, meta


# ---------------------------------------------------------------------------
# Candles
# ---------------------------------------------------------------------------
def fetch_candles(coin: str, tf: str) -> List[dict]:
    """
    HL candleSnapshot for interval tf (1h|2h|4h|1d).
    Returns list of {t, open, high, low, close, volume}.
    """
    cfg = TF_CONFIG[tf]
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(cfg["n_bars"]) * int(cfg["bar_ms"])
    # pad start a bit for incomplete first bar / clock skew
    start_ms -= int(cfg["bar_ms"])
    body = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": cfg["interval"],
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }
    time.sleep(REQUEST_PAUSE_S)
    raw = hl_post(body)
    if not isinstance(raw, list):
        return []
    bars: List[dict] = []
    for c in raw:
        try:
            bars.append(
                {
                    "t": int(c["t"]),
                    "open": float(c["o"]),
                    "high": float(c["h"]),
                    "low": float(c["l"]),
                    "close": float(c["c"]),
                    "volume": float(c.get("v") or 0),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    bars.sort(key=lambda b: b["t"])
    return bars


# ---------------------------------------------------------------------------
# Gaussian Channel (DW / Signum v3.3)
# ---------------------------------------------------------------------------
def _true_range(highs: List[float], lows: List[float], closes: List[float], i: int) -> float:
    if i == 0:
        return highs[0] - lows[0]
    prev_c = closes[i - 1]
    return max(highs[i] - lows[i], abs(highs[i] - prev_c), abs(lows[i] - prev_c))


def _pole_weights(i: int) -> Tuple[int, int, int, int, int, int, int, int]:
    table = {
        1: (0, 0, 0, 0, 0, 0, 0, 0),
        2: (1, 0, 0, 0, 0, 0, 0, 0),
        3: (3, 1, 0, 0, 0, 0, 0, 0),
        4: (6, 4, 1, 0, 0, 0, 0, 0),
        5: (10, 10, 5, 1, 0, 0, 0, 0),
        6: (15, 20, 15, 6, 1, 0, 0, 0),
        7: (21, 35, 35, 21, 7, 1, 0, 0),
        8: (28, 56, 70, 56, 28, 8, 1, 0),
        9: (36, 84, 126, 126, 84, 36, 9, 1),
    }
    return table[i]


def pole_filter(alpha: float, data: List[float], N: int) -> Tuple[List[float], List[float]]:
    """Stateful N-pole Ehlers gaussian filter. Returns (fn, f1)."""
    n = len(data)
    x = 1.0 - alpha
    f: List[List[float]] = [[0.0] * n for _ in range(10)]

    for i in range(1, N + 1):
        m2, m3, m4, m5, m6, m7, m8, m9 = _pole_weights(i)
        a_pow = alpha**i
        for t in range(n):
            s = data[t]
            v = a_pow * s + i * x * (f[i][t - 1] if t >= 1 else 0.0)
            if i >= 2:
                v -= m2 * (x**2) * (f[i][t - 2] if t >= 2 else 0.0)
            if i >= 3:
                v += m3 * (x**3) * (f[i][t - 3] if t >= 3 else 0.0)
            if i >= 4:
                v -= m4 * (x**4) * (f[i][t - 4] if t >= 4 else 0.0)
            if i >= 5:
                v += m5 * (x**5) * (f[i][t - 5] if t >= 5 else 0.0)
            if i >= 6:
                v -= m6 * (x**6) * (f[i][t - 6] if t >= 6 else 0.0)
            if i >= 7:
                v += m7 * (x**7) * (f[i][t - 7] if t >= 7 else 0.0)
            if i >= 8:
                v -= m8 * (x**8) * (f[i][t - 8] if t >= 8 else 0.0)
            if i == 9:
                v += m9 * (x**9) * (f[i][t - 9] if t >= 9 else 0.0)
            f[i][t] = v
    return f[N], f[1]


def compute_gc(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    poles: int = GC_POLES,
    period: int = GC_PERIOD,
    mult: float = GC_MULT,
    reduced_lag: bool = GC_REDUCED_LAG,
    fast_response: bool = GC_FAST_RESPONSE,
) -> List[Dict[str, float]]:
    n = len(closes)
    if n == 0:
        return []

    beta = (1.0 - math.cos((4.0 * math.asin(1.0)) / period)) / (math.pow(1.414, 2.0 / poles) - 1.0)
    alpha = -beta + math.sqrt(beta * beta + 2.0 * beta)
    lag = int(round((period - 1) / (2.0 * poles)))

    src = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(n)]
    tr = [_true_range(highs, lows, closes, i) for i in range(n)]

    if reduced_lag:
        srcdata = [src[i] + (src[i] - src[i - lag]) if i >= lag else src[i] for i in range(n)]
        trdata = [tr[i] + (tr[i] - tr[i - lag]) if i >= lag else tr[i] for i in range(n)]
    else:
        srcdata, trdata = src, tr

    filtn, filt1 = pole_filter(alpha, srcdata, poles)
    filtntr, filt1tr = pole_filter(alpha, trdata, poles)

    out: List[Dict[str, float]] = []
    for i in range(n):
        filt = (filtn[i] + filt1[i]) / 2.0 if fast_response else filtn[i]
        filttr = (filtntr[i] + filt1tr[i]) / 2.0 if fast_response else filtntr[i]
        out.append(
            {
                "filter": filt,
                "upper": filt + filttr * mult,
                "lower": filt - filttr * mult,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Live (forming-bar) values + candle cache (display only; signals stay closed-bar)
# ---------------------------------------------------------------------------
_BARS_CACHE: Dict[Tuple[str, str], List[dict]] = {}
_BARS_LOCK = threading.Lock()


def candles_cache_path(tf: str) -> str:
    return os.path.join(OUT_DIR, f"candles_{tf}.json.gz")


def live_values(bars: List[dict], gc: List[Dict[str, float]], live_i: int, now_ms: int, bar_ms: int) -> dict:
    """GC values on the latest (possibly forming) bar. DISPLAY ONLY — never a signal input."""
    li = live_i
    close = bars[li]["close"]
    filt, upper, lower = gc[li]["filter"], gc[li]["upper"], gc[li]["lower"]
    pf = gc[li - 1]["filter"] if li >= 1 else filt
    pu = gc[li - 1]["upper"] if li >= 1 else upper
    pc = bars[li - 1]["close"] if li >= 1 else close
    return {
        "close": round(close, 8),
        "filter": round(filt, 8),
        "upper": round(upper, 8),
        "lower": round(lower, 8),
        "trend": "Green" if filt > pf else "Red",
        "above_upper": close > upper,
        "cross_up": close > upper and pc <= pu,
        "bar_time": bars[li]["t"],
        "forming": bars[li]["t"] + bar_ms > now_ms,
    }


def write_candles_cache(tf: str, symbols: List[str]) -> Optional[str]:
    """Persist bars (t,o,h,l,c,v) per coin so the live loop only needs allMids (1 HL request)."""
    with _BARS_LOCK:
        data = {s: [[b["t"], b["open"], b["high"], b["low"], b["close"], b["volume"]]
                    for b in _BARS_CACHE.get((tf, s), [])] for s in symbols}
    data = {k: v for k, v in data.items() if v}
    if not data:
        return None
    path = candles_cache_path(tf)
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump({"tf": tf, "ts": datetime.now(timezone.utc).isoformat(), "bars": data}, f, separators=(",", ":"))
    os.replace(tmp, path)
    return path


def radar_timing(tf: str, rows: List[dict], now_ms: Optional[int] = None) -> dict:
    """Closed/forming bar timestamps (ms, UTC) for a radar payload."""
    bar_ms = int(TF_CONFIG[tf]["bar_ms"])
    now_ms = now_ms or int(time.time() * 1000)
    closed = [r.get("bar_time") for r in rows if isinstance(r.get("bar_time"), int)]
    closed_open = max(closed) if closed else None
    forming_open = now_ms - now_ms % bar_ms
    return {
        "closed_bar_open_ms": closed_open,
        "closed_bar_close_ms": closed_open + bar_ms if closed_open else None,
        "forming_bar_open_ms": forming_open,
        "next_close_ms": forming_open + bar_ms,
    }


# ---------------------------------------------------------------------------
# Per-symbol scan row
# ---------------------------------------------------------------------------
def _median(xs: List[float]) -> Optional[float]:
    ys = [x for x in xs if x is not None and x == x]  # drop NaN
    if not ys:
        return None
    ys.sort()
    m = len(ys)
    mid = m // 2
    if m % 2:
        return float(ys[mid])
    return float((ys[mid - 1] + ys[mid]) / 2.0)


def compute_vol_momentum(bars: List[dict], i: int, lookback: int = RVOL_LOOKBACK) -> Dict[str, Any]:
    """
    Volume acceleration / RVOL from already-fetched candles (observe/rank only).
    rvol = last closed bar volume / median(prior lookback volumes)
    vol_change_pct = (v_today - v_yday) / v_yday * 100
    mom_score = log1p(rvol)  (ranking only; does NOT gate dual_cross_up)
    """
    vols = [float(b.get("volume") or 0) for b in bars]
    v_today = vols[i] if 0 <= i < len(vols) else 0.0
    v_yday = vols[i - 1] if i >= 1 else 0.0
    prior = vols[max(0, i - lookback) : i]
    med = _median(prior)
    rvol = (v_today / med) if med and med > 0 else None
    vol_change_pct = ((v_today - v_yday) / v_yday * 100.0) if v_yday > 0 else None
    if rvol is not None and rvol > 0:
        mom_score = round(math.log1p(rvol), 6)
    else:
        mom_score = 0.0
    # mild accel tilt when day-over-day vol rising
    if vol_change_pct is not None and vol_change_pct > 0:
        mom_score = round(mom_score + 0.05 * math.tanh(vol_change_pct / 100.0), 6)
    return {
        "rvol": round(rvol, 4) if rvol is not None else None,
        "vol_change_pct": round(vol_change_pct, 2) if vol_change_pct is not None else None,
        "vol_accel": round(rvol, 4) if rvol is not None else None,  # alias = RVOL
        "mom_score": mom_score,
        "v_today": round(v_today, 4),
        "v_yday": round(v_yday, 4),
    }


def classify_upper_status(
    open_: float,
    high: float,
    close: float,
    upper: float,
    prev_close: float,
    prev_upper: float,
) -> str:
    """Early-entry watch vs GC Upper (observe only; does NOT change dual_cross SoT).

    Labels (single): below | wick | open | close | upper
    Priority: riding/cross first, then open-above, wick, else below.
    """
    if close > upper and prev_close > prev_upper:
        return "upper"
    if close > upper and prev_close <= prev_upper:
        return "close"
    # close back at/below Upper
    if open_ >= upper:
        return "open"
    if high >= upper:
        return "wick"
    return "below"


def scan_symbol(coin: str, tf: str, ctx: Optional[Dict[str, Any]] = None) -> Optional[dict]:
    period = gc_period_for_tf(tf)
    bars = fetch_candles(coin, tf)
    min_bars = period + 20
    if len(bars) < min_bars:
        print(
            f"[skip] {coin}@{tf}: only {len(bars)} bars (need >={min_bars})",
            file=sys.stderr,
        )
        return None

    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    gc = compute_gc(highs, lows, closes, period=period)
    # Use latest CLOSED daily/TF bar only for dual_cross SoT (avoid forming-bar false entries)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    bar_ms = TF_CONFIG[tf]["bar_ms"]
    live_i = len(gc) - 1  # may be incomplete/forming HL candle
    i = live_i
    while i >= 1 and bars[i]["t"] + bar_ms > now_ms:
        i -= 1
    if i < 1:
        return None

    filt = gc[i]["filter"]
    prev_filt = gc[i - 1]["filter"]
    upper = gc[i]["upper"]
    prev_upper = gc[i - 1]["upper"]
    lower = gc[i]["lower"]
    close = closes[i]
    prev_close = closes[i - 1]

    # --- GC entry flags (LOCKED — do not change dual_cross math; closed bar only) ---
    trend = "Green" if filt > prev_filt else "Red"
    above_upper = close > upper
    dual_cross_up = close > upper and prev_close <= prev_upper
    dual_cross_down_filter = close < filt and prev_close >= prev_filt

    # Early-entry status: prefer forming/incomplete candle when HL returns one
    # (status only — Base entry remains closed-bar dual_cross_up).
    si = live_i if live_i >= 1 else i
    if si < 1:
        si = i
    s_upper = gc[si]["upper"]
    s_prev_upper = gc[si - 1]["upper"] if si >= 1 else prev_upper
    s_close = closes[si]
    s_prev_close = closes[si - 1] if si >= 1 else prev_close
    s_open = float(bars[si].get("open") or s_close)
    s_high = float(bars[si].get("high") or max(s_open, s_close))
    upper_status = classify_upper_status(
        s_open, s_high, s_close, s_upper, s_prev_close, s_prev_upper
    )
    last_cross_up_at = None
    for j in range(i, 0, -1):
        if closes[j - 1] <= gc[j - 1]["upper"] and closes[j] > gc[j]["upper"]:
            last_cross_up_at = bars[j]["t"]
            break

    ath = max(highs)
    atl = min(lows)
    drop_from_ath_pct = round((ath - close) / ath * 100, 2) if ath else None
    from_atl_pct = round((close - atl) / atl * 100, 2) if atl else None

    mom = compute_vol_momentum(bars, i)
    live = live_values(bars, gc, live_i, now_ms, bar_ms)
    with _BARS_LOCK:
        _BARS_CACHE[(tf, coin)] = bars
    ctx = ctx or {}
    day_ntl = ctx.get("day_ntl_vlm")
    oi = ctx.get("open_interest")
    try:
        day_ntl_f = float(day_ntl) if day_ntl is not None else None
    except (TypeError, ValueError):
        day_ntl_f = None
    try:
        oi_f = float(oi) if oi is not None else None
    except (TypeError, ValueError):
        oi_f = None

    return {
        "symbol": coin,
        "close": round(close, 8),
        "filter": round(filt, 8),
        "upper": round(upper, 8),
        "lower": round(lower, 8),
        "trend": trend,
        "above_upper": above_upper,
        "dual_cross_up": dual_cross_up,
        "dual_cross_down_filter": dual_cross_down_filter,
        "upper_status": upper_status,
        "ath": round(ath, 8),
        "atl": round(atl, 8),
        "drop_from_ath_pct": drop_from_ath_pct,
        "from_atl_pct": from_atl_pct,
        "last_cross_up_at": last_cross_up_at,
        "bars": len(bars),
        "bar_time": bars[i]["t"],  # open time of the LAST CLOSED bar (top-level = closed SoT)
        # live = forming bar at scan time (display only; never a signal input).
        # serve.py live_radar refreshes it every 10 min from allMids.
        "live": live,
        # momentum / liquidity observe-only (ranking; not entry gates)
        "day_ntl_vlm": round(day_ntl_f, 2) if day_ntl_f is not None else None,
        "open_interest": round(oi_f, 4) if oi_f is not None else None,
        "rvol": mom["rvol"],
        "vol_accel": mom["vol_accel"],
        "vol_change_pct": mom["vol_change_pct"],
        "mom_score": mom["mom_score"],
    }


# ---------------------------------------------------------------------------
# Scan one TF + write outputs
# ---------------------------------------------------------------------------
CSV_FIELDS = [
    "symbol",
    "close",
    "filter",
    "upper",
    "lower",
    "trend",
    "above_upper",
    "dual_cross_up",
    "dual_cross_down_filter",
    "upper_status",
    "ath",
    "atl",
    "drop_from_ath_pct",
    "from_atl_pct",
    "bars",
    "bar_time",
    "day_ntl_vlm",
    "open_interest",
    "rvol",
    "vol_accel",
    "vol_change_pct",
    "mom_score",
    "mom_rank",
    "tier",
    "mcap_usd",
    "mcap_known",
    "category",
]


def scan_tf(
    tf: str,
    symbols: List[str],
    universe_src: str,
    concurrency: int = CONCURRENCY,
    universe_meta: Optional[Dict[str, Any]] = None,
) -> dict:
    """Scan all symbols for one TF; write JSON+CSV; return summary payload."""
    t0 = time.time()
    ts = datetime.now(timezone.utc).isoformat()
    cfg = TF_CONFIG[tf]
    universe_meta = universe_meta or {}
    ctx_by = universe_meta.get("ctx_by_symbol") or {}
    print(f"\n[tf={tf}] scanning n={len(symbols)} interval={cfg['interval']} "
          f"n_bars~={cfg['n_bars']} concurrency={concurrency}")

    rows: List[dict] = []
    errors: List[str] = []

    def _job(sym: str) -> Tuple[str, Optional[dict], Optional[str]]:
        try:
            return sym, scan_symbol(sym, tf, ctx=ctx_by.get(sym)), None
        except Exception as e:
            return sym, None, str(e)

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {ex.submit(_job, s): s for s in symbols}
        for fut in as_completed(futs):
            sym, row, err = fut.result()
            if err:
                print(f"[err] {sym}@{tf}: {err}", file=sys.stderr)
                errors.append(f"{sym}: {err}")
            elif row:
                rows.append(row)
                flag = ""
                if row["dual_cross_up"]:
                    flag = " DUAL_UP"
                elif row["dual_cross_down_filter"]:
                    flag = " DUAL_DN_FILT"
                mom = row.get("mom_score")
                mom_s = f" mom={mom:.2f}" if isinstance(mom, (int, float)) else ""
                print(f"  [{tf}] {row['symbol']:12s} {row['trend']:5s} close={row['close']}{flag}{mom_s}")

    # mom_rank: 1 = highest mom_score (observe/priority only)
    by_mom = sorted(rows, key=lambda r: (-(r.get("mom_score") or 0), r["symbol"]))
    rank_map = {r["symbol"]: i + 1 for i, r in enumerate(by_mom)}
    for r in rows:
        r["mom_rank"] = rank_map.get(r["symbol"])

    # Size tier (mcap) + category sleeve (Narrative / Cemetery / Price)
    narr = load_narrative_tickers()
    mcap_map = load_mcap_map() if load_mcap_map else {}
    for r in rows:
        enrich_row_tier_category(r, narr, mcap_map)

    # Display/candidate priority: dual_cross_up first, then mom_score, then symbol.
    # Does NOT change which rows get dual_cross_up=true (GC only).
    rows.sort(
        key=lambda r: (
            0 if r.get("dual_cross_up") else 1,
            -(r.get("mom_score") or 0),
            r["symbol"],
        )
    )

    green_count = sum(1 for r in rows if r["trend"] == "Green")
    red_count = sum(1 for r in rows if r["trend"] == "Red")
    n = len(rows)
    green_pct = round(100.0 * green_count / n, 2) if n else 0.0

    dual_up = [r["symbol"] for r in rows if r["dual_cross_up"]]
    dual_dn = [r["symbol"] for r in rows if r["dual_cross_down_filter"]]
    above = [r["symbol"] for r in rows if r["above_upper"]]

    floors = (universe_meta.get("floors") or {
        "min_day_ntl_vlm": MIN_DAY_NTL_VLM,
        "require_oi_positive": REQUIRE_OI_POSITIVE,
        "max_symbols": len(symbols),
    })
    payload = {
        "tf": tf,
        "ts": ts,
        "dry_run": True,
        "gc_params": {
            "source": "hlc3",
            "poles": GC_POLES,
            "period": gc_period_for_tf(tf),
            "mult": GC_MULT,
            "reducedLag": GC_REDUCED_LAG,
            "fastResponse": GC_FAST_RESPONSE,
        },
        "universe_source": universe_src,
        "universe_requested": symbols,
        "n_scanned": n,
        "universe_floors": floors,
        "momentum_note": (
            "rvol/vol_accel/vol_change_pct/mom_score/mom_rank are observe+rank only; "
            "dual_cross_up GC entry math unchanged"
        ),
        "errors": errors,
        "breadth": {
            "green_count": green_count,
            "red_count": red_count,
            "green_pct": green_pct,
            "n": n,
        },
        "row_semantics": "top-level row fields = LAST CLOSED bar (signal SoT); row.live = forming bar (display only)",
        "live_ts": ts,
        **radar_timing(tf, rows),
        "flags": {
            "dual_cross_up": dual_up,
            "dual_cross_down_filter": dual_dn,
            "above_upper": above,
        },
        "rows": rows,
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    stem = cfg["out_stem"]
    json_path = os.path.join(OUT_DIR, f"{stem}.json")
    csv_path = os.path.join(OUT_DIR, f"{stem}.csv")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # Back-compat alias for 1d
    alias = cfg.get("alias_stem")
    if alias:
        alias_json = os.path.join(OUT_DIR, f"{alias}.json")
        alias_csv = os.path.join(OUT_DIR, f"{alias}.csv")
        shutil.copy2(json_path, alias_json)
        shutil.copy2(csv_path, alias_csv)
        print(f"[done] alias {alias_json} / {alias_csv}")

    try:
        cp = write_candles_cache(tf, [r["symbol"] for r in rows])
        print(f"[done] candle cache {cp}")
    except Exception as e:  # cache is an optimisation; never fail the scan
        print(f"[warn] candle cache write failed: {e}", file=sys.stderr)

    elapsed = time.time() - t0
    print(f"[done] wrote {json_path}")
    print(f"[done] wrote {csv_path}")
    print(f"[tf={tf}] breadth green={green_count} red={red_count} green_pct={green_pct}% n={n} "
          f"dual_up={len(dual_up)} elapsed={elapsed:.1f}s")
    print(f"[tf={tf}] dual_cross_up ({len(dual_up)}): {', '.join(dual_up) if dual_up else '(none)'}")

    payload["_elapsed_s"] = elapsed
    payload["_json_path"] = json_path
    payload["_csv_path"] = csv_path
    return payload


def parse_tfs(arg: Optional[str]) -> List[str]:
    if not arg or arg.strip().lower() in ("all", "*"):
        return list(VALID_TFS)
    parts = [p.strip().lower() for p in arg.split(",") if p.strip()]
    # normalize aliases
    norm_map = {"2H": "2h", "4H": "4h", "1D": "1d", "d": "1d", "2h": "2h", "4h": "4h", "1d": "1d"}
    out: List[str] = []
    for p in parts:
        p2 = norm_map.get(p, norm_map.get(p.upper(), p))
        if p2 not in VALID_TFS:
            raise SystemExit(f"Unknown tf={p!r}; valid: {', '.join(VALID_TFS)}")
        if p2 not in out:
            out.append(p2)
    # Prefer stable order: 2h, 4h, 1d
    return [t for t in VALID_TFS if t in out]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Own Trend Radar multi-TF GC scan (DRY_RUN)")
    ap.add_argument(
        "--tf",
        default="all",
        help="Timeframe(s): 2h|4h|1d or comma-list, or 'all' (default)",
    )
    ap.add_argument("--max", type=int, default=MAX_SYMBOLS, help=f"Max symbols (default {MAX_SYMBOLS})")
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY, help=f"Workers (default {CONCURRENCY})")
    args = ap.parse_args(argv)

    tfs = parse_tfs(args.tf)
    max_n = max(1, int(args.max))
    concurrency = max(1, min(8, int(args.concurrency)))

    os.makedirs(OUT_DIR, exist_ok=True)
    t_all = time.time()

    symbols, universe_src, universe_meta = load_universe(max_n)
    floors = universe_meta.get("floors") or {}
    print(
        f"[info] universe={universe_src} n={len(symbols)} concurrency={concurrency} "
        f"floor_dayNtlVlm>={floors.get('min_day_ntl_vlm')} oi_soft={floors.get('require_oi_positive')}"
    )
    print(f"[info] TFs={','.join(tfs)}")
    print(
        f"[info] GC poles={GC_POLES} period={GC_PERIOD} mult={GC_MULT} "
        f"lag={GC_REDUCED_LAG} fast={GC_FAST_RESPONSE} (entry unchanged)"
    )

    summaries: List[dict] = []
    # Sequential TFs with shared concurrency pool per TF — avoids N×TFs hammering HL
    for tf in tfs:
        payload = scan_tf(
            tf, symbols, universe_src, concurrency=concurrency, universe_meta=universe_meta
        )
        summaries.append(payload)

    total = time.time() - t_all
    print()
    print("=" * 60)
    print(f"[ALL DONE] TFs={','.join(tfs)} total_elapsed={total:.1f}s")
    for p in summaries:
        b = p.get("breadth") or {}
        flags = p.get("flags") or {}
        dual = flags.get("dual_cross_up") or []
        print(
            f"  {p.get('tf')}: n={b.get('n')} green_pct={b.get('green_pct')}% "
            f"dual_cross_up={len(dual)} → {p.get('_json_path')}"
        )
    print("=" * 60)

    # Fail if any TF scanned too few
    bad = [p for p in summaries if (p.get("n_scanned") or 0) < 20]
    if bad:
        for p in bad:
            print(f"[warn] tf={p.get('tf')} scanned only {p.get('n_scanned')} (<20)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
