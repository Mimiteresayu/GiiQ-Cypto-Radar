"""Gaussian Channel levels used only to measure stops. Same math as scan_gc_radar (Lag/Fast off).

Periods match the live scanner: 1H=48, 4H=72, 1D=144. This module does not import the scanner.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

POLES = 4
MULT = 1.414
PERIOD = {"1h": 48, "4h": 72, "1d": 144}
BAR_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def _true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], i: int) -> float:
    if i == 0:
        return highs[0] - lows[0]
    prev_c = closes[i - 1]
    return max(highs[i] - lows[i], abs(highs[i] - prev_c), abs(lows[i] - prev_c))


def _pole_weights(i: int):
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


def pole_filter(alpha: float, data: List[float], n_poles: int):
    n = len(data)
    x = 1.0 - alpha
    f: List[List[float]] = [[0.0] * n for _ in range(10)]
    for i in range(1, n_poles + 1):
        m2, m3, m4, m5, m6, m7, m8, m9 = _pole_weights(i)
        a_pow = alpha ** i
        for t in range(n):
            v = a_pow * data[t] + i * x * (f[i][t - 1] if t >= 1 else 0.0)
            if i >= 2:
                v -= m2 * (x ** 2) * (f[i][t - 2] if t >= 2 else 0.0)
            if i >= 3:
                v += m3 * (x ** 3) * (f[i][t - 3] if t >= 3 else 0.0)
            if i >= 4:
                v -= m4 * (x ** 4) * (f[i][t - 4] if t >= 4 else 0.0)
            if i >= 5:
                v += m5 * (x ** 5) * (f[i][t - 5] if t >= 5 else 0.0)
            if i >= 6:
                v -= m6 * (x ** 6) * (f[i][t - 6] if t >= 6 else 0.0)
            if i >= 7:
                v += m7 * (x ** 7) * (f[i][t - 7] if t >= 7 else 0.0)
            if i >= 8:
                v -= m8 * (x ** 8) * (f[i][t - 8] if t >= 8 else 0.0)
            if i == 9:
                v += m9 * (x ** 9) * (f[i][t - 9] if t >= 9 else 0.0)
            f[i][t] = v
    return f[n_poles], f[1]


def compute_gc(highs: List[float], lows: List[float], closes: List[float], period: int) -> List[Dict[str, float]]:
    n = len(closes)
    if n == 0:
        return []
    beta = (1.0 - math.cos((4.0 * math.asin(1.0)) / period)) / (math.pow(1.414, 2.0 / POLES) - 1.0)
    alpha = -beta + math.sqrt(beta * beta + 2.0 * beta)
    src = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(n)]
    tr = [_true_range(highs, lows, closes, i) for i in range(n)]
    filtn, filt1 = pole_filter(alpha, src, POLES)
    filtntr, filt1tr = pole_filter(alpha, tr, POLES)
    out = []
    for i in range(n):
        filt = filtn[i]
        filttr = filtntr[i]
        out.append({"filter": filt, "upper": filt + filttr * MULT, "lower": filt - filttr * MULT})
    return out


def parse_candles(raw) -> List[dict]:
    rows = raw if isinstance(raw, list) else []
    out = []
    for c in rows:
        if not isinstance(c, dict):
            continue
        try:
            out.append({"t": int(c.get("t") if c.get("t") is not None else c["T"]),
                        "o": float(c.get("o") if c.get("o") is not None else c["c"]),
                        "h": float(c["h"]), "l": float(c["l"]), "c": float(c["c"])})
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda b: b["t"])
    return out


def last_closed_index(bars: List[dict], bar_ms: int, now_ms: int) -> Optional[int]:
    idx = None
    for i, b in enumerate(bars):
        if b["t"] + bar_ms <= now_ms:
            idx = i
    return idx


def channel_at(raw_candles, tf: str, now_ms: int) -> dict:
    """Last closed bar's filter / lower / upper. Missing data stays None (caller writes 未知)."""
    bars = parse_candles(raw_candles)
    period = PERIOD.get(tf, 144)
    bar_ms = BAR_MS.get(tf, 3_600_000)
    if len(bars) < period:
        return {"ok": False, "error": f"need {period} {tf} bars, have {len(bars)}",
                "filter": None, "lower": None, "upper": None, "t": None}
    gc = compute_gc([b["h"] for b in bars], [b["l"] for b in bars], [b["c"] for b in bars], period)
    i = last_closed_index(bars, bar_ms, now_ms)
    if i is None:
        return {"ok": False, "error": "no closed bar", "filter": None, "lower": None, "upper": None, "t": None}
    g = gc[i]
    return {"ok": True, "error": "", "filter": g["filter"], "lower": g["lower"], "upper": g["upper"],
            "t": bars[i]["t"], "close": bars[i]["c"]}
