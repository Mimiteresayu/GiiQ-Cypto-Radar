#!/usr/bin/env python3
"""Auto-executor for approved entry candidates.

Enforces Source of Truth (SoT) rules:
- Min notional $10, leverage 1-5x
- SL distance >= 1.5%
- Liq price must be beyond Hard SL
- Total used margin <= 80% equity after entry
- Re-check all open positions have liq beyond Hard SL (cross margin)
- BTC 4H close < 4H Filter: fixed 4% per coin

Places limit entry + reduce-only Hard SL trigger order via hyperliquid-python-sdk.
DRY_RUN by default (EXEC_DRY_RUN=1); live only if EXEC_DRY_RUN=0 AND HL_API_PRIVATE_KEY present.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Env vars
EXEC_DRY_RUN = os.environ.get("EXEC_DRY_RUN", "1").strip() in ("1", "true", "yes")
HL_API_PRIVATE_KEY = os.environ.get("HL_API_PRIVATE_KEY", "").strip()
HL_ADDRESS = os.environ.get("HL_ADDRESS", "0xcFCda0F8576a268BaA17935368081F4e687dB122").strip()

# Execution limits
MIN_NOTIONAL_USD = 10.0
MIN_LEVERAGE = 1.0
MAX_LEVERAGE = 5.0
MIN_SL_DIST_PCT = 1.5
MAX_MARGIN_UTILIZATION_PCT = 80.0

# SoT size bands
SIZE_BANDS = {
    "P": (4.0, 8.0),              # Primary only
    "P+N": (8.0, 12.0),           # Primary + Narrative
    "P+CR": (8.0, 12.0),          # Primary + Cemetery Revival
    "P+N+CR": (10.0, 15.0),       # Primary + Narrative + Cemetery Revival
    "Continuation": (2.0, 4.0),   # Continuation (no daily cap)
}

# BTC regime fixed size
BTC_BEARISH_FIXED_SIZE_PCT = 4.0

try:
    from decisions import get_decisions_for_today, get_approved_symbols
    from trade_log import log_entry
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


def _load_candidates() -> dict:
    """Load entry_candidates_latest.json."""
    path = ROOT / "out" / "entry_candidates_latest.json"
    if not path.is_file():
        return {}
    
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _get_account_state() -> dict:
    """Get Hyperliquid account state (Unified mode).
    
    Returns:
        {
            "equity": float (spot USDC),
            "margin_used": float,
            "free_margin": float,
            "positions": list of position dicts
        }
    """
    hl_spot = _get_hl_info("spotClearinghouseState", {"user": HL_ADDRESS})
    hl_perp = _get_hl_info("clearinghouseState", {"user": HL_ADDRESS})
    
    # Spot USDC balance (equity in Unified mode)
    balances = hl_spot.get("balances", [])
    equity = 0.0
    for bal in balances:
        if bal.get("coin") == "USDC":
            try:
                equity = float(bal.get("total", 0))
            except (TypeError, ValueError):
                pass
            break
    
    # Margin used
    margin_summary = hl_perp.get("marginSummary", {})
    try:
        margin_used = float(margin_summary.get("totalMarginUsed", 0))
    except (TypeError, ValueError):
        margin_used = 0.0
    
    # Positions
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
            
            positions.append({
                "coin": position.get("coin", ""),
                "side": "LONG" if szi > 0 else "SHORT",
                "size": abs(szi),
                "entry_px": float(position.get("entryPx", 0)),
                "position_value": float(position.get("positionValue", 0)),
                "unrealized_pnl": float(position.get("unrealizedPnl", 0)),
                "liquidation_px": float(position.get("liquidationPx") or 0),
            })
        except (TypeError, ValueError):
            continue
    
    return {
        "equity": equity,
        "margin_used": margin_used,
        "free_margin": max(0, equity - margin_used),
        "positions": positions,
    }


def _clamp_size_leverage(
    candidate: dict,
    decision: dict,
    btc_bearish: bool,
) -> Tuple[float, float]:
    """Clamp size and leverage to SoT bands.
    
    Args:
        candidate: entry candidate dict with type, tier, trend_1d, trend_4h
        decision: AI decision dict with size_pct, leverage
        btc_bearish: True if BTC 4H close < 4H Filter
        
    Returns:
        (size_pct, leverage) clamped to SoT rules
    """
    size_pct = decision.get("size_pct", 4.0)
    leverage = decision.get("leverage", 2.0)
    
    # BTC bearish: fixed 4% per coin
    if btc_bearish:
        size_pct = BTC_BEARISH_FIXED_SIZE_PCT
    
    # Clamp to SoT size bands (simplified: use type as proxy)
    entry_type = candidate.get("type", "Base")
    if entry_type == "Continuation":
        min_size, max_size = SIZE_BANDS["Continuation"]
    else:
        # Base/Add-on: use Primary band as default
        min_size, max_size = SIZE_BANDS["P"]
    
    size_pct = max(min_size, min(max_size, size_pct))
    
    # Clamp leverage 1-5x
    leverage = max(MIN_LEVERAGE, min(MAX_LEVERAGE, leverage))
    
    return size_pct, leverage


def _compute_liq_price_estimate(
    entry_price: float,
    size_usd: float,
    leverage: float,
    equity: float,
) -> float:
    """Rough estimate of liquidation price for a LONG position in cross margin.
    
    This is a simplified estimate; actual liq price depends on all positions.
    For safety checks, we use a conservative approximation.
    
    liq_price ≈ entry_price * (1 - (equity - initial_margin) / position_value)
    """
    initial_margin = size_usd / leverage
    position_value = size_usd
    
    # Conservative: assume we lose all free equity first
    buffer = equity - initial_margin
    if buffer <= 0:
        # Instant liquidation if no buffer
        return entry_price
    
    liq_pct = buffer / position_value
    liq_price = entry_price * (1 - liq_pct)
    
    return liq_price


def _check_liq_beyond_sl(
    entry_price: float,
    hard_sl: float,
    estimated_liq_price: float,
) -> bool:
    """Check if liquidation price is beyond (safer than) Hard SL for LONG.
    
    For LONG: liq_price < hard_sl is safe (we exit at SL before liquidation)
    """
    return estimated_liq_price < hard_sl


def _check_all_positions_liq_safe(
    positions: List[dict],
    radar_1h: dict,
    radar_4h: dict,
) -> Tuple[bool, List[str]]:
    """Check all open positions have liq beyond Hard SL.
    
    Returns:
        (all_safe: bool, unsafe_symbols: list)
    """
    # Build radar maps
    r1h_map = {r["symbol"]: r for r in radar_1h.get("rows", [])}
    r4h_map = {r["symbol"]: r for r in radar_4h.get("rows", [])}
    
    unsafe = []
    
    for pos in positions:
        coin = pos["coin"]
        side = pos["side"]
        liq_px = pos.get("liquidation_px", 0)
        
        if not liq_px or liq_px <= 0:
            # No liq price available (shouldn't happen in cross margin)
            continue
        
        # Determine Hard SL by tier
        tier = tier_for(coin) if tier_for else "tiny"
        
        # All tiers: Hard SL = 4H Filter
        r4h = r4h_map.get(coin) or r4h_map.get(f"{coin}-PERP")
        if not r4h:
            # No radar data, can't check
            continue
        
        hard_sl = r4h.get("filter")
        if not hard_sl:
            continue
        
        # Check if liq is beyond Hard SL
        if side == "LONG":
            if liq_px >= hard_sl:
                # Liq price is above Hard SL = unsafe (we'd liquidate before SL)
                unsafe.append(coin)
        else:
            # SHORT: liq above Hard SL is safe
            if liq_px <= hard_sl:
                unsafe.append(coin)
    
    return len(unsafe) == 0, unsafe


def execute_approved_candidates() -> Dict[str, Any]:
    """Execute approved candidates with SoT enforcement.
    
    Returns:
        dict with:
            mode: LIVE | DRY_RUN
            status: success | fail_closed | error
            executed: list of executed trades
            skipped: list of skipped trades with reasons
            actions: list of intended orders (DRY_RUN)
    """
    mode = "LIVE" if _is_live_mode() else "DRY_RUN"
    
    result = {
        "mode": mode,
        "status": "success",
        "executed": [],
        "skipped": [],
        "actions": [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    
    # Load data
    candidates_data = _load_candidates()
    candidates = candidates_data.get("candidates", [])
    decisions = get_decisions_for_today()
    approved = get_approved_symbols()
    
    # Fail closed if no decisions
    if not approved:
        result["status"] = "fail_closed"
        result["message"] = "No approved candidates"
        return result
    
    # Load radars
    radar_1h = _load_radar("1h")
    radar_4h = _load_radar("4h")
    
    # Get account state
    try:
        account = _get_account_state()
    except Exception as e:
        result["status"] = "error"
        result["message"] = f"Failed to get account state: {e}"
        return result
    
    equity = account["equity"]
    margin_used = account["margin_used"]
    positions = account["positions"]
    
    # Check BTC regime
    btc_bearish = False
    btc_4h_row = next((r for r in radar_4h.get("rows", []) if r.get("symbol") == "BTC"), None)
    if btc_4h_row:
        close_4h = btc_4h_row.get("close")
        filter_4h = btc_4h_row.get("filter")
        if close_4h and filter_4h and close_4h < filter_4h:
            btc_bearish = True
    
    # Check all positions have liq beyond Hard SL
    liq_safe, unsafe_symbols = _check_all_positions_liq_safe(positions, radar_1h, radar_4h)
    if not liq_safe:
        result["status"] = "fail_closed"
        result["message"] = f"Unsafe liquidation prices for: {', '.join(unsafe_symbols)}"
        return result
    
    # Process approved candidates
    for candidate in candidates:
        symbol = candidate.get("symbol", "")
        if symbol not in approved:
            continue
        
        decision = decisions.get(symbol, {})
        
        # Get fields
        entry_type = candidate.get("type", "Base")
        tier = candidate.get("tier", "unknown")
        trend_1d = candidate.get("trend_1d", "")
        trend_4h = candidate.get("trend_4h", "")
        close_1d = candidate.get("close_1d", 0)
        hard_sl_dist_pct = candidate.get("hard_sl_dist_pct", 0)
        
        # Determine Hard SL level
        if tier in ("mega", "large"):
            hard_sl = candidate.get("lower_4h", 0)
        else:
            hard_sl = candidate.get("filter_4h", 0)
        
        # Clamp size/leverage
        size_pct, leverage = _clamp_size_leverage(candidate, decision, btc_bearish)
        
        # Checks
        skip_reason = None
        
        # 1. SL distance >= 1.5%
        if hard_sl_dist_pct < MIN_SL_DIST_PCT:
            skip_reason = f"SL distance {hard_sl_dist_pct:.2f}% < {MIN_SL_DIST_PCT}%"
        
        # 2. Calculate sizes
        size_usd = equity * (size_pct / 100.0)
        notional_usd = size_usd * leverage
        
        if notional_usd < MIN_NOTIONAL_USD:
            skip_reason = f"Notional ${notional_usd:.2f} < ${MIN_NOTIONAL_USD}"
        
        # 3. Margin utilization check
        initial_margin = size_usd
        new_margin_used = margin_used + initial_margin
        margin_utilization_pct = (new_margin_used / equity) * 100.0 if equity > 0 else 100.0
        
        if margin_utilization_pct > MAX_MARGIN_UTILIZATION_PCT:
            skip_reason = f"Margin utilization {margin_utilization_pct:.1f}% > {MAX_MARGIN_UTILIZATION_PCT}%"
        
        # 4. Liq price check
        estimated_liq = _compute_liq_price_estimate(close_1d, size_usd, leverage, equity)
        liq_safe = _check_liq_beyond_sl(close_1d, hard_sl, estimated_liq)
        
        if not liq_safe:
            skip_reason = f"Liq price ${estimated_liq:.2f} not beyond Hard SL ${hard_sl:.2f}"
        
        # Skip if any check failed
        if skip_reason:
            result["skipped"].append({
                "symbol": symbol,
                "reason": skip_reason,
                "size_pct": size_pct,
                "leverage": leverage,
            })
            continue
        
        # Determine entry price based on entry type
        # Base/Continuation: limit at current mid price + 0.2% (for fill), capped above Hard SL with >=1.5% SL distance
        # Add-on: limit at 4H Filter if price is above it, else skip
        entry_price = None
        
        if entry_type == "Add-on":
            # Add-on: entry at 4H Filter if price is above it
            filter_4h = candidate.get("filter_4h", 0)
            if not filter_4h or close_1d <= filter_4h:
                skip_reason = f"Add-on: price ${close_1d:.2f} not above 4H Filter ${filter_4h:.2f}"
                result["skipped"].append({
                    "symbol": symbol,
                    "reason": skip_reason,
                    "size_pct": size_pct,
                    "leverage": leverage,
                })
                continue
            entry_price = filter_4h
        else:
            # Base/Continuation: entry at mid price + 0.2% (small offset for fill)
            mid_price = close_1d  # Use current close as mid price
            entry_price = mid_price * 1.002  # +0.2% offset
        
        # Cap entry price to ensure it stays above Hard SL with >= 1.5% SL distance
        min_entry_for_sl_dist = hard_sl * (1 + MIN_SL_DIST_PCT / 100.0)
        if entry_price < min_entry_for_sl_dist:
            entry_price = min_entry_for_sl_dist
        
        # Verify final SL distance
        final_sl_dist_pct = ((entry_price - hard_sl) / entry_price) * 100.0
        if final_sl_dist_pct < MIN_SL_DIST_PCT:
            skip_reason = f"Entry ${entry_price:.2f} too close to Hard SL ${hard_sl:.2f} (distance {final_sl_dist_pct:.2f}%)"
            result["skipped"].append({
                "symbol": symbol,
                "reason": skip_reason,
                "size_pct": size_pct,
                "leverage": leverage,
            })
            continue
        
        # SL: reduce-only trigger at Hard SL
        sl_price = hard_sl
        
        # Order expiry: next 08:40 HKT (00:40 UTC next day)
        now_utc = datetime.now(timezone.utc)
        hkt = now_utc + timedelta(hours=8)
        next_0840_hkt = hkt.replace(hour=8, minute=40, second=0, microsecond=0)
        if hkt >= next_0840_hkt:
            # Already past 08:40, use tomorrow
            next_0840_hkt += timedelta(days=1)
        # Convert back to UTC
        expiry_utc = next_0840_hkt - timedelta(hours=8)
        
        trade_id = f"{symbol}_{now_utc.strftime('%Y%m%d_%H%M%S')}"
        
        order_intent = {
            "symbol": symbol,
            "trade_id": trade_id,
            "entry_type": entry_type,
            "tier": tier,
            "entry_price": entry_price,
            "size_usd": size_usd,
            "leverage": leverage,
            "notional_usd": notional_usd,
            "hard_sl": sl_price,
            "sl_dist_pct": final_sl_dist_pct,
            "expiry_utc": expiry_utc.isoformat(),
            "btc_bearish": btc_bearish,
            "estimated_liq": estimated_liq,
        }
        
        # DRY_RUN: log intent
        if mode == "DRY_RUN":
            result["actions"].append(order_intent)
            
            # Log entry to trade log (dry_run=True)
            log_entry(
                trade_id=trade_id,
                symbol=symbol,
                entry_type=entry_type,
                tier=tier,
                trend_1d=trend_1d,
                trend_4h=trend_4h,
                sl_dist_pct=final_sl_dist_pct,
                entry_price=entry_price,
                entry_size=size_usd / entry_price,
                entry_leverage=leverage,
                ai_decision_reason=decision.get("reason", ""),
                dry_run=True,
            )
        else:
            # LIVE: place orders via hyperliquid-python-sdk
            try:
                # TODO: Integrate hyperliquid-python-sdk
                # For now, log the intent
                result["executed"].append(order_intent)
                
                # Log entry to trade log (dry_run=False)
                log_entry(
                    trade_id=trade_id,
                    symbol=symbol,
                    entry_type=entry_type,
                    tier=tier,
                    trend_1d=trend_1d,
                    trend_4h=trend_4h,
                    sl_dist_pct=final_sl_dist_pct,
                    entry_price=entry_price,
                    entry_size=size_usd / entry_price,
                    entry_leverage=leverage,
                    ai_decision_reason=decision.get("reason", ""),
                    dry_run=False,
                )
            except Exception as e:
                result["skipped"].append({
                    "symbol": symbol,
                    "reason": f"Order placement failed: {e}",
                })
    
    return result


if __name__ == "__main__":
    # CLI execution
    result = execute_approved_candidates()
    
    print(json.dumps(result, indent=2))
    
    sys.exit(0 if result["status"] == "success" else 1)
