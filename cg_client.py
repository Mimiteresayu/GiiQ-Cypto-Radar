#!/usr/bin/env python3
"""CoinGecko client for the Bitunix shadow radar (read-only; display / shadow only).

Used for: crypto-vs-unknown classification (symbol + price match), full-market ATH for the C tag,
market cap for tiers, and first-seen dates for new Bitunix contracts. Never imported by the HL order path.

Key: COINGECKO_API_KEY (Demo key -> api.coingecko.com with header x-cg-demo-api-key; without a key the
same public endpoints are used at a lower rate limit). Budget: ~12 /coins/markets pages a day (3,000 coins,
ath + ath_date + market_cap + current_price) plus one market_chart call per NEW contract.
Cache: out/cg_markets.json (refreshed at most every CG_MARKETS_TTL_H hours), out/cg_first_seen.json (kept).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
OUT_DIR = Path(os.environ.get("BX_OUT_DIR") or (ROOT / "out"))
API = "https://api.coingecko.com/api/v3"
MARKET_PAGES = int(os.environ.get("CG_MARKET_PAGES", "12"))
CG_MARKETS_TTL_H = 20.0
PACE_S = 2.2           # Demo plan: 30 calls/min
TIMEOUT_S = 20.0
FIRST_SEEN_DAYS = 90   # enough to decide "first seen >= 30 days before a new BX launch"
FIRST_SEEN_MAX_PER_RUN = 10   # new lookups per process (each BX run is its own subprocess); results are cached
_budget = {"first_seen": FIRST_SEEN_MAX_PER_RUN}


def _key() -> str:
    return (os.environ.get("COINGECKO_API_KEY") or "").strip()


def _get(path: str, params: Dict[str, Any], opener=None) -> Any:
    q = urllib.parse.urlencode(params)
    headers = {"Accept": "application/json", "User-Agent": "own-trend-radar-bx/1"}
    if _key():
        headers["x-cg-demo-api-key"] = _key()
    req = urllib.request.Request(f"{API}{path}?{q}", headers=headers)
    op = opener or urllib.request.urlopen
    last = ""
    for attempt in range(3):
        try:
            with op(req, timeout=TIMEOUT_S) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = f"http {e.code}"
            time.sleep(15 if e.code == 429 else 2 * (attempt + 1))
        except Exception as e:
            last = f"{type(e).__name__}: {e}"[:160]
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"CoinGecko {path} failed: {last}")


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def markets(force: bool = False, opener=None, sleep=time.sleep) -> Dict[str, Any]:
    """{ts, coins: [{id, symbol, current_price, market_cap, ath, ath_date}], error?} — cached ~20h.
    On failure the last cache is returned with `error` set (callers degrade: no CG match -> partial ATH)."""
    path = OUT_DIR / "cg_markets.json"
    cache = _read(path)
    if not force and cache.get("coins") and time.time() - float(cache.get("ts_epoch") or 0) < CG_MARKETS_TTL_H * 3600:
        return cache
    coins: List[dict] = []
    err = None
    for page in range(1, MARKET_PAGES + 1):
        try:
            data = _get("/coins/markets", {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250,
                                           "page": page, "sparkline": "false"}, opener=opener)
        except Exception as e:  # keep what we have: later pages only add smaller caps
            err = f"page {page}: {str(e)[:160]}"
            break
        if not data:
            break
        for c in data:
            coins.append({k: c.get(k) for k in ("id", "symbol", "current_price", "market_cap", "ath", "ath_date")})
        if len(data) < 250:
            break
        sleep(PACE_S)
    if not coins:
        cache = cache or {"coins": []}
        cache["error"] = err or "no data"
        return cache
    out = {"ts_epoch": time.time(), "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "n": len(coins), "coins": coins, "error": err}
    _write(path, out)
    return out


def by_symbol(coins: List[dict]) -> Dict[str, List[dict]]:
    idx: Dict[str, List[dict]] = {}
    for c in coins or []:
        s = str(c.get("symbol") or "").lower()
        if s:
            idx.setdefault(s, []).append(c)
    return idx


def first_seen_ms(cg_id: str, opener=None) -> Optional[int]:
    """Earliest price timestamp CoinGecko has for the coin within FIRST_SEEN_DAYS (cached forever once found).
    A coin listed longer than the window returns the window start, which is enough to call it old."""
    path = OUT_DIR / "cg_first_seen.json"
    cache = _read(path)
    if cg_id in cache:
        return cache[cg_id]
    if _budget["first_seen"] <= 0:
        return None          # asset_age stays 'unknown' this run; looked up on a later run
    _budget["first_seen"] -= 1
    try:
        data = _get(f"/coins/{urllib.parse.quote(cg_id)}/market_chart",
                    {"vs_currency": "usd", "days": FIRST_SEEN_DAYS, "interval": "daily"}, opener=opener)
        prices = (data or {}).get("prices") or []
        ms = int(min(p[0] for p in prices)) if prices else None
    except Exception:
        return None
    if ms:
        cache[cg_id] = ms
        _write(path, cache)
    return ms
