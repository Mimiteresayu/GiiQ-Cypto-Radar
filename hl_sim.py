#!/usr/bin/env python3
"""In-memory Hyperliquid exchange simulator for tests and the end-to-end dry-run pilot.

NEVER talks to the network and never signs anything. It mimics what the real exchange shows after
each write so the GIIQ-SoT-5 post-fill reconciliation (hl_exec._reconcile_position) can be exercised:

  set_leverage      -> remembered per coin (isolated)
  open_long_ioc     -> IOC fill: the LONG position appears in perp_state() (size, entryPx, isolated
                       leverage, liquidationPx from the HL isolated formula, marginUsed); cloid recorded;
                       a cloid already used today is refused like a duplicate on the exchange
  place_stop_loss   -> a reduce-only stop trigger appears in open_orders()
  market_close      -> the position is reduced / removed

Classes reuse it as a mixin: they keep their own read methods / flags (fill, sl_ok, close_ok, lev_ok;
mismatch knobs sim_lev_override / sim_cross make the exchange state differ from the request)
and get exchange-consistent write methods. `positions` holds raw HL position dicts (coin, szi, ...).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from exec_common import isolated_liq_price_long


class SimExchangeMixin:
    """Mixin: needs self.positions (list of raw HL position dicts), self.orders (list), self.calls (list)."""

    def _sim_init(self) -> None:
        if not hasattr(self, "_sim_lev"):
            self._sim_lev: Dict[str, int] = {}
            self._sim_cloids: set = set()
            self._sim_oid = 100
        if getattr(self, "orders", None) is None:
            self.orders = []
        if getattr(self, "positions", None) is None:
            self.positions = []

    def _sim_maxlev(self, coin: str) -> Optional[float]:
        m = (getattr(self, "_meta", None) or {}).get(coin) or {}
        return m.get("maxLeverage")

    def _sim_pos(self, coin: str) -> Optional[dict]:
        return next((p for p in self.positions if p.get("coin") == coin and float(p.get("szi") or 0) != 0), None)

    # ---------------------------------------------------------------- writes
    def set_leverage(self, coin: str, lev: int) -> Dict[str, Any]:
        self._sim_init()
        self.calls.append(("set_leverage", coin, lev))
        if not getattr(self, "lev_ok", True):
            return {"ok": False, "raw": "leverage refused"}
        self._sim_lev[coin] = int(lev)
        return {"ok": True}

    def open_long_ioc(self, coin: str, qty: float, px: float, cloid: Optional[str] = None) -> Dict[str, Any]:
        self._sim_init()
        self.calls.append(("open_long_ioc", coin, qty, px, cloid))
        if cloid is not None:
            if not (str(cloid).startswith("0x") and len(str(cloid)) == 34):
                return {"status": "error", "error": f"invalid cloid {cloid!r}", "filled_sz": 0.0}
            if cloid in self._sim_cloids:
                return {"status": "error", "error": "duplicate cloid", "filled_sz": 0.0}
            self._sim_cloids.add(cloid)
        if not getattr(self, "fill", True):
            return {"status": "error", "error": "Order could not immediately match", "filled_sz": 0.0}
        lev = self._sim_lev.get(coin) or int(getattr(self, "lev", None) or 3)
        pos = self._sim_pos(coin)
        if pos is None:
            pos = {"coin": coin, "szi": "0", "entryPx": str(px)}
            self.positions.append(pos)
        old = float(pos.get("szi") or 0)
        new = old + qty
        entry = (old * float(pos.get("entryPx") or px) + qty * px) / new if new else px
        lev_now = int(float((pos.get("leverage") or {}).get("value") or 0)) or lev
        # mismatch knobs (pilot / tests): the exchange ends up with another leverage or in cross margin
        lev_now = int(getattr(self, "sim_lev_override", None) or lev_now)
        mtype = "cross" if getattr(self, "sim_cross", False) else "isolated"
        liq = isolated_liq_price_long(entry, lev_now, self._sim_maxlev(coin)) or 0.0
        pos.update(szi=str(new), entryPx=str(entry), leverage={"type": mtype, "value": lev_now},
                   liquidationPx=str(liq), marginUsed=str(new * entry / lev_now))
        self._sim_oid += 1
        return {"status": "filled", "filled_sz": qty, "avg_px": px, "oid": self._sim_oid}

    def place_stop_loss(self, coin: str, qty: float, trig: float, szd: int) -> Dict[str, Any]:
        self._sim_init()
        self.calls.append(("place_stop_loss", coin, qty, trig))
        if not getattr(self, "sl_ok", True):
            return {"status": "error", "error": "Invalid TP/SL price"}
        self._sim_oid += 1
        self.orders.append({"coin": coin, "oid": self._sim_oid, "isTrigger": True, "orderType": "Stop Market",
                            "reduceOnly": True, "sz": str(qty), "triggerPx": str(trig), "side": "A"})
        return {"status": "resting", "oid": self._sim_oid, "trigger_px": trig}

    def market_close(self, coin: str, qty: float) -> Dict[str, Any]:
        self._sim_init()
        self.calls.append(("market_close", coin, qty))
        if not getattr(self, "close_ok", True):
            return {"status": "error", "error": "boom", "filled_sz": 0.0}
        pos = self._sim_pos(coin)
        if pos is not None:
            left = max(0.0, float(pos.get("szi") or 0) - qty)
            if left <= 1e-12:
                self.positions.remove(pos)
            else:
                pos["szi"] = str(left)
        return {"status": "filled", "filled_sz": qty, "avg_px": 1.0}

    def cancel(self, coin: str, oid: int) -> Dict[str, Any]:
        self._sim_init()
        self.calls.append(("cancel", coin, oid))
        self.orders = [o for o in self.orders if o.get("oid") != oid]
        return {"ok": True}

    def entries(self) -> List[tuple]:
        return [c for c in self.calls if c[0] == "open_long_ioc"]
