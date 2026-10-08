#!/usr/bin/env python3
"""Manual LONG entry for HL (MMT/agent-requested, authenticated X-AI-Key).

POST body: {
  "symbol": "BTC",
  "size_pct": 3.0,           # margin as % of NAV
  "leverage": 4,
  "sl_override": 42000.0,    # optional, must be >= Hard SL rule
  "dry_run": true            # default true - set false to execute
}

Same fail-closed checks as executor.py: kill switch, margin 80%/20% caps, liq beyond SL, signing, min size.
Logged as entry_type=MANUAL. Never raises (returns error dict on failure).
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from exec_common import (  # noqa: E402
    HKT, MAX_COIN_NOTIONAL_NAV_PCT, MAX_MARGIN_UTILIZATION_PCT, MAX_TOTAL_MARGIN_NAV_PCT,
    MIN_SL_DIST_PCT, SOT_ID, coin_notional_ok, hard_sl_for_tier, is_live_mode, margin_cap_ok,
    min_order_usd, order_qty, parse_ts, price_sane, radar_ref_price, radar_rowcount_ok,
    round_price, size_by_margin, total_margin_nav_ok,
)

RADAR_MAX_AGE_H = {"4h": 5.0, "1d": 26.0}


def _log(msg: str) -> None:
    sys.stderr.write(f"[MANUAL_ORDER] {msg}\n")
    sys.stderr.flush()


def _load_json(name: str) -> dict:
    try:
        return json.loads((ROOT / "out" / name).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def manual_entry_hl(symbol: str, size_pct: float, leverage: int, sl_override: Optional[float] = None,
                    dry_run: bool = True, now: Optional[datetime] = None, hl: Any = None,
                    tier_fn=None, log_entry_fn=None) -> Dict[str, Any]:
    """Execute one manual LONG entry for HL. Returns {ok, ...} or {ok: false, error}."""
    now = now or datetime.now(timezone.utc)
    mode = "DRY_RUN" if dry_run or not is_live_mode() else "LIVE"
    res: Dict[str, Any] = {"ok": False, "mode": mode, "symbol": symbol, "timestamp": now.isoformat()}
    
    # Load radar
    radar_1d = _load_json("gc_radar_1d.json")
    radar_4h = _load_json("gc_radar_4h.json")
    for tf, rd in (("1d", radar_1d), ("4h", radar_4h)):
        ok_rc, why_rc = radar_rowcount_ok(rd, tf)
        if not ok_rc:
            res["error"] = f"Radar row-count check failed: {why_rc}"
            return res
        ts = parse_ts(rd.get("ts"))
        age = (now - ts).total_seconds() / 3600.0 if ts else 999
        if age > RADAR_MAX_AGE_H.get(tf, 99):
            res["error"] = f"{tf.upper()} radar stale ({age:.1f}h old)"
            return res
    
    rows_1d = {r.get("symbol"): r for r in radar_1d.get("rows", []) or []}
    rows_4h = {r.get("symbol"): r for r in radar_4h.get("rows", []) or []}
    
    # HL account state
    if hl is None:
        from hl_exec import HLClient
        hl = HLClient(os.environ.get("HL_ADDRESS") or None)
    
    try:
        from executor import _check_all_positions_liq_safe, _parse_account, _user_abstraction
        perp_raw = hl.perp_state()
        account = _parse_account(hl.spot_state(), perp_raw, _user_abstraction(hl))
        meta, mids = hl.meta(), hl.all_mids()
    except Exception as e:  # noqa: BLE001
        res["error"] = f"HL state failed: {e}"
        return res
    
    equity, cum_margin = account["equity"], account["margin_used"]
    res["nav"] = account["nav"]
    if equity <= 0:
        res["error"] = "Equity is 0 / unavailable"
        return res
    
    # Check liq safety for all positions
    liq_safe, unsafe = _check_all_positions_liq_safe(account["positions"], {}, radar_4h)
    if not liq_safe:
        res["error"] = f"Unsafe liquidation prices for: {', '.join(unsafe)}"
        return res
    
    # Get coin data
    cm = meta.get(symbol)
    if not cm:
        res["error"] = f"{symbol} not in HL meta"
        return res
    
    sz_dec, coin_max = int(cm.get("szDecimals", 0)), cm.get("maxLeverage")
    
    if tier_fn is None:
        from mcap_tiers import tier_for as tier_fn  # noqa: N813
    tier = tier_fn(symbol) or "unknown"
    
    # Hard SL
    hard_sl, sl_label = hard_sl_for_tier(tier, rows_4h.get(symbol))
    if sl_override is not None:
        if not hard_sl or sl_override < hard_sl:
            res["error"] = f"SL override {sl_override:.6g} < Hard SL {hard_sl:.6g} ({sl_label})"
            return res
        hard_sl, sl_label = sl_override, f"manual override (>= {sl_label})"
    
    if not hard_sl or hard_sl <= 0:
        res["error"] = f"No Hard SL available ({tier})"
        return res
    
    # Mid price
    mid = mids.get(symbol)
    if not mid or mid <= 0:
        res["error"] = f"Live mid unavailable"
        return res
    
    if not price_sane(mid, radar_ref_price(rows_4h.get(symbol)) or mid):
        res["error"] = "Price sanity check failed (mid vs radar > 50%)"
        return res
    
    # Size
    sz = size_by_margin(equity, mid, hard_sl, coin_max, ai_size_pct=size_pct, ai_leverage=leverage,
                       tier=tier, flat_tier_size=False)
    if not sz["ok"]:
        res["error"] = f"Sizing failed: {sz['reason']}"
        return res
    
    margin_usd, lev = sz["margin_usd"], sz["leverage"]
    qty = order_qty(margin_usd, lev, mid, sz_dec)
    if qty <= 0:
        res["error"] = "Order qty <= 0"
        return res
    
    notional = qty * mid
    if notional < min_order_usd(equity):
        res["error"] = f"Order notional ${notional:.2f} < minimum"
        return res
    
    # SL distance
    sl_dist_pct = (mid - hard_sl) / mid * 100.0
    if sl_dist_pct < MIN_SL_DIST_PCT - 1e-9:
        res["error"] = f"SL distance {sl_dist_pct:.2f}% < {MIN_SL_DIST_PCT:g}%"
        return res
    
    # Margin caps
    ok_tot, tot_pct = total_margin_nav_ok(cum_margin, margin_usd, equity)
    if not ok_tot:
        res["error"] = f"Total margin {tot_pct:.1f}% > {MAX_TOTAL_MARGIN_NAV_PCT:g}% NAV"
        return res
    
    ok_coin, coin_pct = coin_notional_ok(0.0, notional, equity)
    if not ok_coin:
        res["error"] = f"Coin notional {coin_pct:.1f}% > {MAX_COIN_NOTIONAL_NAV_PCT:g}% NAV"
        return res
    
    ok_util, util = margin_cap_ok(cum_margin + margin_usd, equity)
    if not ok_util:
        res["error"] = f"Margin utilization {util:.1f}% > {MAX_MARGIN_UTILIZATION_PCT:g}%"
        return res
    
    # Build order plan
    limit_px = round_price(mid * 1.005, sz_dec, up=True)
    res.update({
        "ok": True,
        "tier": tier,
        "entry_type": "MANUAL",
        "mid": mid,
        "limit_px": limit_px,
        "qty": qty,
        "size_pct": round(margin_usd / equity * 100.0, 3),
        "margin_usd": round(margin_usd, 2),
        "leverage": lev,
        "notional_usd": round(notional, 2),
        "hard_sl": hard_sl,
        "hard_sl_label": sl_label,
        "sl_dist_pct": round(sl_dist_pct, 3),
        "total_margin_nav_pct": round(tot_pct, 2),
    })
    
    if dry_run or not is_live_mode():
        res["dry_run"] = True
        _log(f"{mode} {symbol}: would enter at {limit_px:.6g} qty {qty} lev {lev}x SL {hard_sl:.6g}")
        return res
    
    # LIVE execution
    try:
        from hl_exec import enter_long_with_sl
        if log_entry_fn is None:
            from trade_log import log_entry as log_entry_fn  # noqa: N813
        
        fill = enter_long_with_sl(hl, symbol, qty, limit_px, hard_sl, lev, log_entry_fn,
                                 entry_type="MANUAL", tier=tier)
        res["fill"] = fill
        _log(f"LIVE {symbol}: entered at {fill.get('entry_px')} qty {fill.get('qty')} SL {hard_sl:.6g}")
    except Exception as e:  # noqa: BLE001
        res["ok"] = False
        res["error"] = f"Execution failed: {e}"
        _log(f"LIVE {symbol}: FAILED {e}")
    
    return res


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Manual HL LONG entry")
    parser.add_argument("symbol", help="Coin symbol (e.g. BTC)")
    parser.add_argument("size_pct", type=float, help="Margin as % of NAV")
    parser.add_argument("leverage", type=int, help="Leverage (e.g. 4)")
    parser.add_argument("--sl", type=float, help="SL override (must be >= Hard SL)")
    parser.add_argument("--live", action="store_true", help="Execute (default is dry-run)")
    args = parser.parse_args()
    
    result = manual_entry_hl(args.symbol.upper(), args.size_pct, args.leverage, args.sl, not args.live)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)
