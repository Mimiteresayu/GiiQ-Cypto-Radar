#!/usr/bin/env python3
"""Exit worker for tier-based exits + Hard SL alignment.

SoT exit rules:
- Mega/Large: primary exit = 4H close < 4H Filter; Hard SL = 4H Lower   (run "4h")
- Small/Tiny: primary exit = 1H close < 1H Lower; Hard SL = 4H Filter   (run "hourly")

Per held LONG in the run's tiers:
- primary exit -> reduce-only IOC close; then cancel that coin's stop triggers
  (if only partially closed, the SL is re-sized to the remaining size)
- otherwise    -> align the reduce-only stop-market Hard SL to the tier level
  (place if missing; replace if drift >= 0.3% or size mismatch; new SL placed BEFORE the old is
  cancelled so the position is never unprotected; never placed at/below the liquidation price)

DRY_RUN by default; LIVE only if EXEC_DRY_RUN=0 AND HL_API_PRIVATE_KEY present.
Trade-log matching: LIVE uses only real (dry_run=False) trades, DRY_RUN only dry-run trades.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from exec_common import hard_sl_for_tier, is_live_mode, round_price  # noqa: E402

try:
    from trade_log import log_exit, get_open_trades
    from mcap_tiers import tier_for
except ImportError as e:  # pragma: no cover
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)

HL_ADDRESS = os.environ.get("HL_ADDRESS", "0xcFCda0F8576a268BaA17935368081F4e687dB122").strip()
SL_DRIFT_PCT = 0.3


def _log(msg: str) -> None:
    sys.stderr.write(f"[EXIT_WORKER] {msg}\n")
    sys.stderr.flush()


def _is_live_mode() -> bool:
    return is_live_mode()


def _load_radar(tf: str) -> dict:
    path = ROOT / "out" / f"gc_radar_{tf}.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _parse_positions(perp: dict) -> List[dict]:
    positions = []
    for grp in perp.get("assetPositions", []) or []:
        p = grp.get("position") or {}
        try:
            szi = float(p.get("szi", 0))
            if abs(szi) < 1e-12:
                continue
            positions.append({
                "coin": p.get("coin", ""),
                "side": "LONG" if szi > 0 else "SHORT",
                "size": abs(szi),
                "entry_px": float(p.get("entryPx") or 0),
                "position_value": float(p.get("positionValue") or 0),
                "unrealized_pnl": float(p.get("unrealizedPnl") or 0),
                "liquidation_px": float(p.get("liquidationPx") or 0),
            })
        except (TypeError, ValueError):
            continue
    return positions


def _row(radar: dict, coin: str) -> Optional[dict]:
    for r in radar.get("rows", []) or []:
        if r.get("symbol") in (coin, f"{coin}-PERP"):
            return r
    return None


def _check_exit_signal(coin: str, tier: str, strategy: Optional[str], radar_1h: dict, radar_4h: dict) -> Tuple[bool, Optional[str]]:
    """Primary exit per tier (closed bars from radar).
    
    CONT_STAIRCASE positions use special exit: 4H close < 4H Filter (for ALL tiers).
    Other positions use tier-based exits.
    """
    # CONT_STAIRCASE override: always use 4H close < Filter exit
    if strategy == "CONT_STAIRCASE":
        r4h = _row(radar_4h, coin)
        if r4h and r4h.get("close") and r4h.get("filter") and r4h["close"] < r4h["filter"]:
            return True, "CONT_STAIRCASE: 4H close < 4H Filter (trailing exit)"
        return False, None
    
    # Tier-based exits for Base/ADD_ON positions
    if tier in ("mega", "large"):
        r4h = _row(radar_4h, coin)
        if r4h and r4h.get("close") and r4h.get("filter") and r4h["close"] < r4h["filter"]:
            return True, "4H close < 4H Filter (primary exit)"
    elif tier in ("small", "tiny"):
        r1h = _row(radar_1h, coin)
        if r1h and r1h.get("close") and r1h.get("lower") and r1h["close"] < r1h["lower"]:
            return True, "1H close < 1H Lower (primary exit)"
    return False, None


def _compute_mae_mfe_r(entry_px: float, current_px: float, unrealized_pnl: float,
                       position_value: float) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Approximate MAE/MFE/R from current uPnL (no intrabar history)."""
    pnl_pct = (unrealized_pnl / position_value * 100.0) if position_value > 0 else 0.0
    return min(0, pnl_pct), max(0, pnl_pct), pnl_pct / 5.0


def _align_sl(hl: Any, live: bool, coin: str, size: float, hard_sl: float, liq_px: float,
              triggers: List[dict], sz_dec: int) -> Optional[Dict[str, Any]]:
    """Ensure exactly one reduce-only stop at hard_sl for `size`."""
    target = round_price(hard_sl, sz_dec)
    if liq_px and target <= liq_px:
        _log(f"{coin}: Hard SL {target} at/below liq {liq_px} -> NOT moving SL (alert)")
        return {"action": "sl_blocked_liq", "coin": coin, "target": target, "liq_px": liq_px}
    current = None
    for o in triggers:
        try:
            t = float(o.get("triggerPx") or 0)
            s = float(o.get("sz") or 0)
        except (TypeError, ValueError):
            continue
        if t > 0 and abs(t - target) / target * 100.0 < SL_DRIFT_PCT and abs(s - size) <= max(1e-9, size * 1e-6):
            current = o
            break
    if current and len(triggers) == 1:
        return None  # aligned
    action = {"action": "place_sl" if not triggers else "update_sl", "coin": coin, "trigger": target, "size": size,
              "old": [{"oid": o.get("oid"), "triggerPx": o.get("triggerPx"), "sz": o.get("sz")} for o in triggers]}
    if not live:
        action["mode"] = "dry_run"
        _log(f"DRY_RUN would {action['action']} {coin} Hard SL @ {target} size={size} (cancel {len(triggers)} old)")
        return action
    keep_oid = current.get("oid") if current else None
    if not current:
        placed = hl.place_stop_loss(coin, size, target, sz_dec)
        action["place"] = {k: v for k, v in placed.items() if k != "raw"}
        if placed.get("status") not in ("resting", "filled"):
            _log(f"{coin}: SL place FAILED ({placed.get('error')}); keeping old triggers")
            action["status"] = "error"
            return action
        keep_oid = placed.get("oid")
    action["cancels"] = []
    for o in triggers:
        if o.get("oid") == keep_oid:
            continue
        action["cancels"].append({"oid": o.get("oid"), **hl.cancel(coin, int(o["oid"]))})
    action["status"] = "ok"
    _log(f"LIVE {action['action']} {coin} Hard SL @ {target} size={size}")
    return action


def check_exits(exit_type: str = "all", hl: Any = None, radar_1h: Optional[dict] = None,
                radar_4h: Optional[dict] = None) -> Dict[str, Any]:
    """exit_type: all | hourly (Small/Tiny) | 4h (Mega/Large)."""
    live = _is_live_mode()
    mode = "LIVE" if live else "DRY_RUN"
    result: Dict[str, Any] = {"mode": mode, "status": "success", "exit_type": exit_type, "exits": [], "holds": [],
                              "actions": [], "sl_actions": [], "timestamp": datetime.now(timezone.utc).isoformat()}
    radar_1h = _load_radar("1h") if radar_1h is None else radar_1h
    radar_4h = _load_radar("4h") if radar_4h is None else radar_4h

    if hl is None:
        from hl_exec import HLClient
        hl = HLClient(HL_ADDRESS)
    try:
        positions = _parse_positions(hl.perp_state())
        orders = hl.open_orders() if positions else []
        meta = hl.meta() if positions else {}
    except Exception as e:  # noqa: BLE001
        result.update(status="error", message=f"Failed to get positions: {e}")
        return result

    from hl_exec import trigger_orders_for
    trade_map = {t["symbol"]: t for t in get_open_trades(dry_run=not live)}

    for pos in positions:
        coin = pos["coin"]
        tier = tier_for(coin)
        if pos["side"] != "LONG":
            result["holds"].append({"coin": coin, "reason": "short: report only"})
            continue
        trade = trade_map.get(coin)
        strategy = trade.get("entry_type") if trade else None
        # Hourly job is the Small/Tiny tier exit (1H close < Lower) plus tier SL align.
        # CONT_STAIRCASE is only judged on the 4H job, and its swing-low SL is never moved.
        if exit_type == "hourly" and strategy == "CONT_STAIRCASE":
            result["holds"].append({"coin": coin, "tier": tier, "strategy": "CONT_STAIRCASE",
                                    "reason": "hourly tier exit skips CONT_STAIRCASE",
                                    "sl_note": "CONT_STAIRCASE SL fixed at entry, never re-aligned"})
            continue
        if exit_type == "hourly" and tier not in ("small", "tiny"):
            result["holds"].append({"coin": coin, "reason": "not hourly tier"})
            continue
        if exit_type == "4h" and tier not in ("mega", "large"):
            # CONT_STAIRCASE positions are checked in the 4H job regardless of tier
            if strategy != "CONT_STAIRCASE":
                result["holds"].append({"coin": coin, "reason": "not 4H tier"})
                continue
        sz_dec = int((meta.get(coin) or {}).get("szDecimals", 0))
        triggers = trigger_orders_for(orders, coin)
        r4h = _row(radar_4h, coin)
        
        # CONT_STAIRCASE positions must NOT use tier-based Hard SL realignment
        if strategy == "CONT_STAIRCASE":
            # CONT_STAIRCASE: SL is fixed at entry (swing low), never re-aligned
            should_exit, reason = _check_exit_signal(coin, tier, strategy, radar_1h, radar_4h)
        else:
            # Normal tier-based logic with Hard SL realignment
            hard_sl, sl_label = hard_sl_for_tier(tier, r4h)
            should_exit, reason = _check_exit_signal(coin, tier, strategy, radar_1h, radar_4h)

        if not should_exit:
            if strategy == "CONT_STAIRCASE":
                result["holds"].append({"coin": coin, "tier": tier, "strategy": "CONT_STAIRCASE", 
                                      "reason": "no exit signal (CONT_STAIRCASE: waiting for 4H close < Filter)", 
                                      "sl_note": "CONT_STAIRCASE SL fixed at entry, never re-aligned"})
            else:
                result["holds"].append({"coin": coin, "tier": tier, "reason": "no exit signal", "hard_sl": hard_sl, "hard_sl_label": sl_label})
                if hard_sl:
                    try:
                        a = _align_sl(hl, live, coin, pos["size"], hard_sl, pos["liquidation_px"], triggers, sz_dec)
                    except Exception as e:  # noqa: BLE001
                        a = {"action": "sl_error", "coin": coin, "error": str(e)}
                    if a:
                        result["sl_actions"].append(a)
            continue

        if strategy == "CONT_STAIRCASE":
            row_px = r4h or {}
        else:
            row_px = (_row(radar_1h, coin) if tier in ("small", "tiny") else r4h) or {}
        current_px = row_px.get("close", 0) or 0
        mae, mfe, r_mult = _compute_mae_mfe_r(pos["entry_px"], current_px, pos["unrealized_pnl"], pos["position_value"])
        intent = {"coin": coin, "tier": tier, "size": pos["size"], "exit_price": current_px, "exit_reason": reason,
                  "mae_pct": mae, "mfe_pct": mfe, "r_multiple": r_mult, "pnl_usd": pos["unrealized_pnl"]}

        if not live:
            result["actions"].append(intent)
            _log(f"DRY_RUN would close {coin} size={pos['size']} ({reason}) and cancel {len(triggers)} SL trigger(s)")
        else:
            try:
                close = hl.market_close(coin, pos["size"])
            except Exception as e:  # noqa: BLE001
                close = {"status": "error", "error": str(e), "filled_sz": 0.0}
            intent["close_result"] = {k: v for k, v in close.items() if k != "raw"}
            filled = float(close.get("filled_sz") or 0)
            if close.get("status") != "filled" or filled <= 0:
                result["status"] = "error"
                intent["error"] = f"close failed: {close.get('error')} (SL left in place)"
                _log(f"{coin}: LIVE close FAILED: {close.get('error')} - Hard SL kept")
                result["exits"].append(intent)
                continue
            remaining = max(0.0, pos["size"] - filled)
            if remaining <= 10 ** (-sz_dec) / 2:
                intent["cancels"] = [{"oid": o.get("oid"), **hl.cancel(coin, int(o["oid"]))} for o in triggers if o.get("oid")]
                _log(f"LIVE closed {coin} {filled} ({reason}); cancelled {len(intent['cancels'])} SL trigger(s)")
            elif hard_sl:
                intent["sl_resize"] = _align_sl(hl, True, coin, remaining, hard_sl, pos["liquidation_px"], triggers, sz_dec)
                _log(f"LIVE partial close {coin} {filled}/{pos['size']}; SL resized to {remaining}")
            intent["exit_price"] = close.get("avg_px") or current_px
            result["exits"].append(intent)

        trade = trade_map.get(coin)
        if trade:
            log_exit(trade_id=trade["trade_id"], exit_price=intent["exit_price"], exit_reason=reason,
                     mae_pct=mae, mfe_pct=mfe, r_multiple=r_mult, pnl_usd=pos["unrealized_pnl"])
    return result


if __name__ == "__main__":
    et = sys.argv[1] if len(sys.argv) > 1 else "all"
    res = check_exits(exit_type=et)
    print(json.dumps(res, indent=2, default=str))
    sys.exit(0 if res["status"] == "success" else 1)
