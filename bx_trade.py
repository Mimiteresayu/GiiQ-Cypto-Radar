#!/usr/bin/env python3
"""Bitunix futures PRIVATE client for the live pilot — trade endpoints only.

MMT-approved 2026-09-30. Hard rules:
  * Keys come ONLY from Railway variables BX_API_KEY / BX_API_SECRET. They are never logged, printed,
    written to disk or put in an exception message (redact() + tests).
  * No withdrawal / transfer / sub-account endpoint exists in this module (tested). The key itself must be
    created trade-only, without withdrawal, IP-whitelisted to the static egress IP (MMT does this).
  * dry_run=True never sends anything: it records the exact signed request (headers redacted) so the
    order payload can be reviewed (python bx_live.py dry-run).

Signing (Bitunix docs, common/sign): digest = sha256(nonce + timestamp + api_key + queryParams + body);
sign = sha256(digest + secret). queryParams = keys sorted ASCII, "k1v1k2v2" (no separators); body = the exact
compact JSON sent on the wire (no spaces). timestamp in ms. Error codes: 10004 IP not whitelisted,
10007 bad signature, 20003 insufficient balance, 30001 order would liquidate.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

BASE_URL = os.environ.get("BX_BASE_URL", "https://fapi.bitunix.com").rstrip("/")
TIMEOUT_S = 10.0
MARGIN_COIN = "USDT"

# The only private paths this module may call.
PATHS = {
    "account": ("GET", "/api/v1/futures/account"),
    "position_mode": ("GET", "/api/v1/futures/account/position_mode"),
    "leverage_margin_mode": ("GET", "/api/v1/futures/account/get_leverage_margin_mode"),
    "change_margin_mode": ("POST", "/api/v1/futures/account/change_margin_mode"),
    "change_leverage": ("POST", "/api/v1/futures/account/change_leverage"),
    "pending_positions": ("GET", "/api/v1/futures/position/get_pending_positions"),
    "history_positions": ("GET", "/api/v1/futures/position/get_history_positions"),
    "place_order": ("POST", "/api/v1/futures/trade/place_order"),
    "order_detail": ("GET", "/api/v1/futures/trade/get_order_detail"),
    "flash_close": ("POST", "/api/v1/futures/trade/flash_close_position"),
    "tpsl_pending": ("GET", "/api/v1/futures/tpsl/get_pending_orders"),
    "tpsl_position_place": ("POST", "/api/v1/futures/tpsl/position/place_order"),
}
PUBLIC_TIERS = "/api/v1/futures/position/get_position_tiers"


class BXTradeError(RuntimeError):
    def __init__(self, msg: str, code: Any = None):
        super().__init__(msg)
        self.code = code


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def query_string_for_sign(params: Optional[Dict[str, Any]]) -> str:
    items = sorted((k, v) for k, v in (params or {}).items() if v is not None)
    return "".join(f"{k}{v}" for k, v in items).replace(" ", "")


def compact_body(body: Optional[Dict[str, Any]]) -> str:
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else ""


def sign(api_key: str, secret: str, nonce: str, timestamp: str, params: Optional[Dict[str, Any]],
         body_str: str) -> str:
    digest = sha256_hex(nonce + timestamp + api_key + query_string_for_sign(params) + body_str)
    return sha256_hex(digest + secret)


def keys_present() -> bool:
    return bool((os.environ.get("BX_API_KEY") or "").strip() and (os.environ.get("BX_API_SECRET") or "").strip())


def redact(text: str) -> str:
    """Remove the key and secret from any string before it can reach a log or an exception."""
    out = str(text)
    for v in ((os.environ.get("BX_API_KEY") or "").strip(), (os.environ.get("BX_API_SECRET") or "").strip()):
        if v and len(v) >= 6:
            out = out.replace(v, "***")
    return out


class BXTrade:
    def __init__(self, dry_run: bool = False, opener=None, api_key: Optional[str] = None,
                 secret: Optional[str] = None, clock=time.time):
        self._key = (api_key if api_key is not None else os.environ.get("BX_API_KEY") or "").strip()
        self._secret = (secret if secret is not None else os.environ.get("BX_API_SECRET") or "").strip()
        self.dry_run = dry_run
        self.opener = opener or urllib.request.urlopen
        self.clock = clock
        self.recorded: List[Dict[str, Any]] = []
        if not dry_run and not (self._key and self._secret):
            raise BXTradeError("Bitunix API key/secret missing (BX_API_KEY / BX_API_SECRET): live trading refused")

    def __repr__(self) -> str:  # never show the key
        return f"<BXTrade dry_run={self.dry_run}>"

    # ------------------------------------------------------------------ transport
    def _request(self, name: str, params: Optional[Dict[str, Any]] = None,
                 body: Optional[Dict[str, Any]] = None, dry_result: Any = None) -> Any:
        method, path = PATHS[name]
        nonce = secrets.token_hex(16)
        ts = str(int(self.clock() * 1000))
        body_str = compact_body(body) if method == "POST" else ""
        signature = sign(self._key, self._secret, nonce, ts, params if method == "GET" else None, body_str)
        q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{BASE_URL}{path}" + (f"?{q}" if q and method == "GET" else "")
        headers = {"api-key": self._key, "nonce": nonce, "timestamp": ts, "sign": signature,
                   "language": "en-US", "Content-Type": "application/json"}
        if self.dry_run:
            self.recorded.append({"method": method, "url": url, "body": body_str or None,
                                  "headers": {**headers, "api-key": "***", "sign": "<sha256 of digest+secret>"}})
            return dry_result
        req = urllib.request.Request(url, data=body_str.encode("utf-8") if method == "POST" else None,
                                     headers=headers, method=method)
        try:
            with self.opener(req, timeout=TIMEOUT_S) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise BXTradeError(redact(f"{name}: http {e.code}"), code=e.code) from None
        except Exception as e:  # noqa: BLE001
            raise BXTradeError(redact(f"{name}: {type(e).__name__}: {str(e)[:160]}")) from None
        if not isinstance(data, dict) or str(data.get("code")) != "0":
            code = data.get("code") if isinstance(data, dict) else None
            msg = data.get("msg") if isinstance(data, dict) else str(data)[:160]
            raise BXTradeError(redact(f"{name}: code {code} {msg}"), code=code)
        return data.get("data")

    # ------------------------------------------------------------------ reads
    def account(self) -> dict:
        d = self._request("account", {"marginCoin": MARGIN_COIN},
                          dry_result=[{"marginCoin": "USDT", "available": "1000", "margin": "0", "frozen": "0",
                                       "crossUnrealizedPNL": "0", "isolationUnrealizedPNL": "0",
                                       "positionMode": "ONE_WAY"}])
        return (d[0] if isinstance(d, list) and d else d) or {}

    def position_mode(self) -> str:
        d = self._request("position_mode", dry_result={"positionMode": "ONE_WAY"})
        d = d[0] if isinstance(d, list) and d else (d or {})
        return str(d.get("positionMode") or "").upper()

    def pending_positions(self, symbol: Optional[str] = None) -> List[dict]:
        return list(self._request("pending_positions", {"symbol": symbol}, dry_result=[]) or [])

    def history_positions(self, symbol: Optional[str] = None, position_id: Optional[str] = None) -> List[dict]:
        d = self._request("history_positions", {"symbol": symbol, "positionId": position_id, "limit": 20},
                          dry_result={"positionList": []}) or {}
        return list(d.get("positionList") or []) if isinstance(d, dict) else list(d)

    def order_detail(self, order_id: Optional[str] = None, client_id: Optional[str] = None) -> dict:
        return self._request("order_detail", {"orderId": order_id, "clientId": client_id},
                             dry_result={"status": "FILLED"}) or {}

    def tpsl_pending(self, symbol: Optional[str] = None, position_id: Optional[str] = None) -> List[dict]:
        return list(self._request("tpsl_pending", {"symbol": symbol, "positionId": position_id, "limit": 100},
                                  dry_result=[]) or [])

    # ------------------------------------------------------------------ writes (trade only)
    def set_isolated(self, symbol: str) -> Any:
        return self._request("change_margin_mode", body={"marginMode": "ISOLATION", "symbol": symbol,
                                                        "marginCoin": MARGIN_COIN}, dry_result=[{}])

    def set_leverage(self, symbol: str, leverage: int) -> Any:
        return self._request("change_leverage", body={"symbol": symbol, "leverage": int(leverage),
                                                     "marginCoin": MARGIN_COIN}, dry_result=[{}])

    def open_long(self, symbol: str, qty: str, limit_price: str, sl_price: str, client_id: str) -> dict:
        """IOC limit buy (price cap = slippage guard) with the Hard SL attached to the same order."""
        body = {"symbol": symbol, "side": "BUY", "tradeSide": "OPEN", "orderType": "LIMIT", "effect": "IOC",
                "qty": qty, "price": limit_price, "reduceOnly": False, "clientId": client_id,
                "slPrice": sl_price, "slStopType": "MARK_PRICE", "slOrderType": "MARKET"}
        return self._request("place_order", body=body, dry_result={"orderId": "DRYRUN", "clientId": client_id}) or {}

    def place_position_sl(self, symbol: str, position_id: str, sl_price: str) -> dict:
        return self._request("tpsl_position_place", body={"symbol": symbol, "positionId": position_id,
                                                         "slPrice": sl_price, "slStopType": "MARK_PRICE"},
                             dry_result={"orderId": "DRYRUN-SL"}) or {}

    def flash_close(self, position_id: str) -> dict:
        return self._request("flash_close", body={"positionId": position_id},
                             dry_result={"positionId": position_id}) or {}


def position_tiers(symbol: str, opener=None) -> List[dict]:
    """Public: maintenance-margin tiers (used for the liquidation estimate)."""
    op = opener or urllib.request.urlopen
    req = urllib.request.Request(f"{BASE_URL}{PUBLIC_TIERS}?symbol={urllib.parse.quote(symbol)}",
                                 headers={"Accept": "application/json", "User-Agent": "own-trend-radar-bx/1"})
    with op(req, timeout=TIMEOUT_S) as r:
        d = json.loads(r.read().decode("utf-8"))
    if str(d.get("code")) != "0":
        raise BXTradeError(f"position tiers {symbol}: code {d.get('code')}")
    return list(d.get("data") or [])
