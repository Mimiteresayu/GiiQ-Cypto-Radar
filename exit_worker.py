#!/usr/bin/env python3
"""Exit worker for tier-based exit signals.

SoT exit rules:
- Mega/Large: primary exit = 4H close < 4H Filter; Hard SL = 4H Lower
- Small/Tiny: primary exit = 1H close < 1H Lower; Hard SL = 4H Filter

Cron schedule:
- Hourly for Small/Tiny (1H exits)
- 4-hourly for Mega/Large (4H exits)

DRY_RUN by default; live only if EXEC_DRY_RUN=0 AND HL_API_PRIVATE_KEY present.
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

# Env vars
EXEC_DRY_RUN = os.environ.get("EXEC_DRY_RUN", "1").strip() in ("1", "true", "yes")
HL_API_PRIVATE_KEY = os.environ.get("HL_API_PRIVATE_KEY", "").strip()
HL_ADDRESS = os.environ.get("HL_ADDRESS", "0xcFCda0F8576a268BaA17935368081F4e687dB122").strip()

try:
    from trade_log import log_exit, get_open_trades
    from mcap_tiers import tier_for
except ImportError as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)


def _is_live_mode() -> bool:
    """Check if executor should run in LIVE mode."""
    return not EXEC_DRY_RUN and bool(HL_API_PRIVATE_KEY)


def _get_hl_info(endpoint: str, params: dict) -> dict:
    """Call Hyperliquid info API."""
    import urllib.request as _url_req
    
    payload = json.dumps({"type": endpoint, **params}).encode()
    req = _url_req.Request(
        "https://api.hyperliquid.xyz/info",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    
    with _url_req.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _load_radar(tf: str) -> dict:
    """Load radar JSON for timeframe."""
    path = ROOT / "out" / f"gc_radar_{tf}.json"
    if not path.is_file():
        return {}
    
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _get_positions() -> List[dict]:
    """Get open positions from Hyperliquid."""
    hl_perp = _get_hl_info("clearinghouseState", {"user": HL_ADDRESS})
    
    positions_raw = hl_perp.get("assetPositions", [])
    positions = []
    
    for pos_group in positions_raw:
        position = pos_group.get("position", {})
        if not position:
            continue
        
        try:
            szi = float(position.get("szi", 0))
            if abs(szi) < 1e-8:
                continue
            
            coin = position.get("coin", "")
            entry_px = float(position.get("entryPx", 0))
            unrealized_pnl = float(position.get("unrealizedPnl", 0))
            position_value = float(position.get("positionValue", 0))
            
            positions.append({
                "coin": coin,
                "side": "LONG" if szi > 0 else "SHORT",
                "size": abs(szi),
                "entry_px": entry_px,
                "position_value": position_value,
                "unrealized_pnl": unrealized_pnl,
            })
        except (TypeError, ValueError):
            continue
    
    return positions


def _check_exit_signal(
    coin: str,
    tier: str,
    radar_1h: dict,
    radar_4h: dict,
) -> Tuple[bool, Optional[str]]:
    """Check if position should exit based on tier rules.
    
    Returns:
        (should_exit: bool, exit_reason: Optional[str])
    """
    # Build radar maps
    r1h_map = {r["symbol"]: r for r in radar_1h.get("rows", [])}
    r4h_map = {r["symbol"]: r for r in radar_4h.get("rows", [])}
    
    r1h = r1h_map.get(coin) or r1h_map.get(f"{coin}-PERP")
    r4h = r4h_map.get(coin) or r4h_map.get(f"{coin}-PERP")
    
    # Mega/Large: 4H close < 4H Filter
    if tier in ("mega", "large"):
        if not r4h:
            return False, None
        
        close_4h = r4h.get("close")
        filter_4h = r4h.get("filter")
        
        if close_4h and filter_4h and close_4h < filter_4h:
            return True, "4H close < 4H Filter (primary exit)"
    
    # Small/Tiny: 1H close < 1H Lower
    elif tier in ("small", "tiny"):
        if not r1h:
            return False, None
        
        close_1h = r1h.get("close")
        lower_1h = r1h.get("lower")
        
        if close_1h and lower_1h and close_1h < lower_1h:
            return True, "1H close < 1H Lower (primary exit)"
    
    return False, None


def _compute_mae_mfe_r(
    entry_px: float,
    current_px: float,
    unrealized_pnl: float,
    position_value: float,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Compute MAE, MFE, and R multiple.
    
    For simplicity, we approximate:
    - MAE: worst unrealized loss % (not tracked historically, use current if negative)
    - MFE: best unrealized profit % (not tracked historically, use current if positive)
    - R: realized profit / initial risk (approximated)
    """
    # Current PnL %
    current_pnl_pct = (unrealized_pnl / position_value * 100.0) if position_value > 0 else 0.0
    
    # MAE: approximate as current loss if negative
    mae_pct = min(0, current_pnl_pct)
    
    # MFE: approximate as current profit if positive
    mfe_pct = max(0, current_pnl_pct)
    
    # R multiple: current_pnl_pct / assumed_risk (e.g., 5% SL)
    assumed_risk_pct = 5.0
    r_multiple = current_pnl_pct / assumed_risk_pct if assumed_risk_pct > 0 else 0.0
    
    return mae_pct, mfe_pct, r_multiple


def check_exits(exit_type: str = "all") -> Dict[str, Any]:
    """Check and execute exits for open positions.
    
    Args:
        exit_type: "all" | "hourly" (1H Small/Tiny) | "4h" (4H Mega/Large)
        
    Returns:
        dict with:
            mode: LIVE | DRY_RUN
            status: success | error
            exits: list of executed exits
            holds: list of positions held
            actions: list of intended exits (DRY_RUN)
    """
    mode = "LIVE" if _is_live_mode() else "DRY_RUN"
    
    result = {
        "mode": mode,
        "status": "success",
        "exits": [],
        "holds": [],
        "actions": [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    
    # Load radars
    radar_1h = _load_radar("1h")
    radar_4h = _load_radar("4h")
    
    # Get positions
    try:
        positions = _get_positions()
    except Exception as e:
        result["status"] = "error"
        result["message"] = f"Failed to get positions: {e}"
        return result
    
    # Get open trades from log
    open_trades = get_open_trades()
    trade_map = {t["symbol"]: t for t in open_trades}
    
    # Check each position
    for pos in positions:
        coin = pos["coin"]
        tier = tier_for(coin) if tier_for else "unknown"
        
        # Filter by exit_type
        if exit_type == "hourly" and tier not in ("small", "tiny"):
            result["holds"].append({"coin": coin, "reason": "not hourly tier"})
            continue
        
        if exit_type == "4h" and tier not in ("mega", "large"):
            result["holds"].append({"coin": coin, "reason": "not 4H tier"})
            continue
        
        # Check exit signal
        should_exit, exit_reason = _check_exit_signal(coin, tier, radar_1h, radar_4h)
        
        if not should_exit:
            result["holds"].append({"coin": coin, "tier": tier, "reason": "no exit signal"})
            continue
        
        # Get current price (close from radar)
        r4h_map = {r["symbol"]: r for r in radar_4h.get("rows", [])}
        r4h = r4h_map.get(coin) or r4h_map.get(f"{coin}-PERP")
        current_px = r4h.get("close", 0) if r4h else 0
        
        # Compute MAE/MFE/R
        entry_px = pos["entry_px"]
        unrealized_pnl = pos["unrealized_pnl"]
        position_value = pos["position_value"]
        
        mae_pct, mfe_pct, r_multiple = _compute_mae_mfe_r(
            entry_px, current_px, unrealized_pnl, position_value
        )
        
        pnl_usd = unrealized_pnl
        
        exit_intent = {
            "coin": coin,
            "tier": tier,
            "exit_price": current_px,
            "exit_reason": exit_reason,
            "mae_pct": mae_pct,
            "mfe_pct": mfe_pct,
            "r_multiple": r_multiple,
            "pnl_usd": pnl_usd,
        }
        
        # DRY_RUN: log intent
        if mode == "DRY_RUN":
            result["actions"].append(exit_intent)
            
            # Log exit to trade log if trade exists
            trade = trade_map.get(coin)
            if trade:
                log_exit(
                    trade_id=trade["trade_id"],
                    exit_price=current_px,
                    exit_reason=exit_reason,
                    mae_pct=mae_pct,
                    mfe_pct=mfe_pct,
                    r_multiple=r_multiple,
                    pnl_usd=pnl_usd,
                )
        else:
            # LIVE: place market sell order
            try:
                # TODO: Integrate hyperliquid-python-sdk for market sell
                result["exits"].append(exit_intent)
                
                # Log exit to trade log
                trade = trade_map.get(coin)
                if trade:
                    log_exit(
                        trade_id=trade["trade_id"],
                        exit_price=current_px,
                        exit_reason=exit_reason,
                        mae_pct=mae_pct,
                        mfe_pct=mfe_pct,
                        r_multiple=r_multiple,
                        pnl_usd=pnl_usd,
                    )
            except Exception as e:
                result["exits"].append({
                    "coin": coin,
                    "error": f"Exit failed: {e}",
                })
    
    return result


if __name__ == "__main__":
    # CLI execution
    import sys
    
    exit_type = sys.argv[1] if len(sys.argv) > 1 else "all"
    
    result = check_exits(exit_type=exit_type)
    
    print(json.dumps(result, indent=2))
    
    sys.exit(0 if result["status"] == "success" else 1)
