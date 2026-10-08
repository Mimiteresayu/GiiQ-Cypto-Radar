#!/usr/bin/env python3
"""Dry run verification for CONT-Staircase rule against historical data.

Verify entries match the backtest results for:
- LINK 2026-08-10 20:00 HKT entry 8.3062 SL 8.1617, exit 08-26 08:00 at 11.288
- HYPE 07-02 20:00 entry 65.094 SL 61.644
- INJ 07-01 20:00 entry 4.6695
- JUP 10-08 08:00 entry 0.35891 SL 0.31254
- TIA 10-08 08:00 entry 0.47211 SL 0.43557
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_gc_radar import fetch_candles, compute_gc, gc_period_for_tf
from exec_common import HKT


def analyze_coin_history(coin, entry_date_hkt, expected_entry, expected_sl):
    """Analyze historical 4H bars to find CONT-Staircase entries."""
    print(f"\n{'='*80}")
    print(f"Analyzing {coin} around {entry_date_hkt}")
    print(f"Expected entry: {expected_entry}, SL: {expected_sl}")
    print(f"{'='*80}")
    
    try:
        # Fetch 4H candles
        bars_4h = fetch_candles(coin, "4h")
        bars_1d = fetch_candles(coin, "1d")
        
        if not bars_4h or not bars_1d:
            print(f"ERROR: No candle data for {coin}")
            return False
        
        # Compute GC on both timeframes
        highs_4h = [b["high"] for b in bars_4h]
        lows_4h = [b["low"] for b in bars_4h]
        closes_4h = [b["close"] for b in bars_4h]
        gc_4h = compute_gc(highs_4h, lows_4h, closes_4h, period=72)
        
        highs_1d = [b["high"] for b in bars_1d]
        lows_1d = [b["low"] for b in bars_1d]
        closes_1d = [b["close"] for b in bars_1d]
        gc_1d = compute_gc(highs_1d, lows_1d, closes_1d, period=144)
        
        # Find the expected entry bar (convert HKT to UTC, then find bar)
        entry_dt_hkt = datetime.strptime(entry_date_hkt, "%Y-%m-%d %H:%M")
        entry_dt_hkt = entry_dt_hkt.replace(tzinfo=HKT)
        entry_dt_utc = entry_dt_hkt.astimezone(timezone.utc)
        entry_ts = int(entry_dt_utc.timestamp() * 1000)
        
        # Find the 4H bar that closed at this time (allow 2-hour window)
        entry_idx = None
        for i, bar in enumerate(bars_4h):
            bar_close_ts = bar["t"] + 4 * 3600 * 1000
            # Allow 2-hour tolerance in case of slight timestamp differences
            if abs(bar_close_ts - entry_ts) < 2 * 3600 * 1000:
                entry_idx = i
                break
        
        if entry_idx is None:
            print(f"ERROR: Could not find 4H bar closing at {entry_date_hkt}")
            return False
        
        print(f"\nFound entry bar at index {entry_idx}")
        print(f"Bar: {bars_4h[entry_idx]}")
        
        # Check CONT-Staircase trigger conditions at this bar
        i = entry_idx
        if i < 12 or i >= len(gc_4h):
            print(f"ERROR: Not enough history at index {i}")
            return False
        
        # Get corresponding 1D index
        bar_t_4h = bars_4h[i]["t"]
        j_1d = None
        for j, bar in enumerate(bars_1d):
            if bar["t"] <= bar_t_4h < bar["t"] + 86400 * 1000:
                j_1d = j
                break
        
        if j_1d is None or j_1d >= len(gc_1d):
            print(f"ERROR: Could not find corresponding 1D bar")
            return False
        
        print(f"\n1D bar index: {j_1d}")
        print(f"1D: close={closes_1d[j_1d]:.6f}, filter={gc_1d[j_1d]['filter']:.6f}, upper={gc_1d[j_1d]['upper']:.6f}, lower={gc_1d[j_1d]['lower']:.6f}")
        
        # Check 1D trend Green
        d1d_trend = "Green" if gc_1d[j_1d]["filter"] > gc_1d[j_1d-1]["filter"] else "Red"
        print(f"1. 1D trend: {d1d_trend} {'✓' if d1d_trend == 'Green' else '✗'}")
        
        # Check 4H trend Green
        d4h_trend = "Green" if gc_4h[i]["filter"] > gc_4h[i-1]["filter"] else "Red"
        print(f"2. 4H trend: {d4h_trend} {'✓' if d4h_trend == 'Green' else '✗'}")
        
        # Check Filter rising on each of last 6 bars
        filter_rising_6 = all(gc_4h[i-j]["filter"] > gc_4h[i-j-1]["filter"] for j in range(6))
        print(f"3. Filter rising last 6 bars: {filter_rising_6} {'✓' if filter_rising_6 else '✗'}")
        
        # Check band width <= 180-bar median
        lookback_end = max(0, i - 180)
        bw_current = (gc_4h[i]["upper"] - gc_4h[i]["lower"]) / gc_4h[i]["filter"]
        bw_history = [(gc_4h[j]["upper"] - gc_4h[j]["lower"]) / gc_4h[j]["filter"] 
                      for j in range(lookback_end, i) if gc_4h[j]["filter"] > 0]
        bw_median = sorted(bw_history)[len(bw_history) // 2] if bw_history else None
        bw_pct50 = bw_current <= bw_median if bw_median else None
        bw_med_str = f"{bw_median:.6f}" if bw_median else "N/A"
        print(f"4. Band width: {bw_current:.6f} vs median {bw_med_str}: {bw_pct50} {'✓' if bw_pct50 else '✗'}")
        
        # Check Filter cross-up
        close = closes_4h[i]
        filt = gc_4h[i]["filter"]
        prev_close = closes_4h[i-1]
        prev_filt = gc_4h[i-1]["filter"]
        cross_up = close > filt and prev_close <= prev_filt
        print(f"5. Filter cross-up: close {close:.6f} > filter {filt:.6f}, prev {prev_close:.6f} <= {prev_filt:.6f}: {cross_up} {'✓' if cross_up else '✗'}")
        
        # Compute SL (swing low of last 12 bars)
        swing_low = min(bars_4h[i-j]["low"] for j in range(12))
        sl_dist_pct = (close - swing_low) / close * 100.0
        print(f"\nSL calculation:")
        print(f"  Swing low (last 12 4H bars): {swing_low:.6f}")
        print(f"  SL distance: {sl_dist_pct:.2f}%")
        print(f"  Expected SL: {expected_sl}")
        print(f"  Match: {'✓' if abs(swing_low - expected_sl) / expected_sl < 0.01 else '✗'}")
        
        # Check if SL >= 1.5%
        sl_ok = sl_dist_pct >= 1.5
        print(f"  SL >= 1.5%: {sl_ok} {'✓' if sl_ok else '✗'}")
        
        # Summary
        all_ok = d1d_trend == "Green" and d4h_trend == "Green" and filter_rising_6 and bw_pct50 and cross_up and sl_ok
        print(f"\nTRIGGER: {'✓ WOULD ENTER' if all_ok else '✗ NO ENTRY'}")
        
        # Verify entry price
        entry_match = abs(close - expected_entry) / expected_entry < 0.01
        sl_match = abs(swing_low - expected_sl) / expected_sl < 0.01
        print(f"\nEntry price match: {close:.6f} vs {expected_entry} {'✓' if entry_match else '✗'}")
        print(f"SL match: {swing_low:.6f} vs {expected_sl} {'✓' if sl_match else '✗'}")
        
        return all_ok and entry_match and sl_match
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Verify CONT-Staircase rule against backtest entries."""
    print("CONT-Staircase Dry Run Verification")
    print("=" * 80)
    
    # Expected entries from backtest (variant #3: F|slope6+bw50|SW12|TR)
    entries = [
        {"coin": "LINK", "date": "2026-08-10 20:00", "entry": 8.3062, "sl": 8.1617},
        {"coin": "HYPE", "date": "2026-07-02 20:00", "entry": 65.094, "sl": 61.644},
        {"coin": "INJ", "date": "2026-07-01 20:00", "entry": 4.6695, "sl": 4.4811},
        {"coin": "JUP", "date": "2026-10-08 08:00", "entry": 0.35891, "sl": 0.31254},
        {"coin": "TIA", "date": "2026-10-08 08:00", "entry": 0.47211, "sl": 0.43557},
    ]
    
    results = []
    for e in entries:
        result = analyze_coin_history(e["coin"], e["date"], e["entry"], e["sl"])
        results.append({"coin": e["coin"], "date": e["date"], "match": result})
    
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")
    for r in results:
        status = "✓ PASS" if r["match"] else "✗ FAIL"
        print(f"{r['coin']:8s} {r['date']:20s} {status}")
    
    passed = sum(1 for r in results if r["match"])
    print(f"\nTotal: {passed}/{len(results)} passed")
    
    return passed == len(results)


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
