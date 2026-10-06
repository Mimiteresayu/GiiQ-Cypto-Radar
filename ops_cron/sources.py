"""Read-only data sources. Cockpit: HTTP GET only, key in the X-AI-Key header (never in the URL).
Hyperliquid: the PUBLIC info API (POST /info is HL's read endpoint) limited to the read types below; no key.

Every call returns {"ok": bool, "data": <json>, "error": str, "http": int|None} and never raises.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from . import rules

HL_READ_TYPES = frozenset({
    "clearinghouseState", "spotClearinghouseState", "frontendOpenOrders", "userFills",
    "userFillsByTime", "userFunding", "candleSnapshot", "allMids",
})
_HL_NO_USER = frozenset({"candleSnapshot", "allMids"})
_HL_DICT_TYPES = frozenset({"clearinghouseState", "spotClearinghouseState", "allMids"})
_HL_LIST_TYPES = frozenset({
    "frontendOpenOrders", "userFills", "userFillsByTime", "userFunding", "candleSnapshot",
})
# First try, then up to three retries. Wait 1s, 2s, 4s between them.
_HL_429_BACKOFF_S = (1, 2, 4)
HL_FILLS_LOOKBACK_DAYS = 30


def normalize_hl_address(raw: Optional[str]) -> str:
    """Lowercase is normalisation only. HL returned null with HTTP 429 (rate limit), not because of address case."""
    return (raw or "").strip().lower()


def _res(ok: bool, data: Any = None, error: str = "", http: Optional[int] = None) -> Dict[str, Any]:
    return {"ok": ok, "data": data, "error": error, "http": http}


def _call(req: urllib.request.Request, timeout: float) -> Dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _res(True, json.loads(r.read().decode() or "null"), http=r.status)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode() or "{}")
            msg = body.get("error") if isinstance(body, dict) else None
        except Exception:  # noqa: BLE001
            msg = None
        return _res(False, error=f"HTTP {e.code}{': ' + str(msg)[:160] if msg else ''}", http=e.code)
    except Exception as e:  # noqa: BLE001  timeouts, DNS, TLS, bad JSON
        return _res(False, error=f"{type(e).__name__}: {str(e)[:160]}")


def _hl_body_ok(kind: str, data: Any) -> bool:
    """null and the wrong JSON shape are a failed read, not an empty book."""
    if kind in _HL_DICT_TYPES:
        return isinstance(data, dict)
    if kind in _HL_LIST_TYPES:
        return isinstance(data, list)
    return data is not None


class Sources:
    def __init__(self, cockpit_url: str, ai_key: str, hl_address: str, hl_info_url: str, timeout: float = 20.0,
                 bx_base: str = "https://fapi.bitunix.com"):
        self.cockpit_url = (cockpit_url or "").rstrip("/")
        self.ai_key = ai_key or ""
        self.hl_address = normalize_hl_address(hl_address)
        self.hl_info_url = hl_info_url
        self.timeout = timeout
        self.bx_base = bx_base or "https://fapi.bitunix.com"

    def cockpit(self, path: str) -> Dict[str, Any]:
        if not self.cockpit_url or not self.ai_key:
            return _res(False, error="COCKPIT_URL / COCKPIT_AI_KEY not set")
        req = urllib.request.Request(self.cockpit_url + path, method="GET",
                                     headers={"X-AI-Key": self.ai_key, "Accept": "application/json"})
        return _call(req, self.timeout)

    def hl(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if body.get("type") not in HL_READ_TYPES:
            raise ValueError(f"HL info type {body.get('type')!r} is not an allowed read type")
        kind = str(body.get("type") or "")
        if kind not in _HL_NO_USER and not self.hl_address:
            return _res(False, error="HL_ADDRESS not set")
        sent = dict(body)
        if kind not in _HL_NO_USER:
            sent["user"] = self.hl_address
        last = _res(False, error="HL info not called")
        for attempt in range(len(_HL_429_BACKOFF_S) + 1):
            req = urllib.request.Request(self.hl_info_url, data=json.dumps(sent).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            last = _call(req, self.timeout)
            if last.get("http") != 429 or attempt == len(_HL_429_BACKOFF_S):
                break
            time.sleep(_HL_429_BACKOFF_S[attempt])
        if not last.get("ok") or last.get("http") not in (None, 200):
            if last.get("ok") and last.get("http") not in (None, 200):
                return _res(False, error=f"HTTP {last.get('http')}", http=last.get("http"))
            return last
        if not _hl_body_ok(kind, last.get("data")):
            return _res(False, error="HL info unreadable (null or unexpected body)", http=last.get("http"))
        return last

    def cockpit_public(self, path: str) -> Dict[str, Any]:
        """GET a cockpit route that does not take the AI key (for example /api/public/radar)."""
        if not self.cockpit_url:
            return _res(False, error="COCKPIT_URL not set")
        if "key=" in path.lower():
            raise ValueError("cockpit_public refuses a key in the URL")
        req = urllib.request.Request(self.cockpit_url + path, method="GET", headers={"Accept": "application/json"})
        return _call(req, self.timeout)

    def bx_klines(self, symbol: str, interval: str = "1h", limit: int = 200) -> Dict[str, Any]:
        """Bitunix public futures kline. GET only, no key."""
        import urllib.parse
        q = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": str(min(int(limit), 200))})
        url = self.bx_base.rstrip("/") + "/api/v1/futures/market/kline?" + q
        req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
        return _call(req, self.timeout)


def _hkt_day(d: datetime) -> str:
    return d.astimezone(rules.HKT).strftime("%Y-%m-%d")


def exit_monitor_inputs(src: Sources, now: datetime, lookback_min: int, bx_enabled: bool) -> Dict[str, Any]:
    start = min(rules.hkt_day_start(now), now - timedelta(minutes=lookback_min))
    inp = {
        "exit_health": src.cockpit("/api/exit/health"),
        "scheduler": src.cockpit("/api/scheduler/status"),
        "hl_state": src.hl({"type": "clearinghouseState", "user": src.hl_address}),
        "hl_spot": src.hl({"type": "spotClearinghouseState", "user": src.hl_address}),
        "hl_orders": src.hl({"type": "frontendOpenOrders", "user": src.hl_address}),
        "all_mids": src.hl({"type": "allMids"}),
        "hl_fills": src.hl({"type": "userFillsByTime", "user": src.hl_address,
                            "startTime": int(start.timestamp() * 1000), "endTime": int(now.timestamp() * 1000)}),
    }
    if bx_enabled:
        inp["bx_status"] = src.cockpit("/api/bx/status")
    inp["pending"] = src.cockpit("/api/exec/pending")
    return inp


def daily_audit_inputs(src: Sources, now: datetime, bx_enabled: bool, window_h: int = 24) -> Dict[str, Any]:
    today, yday = _hkt_day(now), _hkt_day(now - timedelta(days=1))
    inp = {
        "run_report_today": src.cockpit(f"/api/exec/run-report?date={today}"),
        "run_report_yesterday": src.cockpit(f"/api/exec/run-report?date={yday}"),
        "pending": src.cockpit("/api/exec/pending"),
        "hl_fills": src.hl({"type": "userFillsByTime", "user": src.hl_address,
                            "startTime": int((now - timedelta(days=HL_FILLS_LOOKBACK_DAYS)).timestamp() * 1000),
                            "endTime": int(now.timestamp() * 1000)}),
    }
    if bx_enabled:
        inp["bx_status"] = src.cockpit("/api/bx/status")
        inp["bx_day"] = src.cockpit(f"/api/bx/day?date={today}")
    inp["hl_state"] = src.hl({"type": "clearinghouseState", "user": src.hl_address})
    inp["hl_spot"] = src.hl({"type": "spotClearinghouseState", "user": src.hl_address})
    inp["hl_orders"] = src.hl({"type": "frontendOpenOrders", "user": src.hl_address})
    inp["all_mids"] = src.hl({"type": "allMids"})
    candles: Dict[str, Any] = {}
    for req in rules.candle_requests(inp["hl_fills"], now, window_h):
        candles[req["coin"]] = src.hl({"type": "candleSnapshot", "req": {
            "coin": req["coin"], "interval": "1h", "startTime": req["start_ms"], "endTime": req["end_ms"]}})
    inp["candles"] = candles
    inp["pos_candles"] = _position_candles(src, inp["hl_state"], now)
    return inp


def _position_candles(src: Sources, state_res: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """1H and 4H candles for each open HL coin, enough bars for the GC channel."""
    from . import hlparse
    coins = [p["coin"] for p in hlparse.positions(state_res.get("data") if state_res.get("ok") else None)]
    out: Dict[str, Any] = {}
    end_ms = int(now.timestamp() * 1000)
    for coin in coins:
        out[coin] = {}
        for interval, n_bars, bar_ms in (("1h", 400, 3_600_000), ("4h", 450, 14_400_000)):
            out[coin][interval] = src.hl({"type": "candleSnapshot", "req": {
                "coin": coin, "interval": interval,
                "startTime": end_ms - n_bars * bar_ms, "endTime": end_ms}})
    return out


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
