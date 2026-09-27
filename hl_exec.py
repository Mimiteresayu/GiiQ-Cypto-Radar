#!/usr/bin/env python3
"""Hyperliquid I/O for the executor and exit worker.

- Read-only info calls use plain HTTPS (work in DRY_RUN without any key).
- Signed actions use the official hyperliquid-python-sdk ``Exchange`` with the API (agent)
  wallet key HL_API_PRIVATE_KEY acting for the main account HL_ADDRESS.
- The exchange client is only built when exec_common.is_live_mode() is True
  (EXEC_DRY_RUN=0 AND HL_API_PRIVATE_KEY present) and the key's address matches
  HL_API_WALLET_ADDRESS (defaults to the registered API wallet). Otherwise it refuses.

Margin mode: ISOLATED. Each entry's liquidation price then depends only on that position's
own margin/leverage, so the SoT check "liq beyond Hard SL" is deterministic at entry time,
and a gap through one coin cannot drain the whole Unified balance. (In cross mode the liq
price depends on total equity, which is what produced the meaningless negative estimates.)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request as _url_req
from typing import Any, Dict, List, Optional

from exec_common import is_live_mode, round_price, floor_to_decimals

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
DEFAULT_MAIN_ADDRESS = "0xcFCda0F8576a268BaA17935368081F4e687dB122"
DEFAULT_API_WALLET_ADDRESS = "0xb74a9E2EA3e12511aDfc34a0a8327FbE4bc4e4D0"
USE_ISOLATED_MARGIN = True
SL_SLIPPAGE_PCT = 5.0      # limit_px band for the stop-market SL (market on trigger)
CLOSE_SLIPPAGE = 0.05      # 5% band for reduce-only IOC market closes


def _log(msg: str) -> None:
    sys.stderr.write(f"[HL_EXEC] {msg}\n")
    sys.stderr.flush()


class LiveModeRefused(RuntimeError):
    pass


def parse_order_response(resp: Any) -> Dict[str, Any]:
    """Normalize an SDK order response to {status: filled|resting|error, oid, filled_sz, avg_px, error}."""
    out: Dict[str, Any] = {"status": "error", "oid": None, "filled_sz": 0.0, "avg_px": None, "error": None, "raw": resp}
    if not isinstance(resp, dict):
        out["error"] = f"unexpected response: {resp!r}"[:300]
        return out
    if resp.get("status") != "ok":
        out["error"] = str(resp.get("response") or resp)[:300]
        return out
    try:
        statuses = resp["response"]["data"]["statuses"]
        st = statuses[0]
    except (KeyError, IndexError, TypeError):
        out["error"] = f"no statuses in response: {resp!r}"[:300]
        return out
    if isinstance(st, dict) and "filled" in st:
        f = st["filled"]
        out.update(status="filled", oid=f.get("oid"), filled_sz=float(f.get("totalSz") or 0), avg_px=float(f.get("avgPx") or 0))
    elif isinstance(st, dict) and "resting" in st:
        out.update(status="resting", oid=st["resting"].get("oid"))
    elif isinstance(st, dict) and "error" in st:
        out["error"] = str(st["error"])[:300]
    else:
        out["error"] = f"unknown status: {st!r}"[:300]
    return out


def _ok(resp: Any) -> bool:
    return isinstance(resp, dict) and resp.get("status") == "ok"


class HLClient:
    """Thin wrapper. Tests substitute a fake with the same method names."""

    def __init__(self, address: Optional[str] = None):
        self.address = (address or os.environ.get("HL_ADDRESS") or DEFAULT_MAIN_ADDRESS).strip()
        self._meta: Optional[Dict[str, dict]] = None
        self._exchange = None

    # ------------------------------------------------------------ read-only
    def info(self, payload: dict, retries: int = 4, backoff_s: float = 2.0) -> Any:
        data = json.dumps(payload).encode()
        for attempt in range(retries):
            req = _url_req.Request(HL_INFO_URL, data=data, headers={"Content-Type": "application/json"}, method="POST")
            try:
                with _url_req.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < retries - 1:
                    _log(f"429 on {payload.get('type')}, retry in {backoff_s:.0f}s")
                    time.sleep(backoff_s)
                    backoff_s *= 2
                    continue
                raise
        raise RuntimeError("HL info retries exhausted")

    def meta(self) -> Dict[str, dict]:
        """coin -> {szDecimals, maxLeverage, asset}."""
        if self._meta is None:
            m = self.info({"type": "meta"})
            self._meta = {
                a["name"]: {"szDecimals": int(a.get("szDecimals", 0)), "maxLeverage": float(a.get("maxLeverage") or 0), "asset": i}
                for i, a in enumerate(m.get("universe") or [])
                if not a.get("isDelisted")
            }
        return self._meta

    def all_mids(self) -> Dict[str, float]:
        mids = self.info({"type": "allMids"}) or {}
        out = {}
        for k, v in mids.items():
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                continue
        return out

    def perp_state(self) -> dict:
        return self.info({"type": "clearinghouseState", "user": self.address})

    def spot_state(self) -> dict:
        return self.info({"type": "spotClearinghouseState", "user": self.address})

    def open_orders(self) -> List[dict]:
        """frontendOpenOrders (includes isTrigger / triggerPx / reduceOnly)."""
        return self.info({"type": "frontendOpenOrders", "user": self.address}) or []

    # ------------------------------------------------------------ signed (LIVE only)
    def exchange(self):
        if self._exchange is not None:
            return self._exchange
        if not is_live_mode():
            raise LiveModeRefused("not in LIVE mode (EXEC_DRY_RUN!=0 or HL_API_PRIVATE_KEY missing)")
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants

        wallet = Account.from_key(os.environ["HL_API_PRIVATE_KEY"].strip())
        expected = (os.environ.get("HL_API_WALLET_ADDRESS") or DEFAULT_API_WALLET_ADDRESS).strip()
        if wallet.address.lower() != expected.lower():
            raise LiveModeRefused(f"HL_API_PRIVATE_KEY address {wallet.address} != expected API wallet {expected}")
        self._exchange = Exchange(wallet=wallet, base_url=constants.MAINNET_API_URL, account_address=self.address)
        _log(f"LIVE exchange client ready: api_wallet={wallet.address[:10]}.. account={self.address[:10]}..")
        return self._exchange

    def set_leverage(self, coin: str, leverage: int) -> Dict[str, Any]:
        resp = self.exchange().update_leverage(int(leverage), coin, is_cross=not USE_ISOLATED_MARGIN)
        return {"ok": _ok(resp), "raw": resp}

    def open_long_ioc(self, coin: str, qty: float, limit_px: float) -> Dict[str, Any]:
        resp = self.exchange().order(coin, True, qty, limit_px, {"limit": {"tif": "Ioc"}}, reduce_only=False)
        return parse_order_response(resp)

    def place_stop_loss(self, coin: str, qty: float, trigger_px: float, sz_decimals: int) -> Dict[str, Any]:
        """Reduce-only stop-market SELL trigger (long Hard SL)."""
        trig = round_price(trigger_px, sz_decimals)
        limit_px = round_price(trig * (1 - SL_SLIPPAGE_PCT / 100.0), sz_decimals)
        order_type = {"trigger": {"triggerPx": trig, "isMarket": True, "tpsl": "sl"}}
        resp = self.exchange().order(coin, False, qty, limit_px, order_type, reduce_only=True)
        r = parse_order_response(resp)
        r["trigger_px"] = trig
        return r

    def market_close(self, coin: str, qty: float) -> Dict[str, Any]:
        """Reduce-only IOC close (SDK market_close)."""
        resp = self.exchange().market_close(coin, sz=qty, slippage=CLOSE_SLIPPAGE)
        if resp is None:
            return {"status": "error", "error": "no position to close", "filled_sz": 0.0}
        return parse_order_response(resp)

    def cancel(self, coin: str, oid: int) -> Dict[str, Any]:
        resp = self.exchange().cancel(coin, int(oid))
        ok = _ok(resp)
        try:
            st = resp["response"]["data"]["statuses"][0]
            ok = ok and st == "success"
        except (KeyError, IndexError, TypeError):
            pass
        return {"ok": ok, "raw": resp}


# ---------------------------------------------------------------- composite flows
def enter_long_with_sl(hl: Any, coin: str, qty: float, limit_px: float, leverage: int,
                       hard_sl: float, sz_decimals: int) -> Dict[str, Any]:
    """LIVE entry: set isolated leverage -> IOC limit buy -> reduce-only stop-market SL.

    Fail-safe: if the SL cannot be placed, the filled size is closed immediately.
    Returns {status: executed|no_fill|leverage_failed|entry_failed|sl_failed_closed|sl_failed_CLOSE_FAILED, ...}
    """
    res: Dict[str, Any] = {"coin": coin, "qty": qty, "limit_px": limit_px, "leverage": leverage, "hard_sl": hard_sl}
    try:
        lev = hl.set_leverage(coin, leverage)
    except Exception as e:  # noqa: BLE001
        lev = {"ok": False, "raw": str(e)}
    res["leverage_result"] = lev
    if not lev.get("ok"):
        res["status"] = "leverage_failed"
        _log(f"{coin}: set leverage {leverage}x isolated FAILED: {lev.get('raw')}")
        return res

    try:
        entry = hl.open_long_ioc(coin, qty, limit_px)
    except Exception as e:  # noqa: BLE001
        entry = {"status": "error", "error": str(e), "filled_sz": 0.0}
    res["entry_result"] = {k: v for k, v in entry.items() if k != "raw"}
    filled = float(entry.get("filled_sz") or 0)
    if entry.get("status") == "error" and filled <= 0:
        res["status"] = "entry_failed"
        _log(f"{coin}: entry FAILED: {entry.get('error')}")
        return res
    if filled <= 0:
        res["status"] = "no_fill"
        _log(f"{coin}: IOC entry got no fill @ {limit_px}")
        return res
    filled = floor_to_decimals(filled, sz_decimals)
    res["filled_sz"] = filled
    res["avg_px"] = entry.get("avg_px")
    _log(f"{coin}: FILLED {filled} @ {entry.get('avg_px')} ({leverage}x isolated); placing Hard SL @ {hard_sl}")

    try:
        sl = hl.place_stop_loss(coin, filled, hard_sl, sz_decimals)
    except Exception as e:  # noqa: BLE001
        sl = {"status": "error", "error": str(e)}
    res["sl_result"] = {k: v for k, v in sl.items() if k != "raw"}
    if sl.get("status") in ("resting", "filled"):
        res["status"] = "executed"
        res["sl_oid"] = sl.get("oid")
        _log(f"{coin}: Hard SL placed oid={sl.get('oid')} trigger={sl.get('trigger_px')}")
        return res

    # FAIL-SAFE: no SL -> flatten immediately
    _log(f"{coin}: Hard SL placement FAILED ({sl.get('error')}) -> closing position NOW")
    try:
        close = hl.market_close(coin, filled)
    except Exception as e:  # noqa: BLE001
        close = {"status": "error", "error": str(e)}
    res["close_result"] = {k: v for k, v in close.items() if k != "raw"}
    if close.get("status") == "filled":
        res["status"] = "sl_failed_closed"
    else:
        res["status"] = "sl_failed_CLOSE_FAILED"
        _log(f"{coin}: CRITICAL fail-safe close FAILED: {close.get('error')} -- MANUAL ACTION REQUIRED")
    return res


def trigger_orders_for(orders: List[dict], coin: str) -> List[dict]:
    """Reduce-only stop triggers resting for coin (from frontendOpenOrders)."""
    out = []
    for o in orders or []:
        if o.get("coin") != coin:
            continue
        is_trig = bool(o.get("isTrigger")) or "stop" in str(o.get("orderType") or "").lower()
        if is_trig:
            out.append(o)
    return out
