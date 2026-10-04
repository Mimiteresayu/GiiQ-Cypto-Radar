"""Read-only data sources. Cockpit: HTTP GET only, key in the X-AI-Key header (never in the URL).
Hyperliquid: the PUBLIC info API (POST /info is HL's read endpoint) limited to the read types below; no key.

Every call returns {"ok": bool, "data": <json>, "error": str, "http": int|None} and never raises.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from . import rules

HL_READ_TYPES = frozenset({"clearinghouseState", "frontendOpenOrders", "userFillsByTime", "candleSnapshot"})
HL_FILLS_LOOKBACK_DAYS = 30


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


class Sources:
    def __init__(self, cockpit_url: str, ai_key: str, hl_address: str, hl_info_url: str, timeout: float = 20.0):
        self.cockpit_url = (cockpit_url or "").rstrip("/")
        self.ai_key = ai_key or ""
        self.hl_address = hl_address or ""
        self.hl_info_url = hl_info_url
        self.timeout = timeout

    def cockpit(self, path: str) -> Dict[str, Any]:
        if not self.cockpit_url or not self.ai_key:
            return _res(False, error="COCKPIT_URL / COCKPIT_AI_KEY not set")
        req = urllib.request.Request(self.cockpit_url + path, method="GET",
                                     headers={"X-AI-Key": self.ai_key, "Accept": "application/json"})
        return _call(req, self.timeout)

    def hl(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if body.get("type") not in HL_READ_TYPES:
            raise ValueError(f"HL info type {body.get('type')!r} is not an allowed read type")
        if body.get("type") != "candleSnapshot" and not self.hl_address:
            return _res(False, error="HL_ADDRESS not set")
        req = urllib.request.Request(self.hl_info_url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        return _call(req, self.timeout)


def _hkt_day(d: datetime) -> str:
    return d.astimezone(rules.HKT).strftime("%Y-%m-%d")


def exit_monitor_inputs(src: Sources, now: datetime, lookback_min: int, bx_enabled: bool) -> Dict[str, Any]:
    start = min(rules.hkt_day_start(now), now - timedelta(minutes=lookback_min))
    inp = {
        "exit_health": src.cockpit("/api/exit/health"),
        "scheduler": src.cockpit("/api/scheduler/status"),
        "hl_state": src.hl({"type": "clearinghouseState", "user": src.hl_address}),
        "hl_orders": src.hl({"type": "frontendOpenOrders", "user": src.hl_address}),
        "hl_fills": src.hl({"type": "userFillsByTime", "user": src.hl_address,
                            "startTime": int(start.timestamp() * 1000), "endTime": int(now.timestamp() * 1000)}),
    }
    if bx_enabled:
        inp["bx_status"] = src.cockpit("/api/bx/status")
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
    candles: Dict[str, Any] = {}
    for req in rules.candle_requests(inp["hl_fills"], now, window_h):
        candles[req["coin"]] = src.hl({"type": "candleSnapshot", "req": {
            "coin": req["coin"], "interval": "1h", "startTime": req["start_ms"], "endTime": req["end_ms"]}})
    inp["candles"] = candles
    return inp


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
