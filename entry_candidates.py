#!/usr/bin/env python3
"""Entry candidates builder: rule-based daily entry list (no LLM).

Rules (closed bars only, from gc_radar_1d / gc_radar_4h):
- Base: 1D Upper dual_cross_up (close > upper AND prev_close <= prev_upper) AND 1D Green
- Chase: 1D Green + 4H Green + 4H Upper dual_cross_up
- Universe: rows already in radar (dayNtlVlm >= $75k gate applied by scanner)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from mcap_tiers import tier_for
except ImportError:
    tier_for = None  # type: ignore


def _parse_radar_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse ISO timestamp from radar JSON."""
    if not ts_str:
        return None
    try:
        # Handle with or without Z suffix
        clean = ts_str.replace("Z", "+00:00")
        return datetime.fromisoformat(clean)
    except Exception:
        return None


def _radar_age_hours(ts_str: str) -> Optional[float]:
    """Calculate age in hours from radar timestamp."""
    dt = _parse_radar_timestamp(ts_str)
    if not dt:
        return None
    now = datetime.now(timezone.utc)
    delta = now - dt
    return delta.total_seconds() / 3600.0


def _extract_btc_block(radar: dict, tf: str) -> dict:
    """Extract BTC trend/close/filter info for a timeframe."""
    rows = radar.get("rows") or []
    for r in rows:
        if r.get("symbol") == "BTC":
            close = r.get("close")
            filt = r.get("filter")
            close_vs_filter = None
            if close and filt:
                close_vs_filter = round((close - filt) / close * 100, 2)
            return {
                f"trend_{tf}": r.get("trend"),
                f"close_vs_filter_{tf}": close_vs_filter,
            }
    return {}


def build_candidates(
    radar_1d: dict,
    radar_4h: dict,
    positions: Optional[List[dict]] = None,
) -> dict:
    """Build entry candidates from radar JSONs.
    
    Args:
        radar_1d: gc_radar_1d.json content
        radar_4h: gc_radar_4h.json content
        positions: Optional list of held positions (for already_held flag)
        
    Returns:
        dict with generated_at, radar metadata, btc block, candidates[]
    """
    now = datetime.now(timezone.utc)
    
    # Parse radar timestamps
    radar_1d_ts = radar_1d.get("ts", "")
    radar_4h_ts = radar_4h.get("ts", "")
    radar_1d_age_h = _radar_age_hours(radar_1d_ts)
    radar_4h_age_h = _radar_age_hours(radar_4h_ts)
    
    # Staleness flags
    stale = False
    if radar_1d_age_h and radar_1d_age_h > 36:
        stale = True
    # 4H radar refreshes every 4h (:10) and the 08:05 job now rescans 4H too; allow one
    # full 4H cycle + slack before flagging stale (was 2h -> always stale at 08:05).
    if radar_4h_age_h and radar_4h_age_h > 4.5:
        stale = True
    
    # Build position set for already_held check
    held_symbols = set()
    if positions:
        for p in positions:
            symbol = p.get("symbol") or p.get("coin")
            if symbol:
                held_symbols.add(symbol.upper())
    
    # Build 4H lookup for Chase logic
    radar_4h_rows = radar_4h.get("rows") or []
    r4h_map: Dict[str, dict] = {}
    for r in radar_4h_rows:
        symbol = r.get("symbol")
        if symbol:
            r4h_map[symbol] = r
    
    # Process 1D rows
    radar_1d_rows = radar_1d.get("rows") or []
    candidates: List[dict] = []
    
    for r1d in radar_1d_rows:
        symbol = r1d.get("symbol")
        if not symbol:
            continue
        
        # Base entry: 1D Upper dual_cross_up AND 1D Green (SoT requirement, PR #6 fix)
        dual_cross_up_1d = r1d.get("dual_cross_up", False)
        trend_1d = r1d.get("trend", "")
        base = dual_cross_up_1d and trend_1d == "Green"
        
        # Chase entry: 1D Green + 4H Green + 4H Upper dual_cross_up
        r4h = r4h_map.get(symbol)
        trend_4h = r4h.get("trend", "") if r4h else ""
        dual_cross_up_4h = r4h.get("dual_cross_up", False) if r4h else False
        
        chase = (
            trend_1d == "Green"
            and trend_4h == "Green"
            and dual_cross_up_4h
        )
        
        # Skip if neither Base nor Chase
        if not base and not chase:
            continue
        
        # Determine type
        entry_type = "Chase" if chase else "Base"
        
        # Extract fields
        close_1d = r1d.get("close")
        upper_1d = r1d.get("upper")
        filter_1d = r1d.get("filter")
        lower_1d = r1d.get("lower")
        filter_4h = r4h.get("filter") if r4h else None
        lower_4h = r4h.get("lower") if r4h else None
        day_ntl_vlm = r1d.get("day_ntl_vlm") or r1d.get("dayNtlVlm")
        
        # Tier
        tier = tier_for(symbol) if tier_for else "unknown"
        
        # Hard SL distance (tier-dependent)
        # Mega/Large: Hard SL = 4H Lower
        # Small/Tiny: Hard SL = 4H Filter (mid)
        hard_sl_dist_pct = None
        hard_sl_level = None
        if tier in ("mega", "large"):
            hard_sl_level = lower_4h
        else:  # small, tiny, unknown
            hard_sl_level = filter_4h
        
        if close_1d and hard_sl_level:
            hard_sl_dist_pct = round((close_1d - hard_sl_level) / close_1d * 100, 2)
        
        # Already held
        already_held = symbol in held_symbols
        
        candidates.append({
            "symbol": symbol,
            "type": entry_type,
            "tier": tier,
            "trend_1d": trend_1d,
            "trend_4h": trend_4h,
            "close_1d": close_1d,
            "upper_1d": upper_1d,
            "filter_1d": filter_1d,
            "lower_1d": lower_1d,
            "filter_4h": filter_4h,
            "lower_4h": lower_4h,
            "hard_sl_dist_pct": hard_sl_dist_pct,
            "dayNtlVlm": day_ntl_vlm,
            "already_held": already_held,
        })
    
    # Sort: Chase first, then by symbol
    candidates.sort(key=lambda c: (0 if c["type"] == "Chase" else 1, c["symbol"]))
    
    # BTC block
    btc_1d = _extract_btc_block(radar_1d, "1d")
    btc_4h = _extract_btc_block(radar_4h, "4h")
    btc_block = {**btc_1d, **btc_4h}
    
    return {
        "generated_at": now.isoformat(),
        "radar_1d_asof": radar_1d_ts,
        "radar_4h_asof": radar_4h_ts,
        "radar_1d_age_h": round(radar_1d_age_h, 2) if radar_1d_age_h else None,
        "radar_4h_age_h": round(radar_4h_age_h, 2) if radar_4h_age_h else None,
        "stale": stale,
        "btc": btc_block,
        "count": len(candidates),
        "candidates": candidates,
    }


def main():
    """CLI: read radar JSONs from out/, write entry_candidates_latest.json."""
    out_dir = ROOT / "out"
    
    # Load radars
    try:
        with open(out_dir / "gc_radar_1d.json") as f:
            radar_1d = json.load(f)
    except FileNotFoundError:
        print("ERROR: gc_radar_1d.json not found", file=sys.stderr)
        sys.exit(1)
    
    try:
        with open(out_dir / "gc_radar_4h.json") as f:
            radar_4h = json.load(f)
    except FileNotFoundError:
        print("ERROR: gc_radar_4h.json not found", file=sys.stderr)
        sys.exit(1)
    
    # Build candidates
    result = build_candidates(radar_1d, radar_4h)
    
    # Write output
    out_file = out_dir / "entry_candidates_latest.json"
    with open(out_file, "w") as f:
        json.dump(result, f, indent=2)
    
    print(f"Generated {result['count']} candidates → {out_file}")
    for c in result["candidates"][:5]:  # show first 5
        print(f"  {c['symbol']:8s} {c['type']:5s} tier={c['tier']}")


if __name__ == "__main__":
    main()
