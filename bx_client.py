#!/usr/bin/env python3
"""Bitunix futures PUBLIC market-data client (read-only, no API keys, never places orders).

Shadow / display only (Harbor-approved 2026-09-29): nothing on the HL order path imports this
module (see test_bx_isolation.py). Endpoints (documented limit 10 req/s per IP; we pace at 5):

  GET /api/v1/futures/market/trading_pairs   catalog: symbol, base, quote, symbolStatus, launchTime, delistTime, ...
  GET /api/v1/futures/market/tickers         24h: lastPrice, markPrice, quoteVol, baseVol, high, low (no bid/ask)
  GET /api/v1/futures/market/kline           symbol, interval (1h/4h/1d...), limit <= 200, startTime/endTime (ms)
  GET /api/v1/futures/market/depth           symbol, limit -> bids/asks (for spread)

Responses are {"code": 0, "data": ..., "msg": ...}; code != 0 is treated as an error.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

BASE_URL = os.environ.get("BX_BASE_URL", "https://fapi.bitunix.com").rstrip("/")
MARKET = "/api/v1/futures/market"
REQ_PER_S = float(os.environ.get("BX_REQ_PER_S", "5"))   # half the documented 10 req/s/IP
TIMEOUT_S = float(os.environ.get("BX_TIMEOUT_S", "10"))
RETRIES = 3
KLINE_MAX = 200
INTERVALS = {"1h": "1h", "4h": "4h", "1d": "1d"}
BAR_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


class BXError(RuntimeError):
    pass


class _Pacer:
    """Process-wide minimum spacing between requests (token bucket of size 1)."""

    def __init__(self, per_s: float) -> None:
        self.gap = 1.0 / max(per_s, 0.1)
        self.lock = threading.Lock()
        self.next_t = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next_t)
            self.next_t = t + self.gap
        if t > now:
            time.sleep(t - now)


_pacer = _Pacer(REQ_PER_S)
STATS: Dict[str, Any] = {"calls": 0, "errors": 0, "n429": 0, "ms_total": 0.0}


def _get(path: str, params: Optional[Dict[str, Any]] = None, opener=None) -> Any:
    """GET with pacing + up to 3 retries (backoff 1s, 2s; 429 honours Retry-After). Returns `data`."""
    q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{BASE_URL}{MARKET}{path}" + (f"?{q}" if q else "")
    op = opener or urllib.request.urlopen
    last = ""
    for attempt in range(RETRIES):
        _pacer.wait()
        t0 = time.monotonic()
        STATS["calls"] += 1
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "own-trend-radar-bx/1", "Accept": "application/json"})
            with op(req, timeout=TIMEOUT_S) as r:
                body = json.loads(r.read().decode("utf-8"))
            STATS["ms_total"] += (time.monotonic() - t0) * 1000
            if isinstance(body, dict) and str(body.get("code")) == "0":
                return body.get("data")
            last = f"api code {body.get('code') if isinstance(body, dict) else '?'}: " \
                   f"{str((body or {}).get('msg') if isinstance(body, dict) else body)[:120]}"
        except urllib.error.HTTPError as e:
            last = f"http {e.code}"
            if e.code == 429:
                STATS["n429"] += 1
                try:
                    time.sleep(min(10.0, float(e.headers.get("Retry-After") or 2)))
                except (TypeError, ValueError):
                    time.sleep(2)
                continue
        except Exception as e:  # timeout, DNS, TLS, JSON
            last = f"{type(e).__name__}: {e}"[:160]
        STATS["errors"] += 1
        if attempt < RETRIES - 1:
            time.sleep(1.0 * (attempt + 1))
    raise BXError(f"GET {path} failed after {RETRIES} tries: {last}")


def trading_pairs(opener=None) -> List[dict]:
    return list(_get("/trading_pairs", opener=opener) or [])


def tickers(opener=None) -> List[dict]:
    return list(_get("/tickers", opener=opener) or [])


def depth(symbol: str, limit: int = 5, opener=None) -> dict:
    return _get("/depth", {"symbol": symbol, "limit": limit}, opener=opener) or {}


def usd_volume(quote_vol: Any, base_vol: Any, price: Any) -> Optional[float]:
    """USD (USDT) turnover from a Bitunix quoteVol/baseVol pair.

    The public docs' example shows the two fields swapped (quoteVol "1", baseVol "60000" at price 60000),
    so we do not trust the label: whichever field is consistent with the other x price is the USD one.
    Falls back to quoteVol when neither check is conclusive."""
    try:
        q, b, p = float(quote_vol or 0), float(base_vol or 0), float(price or 0)
    except (TypeError, ValueError):
        return None
    if p <= 0:
        return q or None

    def close(x: float, y: float) -> bool:
        return x > 0 and y > 0 and abs(x - y) / max(x, y) < 0.35

    if close(q, b * p):
        return q
    if close(b, q * p):
        return b
    return q


def _bar(k: dict) -> Optional[list]:
    """Bitunix kline -> [t, o, h, l, c, usd_vol] (same shape as the HL candle cache)."""
    try:
        t = int(k.get("time") if k.get("time") is not None else k.get("t"))
        c = float(k["close"])
        return [t, float(k["open"]), float(k["high"]), float(k["low"]), c,
                float(usd_volume(k.get("quoteVol"), k.get("baseVol"), c) or 0.0)]
    except (TypeError, ValueError, KeyError):
        return None


def klines(symbol: str, tf: str, start_ms: Optional[int] = None, end_ms: Optional[int] = None,
           limit: int = KLINE_MAX, opener=None) -> List[list]:
    """One page (<= 200 bars), sorted oldest first."""
    data = _get("/kline", {"symbol": symbol, "interval": INTERVALS[tf], "limit": min(limit, KLINE_MAX),
                           "startTime": start_ms, "endTime": end_ms}, opener=opener) or []
    bars = [b for b in (_bar(k) for k in data) if b]
    return sorted(bars, key=lambda b: b[0])


def klines_history(symbol: str, tf: str, n_bars: int, now_ms: Optional[int] = None, opener=None) -> List[list]:
    """Up to n_bars most recent bars, paging backwards 200 at a time (stops when the contract's history ends)."""
    now_ms = now_ms or int(time.time() * 1000)
    out: Dict[int, list] = {}
    end = now_ms
    while len(out) < n_bars:
        page = klines(symbol, tf, end_ms=end, limit=KLINE_MAX, opener=opener)
        new = [b for b in page if b[0] not in out]
        for b in new:
            out[b[0]] = b
        if len(page) < KLINE_MAX or not new:
            break
        end = min(b[0] for b in page) - 1
    return sorted(out.values(), key=lambda b: b[0])[-n_bars:]


def spread_bp(book: dict) -> Optional[float]:
    """(best ask - best bid) / mid in basis points; None if either side is empty."""
    try:
        bid = float((book.get("bids") or [])[0][0])
        ask = float((book.get("asks") or [])[0][0])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return round((ask - bid) / ((ask + bid) / 2) * 10_000, 2)
