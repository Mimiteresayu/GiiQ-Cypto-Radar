#!/usr/bin/env python3
"""Backtest (read-only, no orders): Base = fresh 1D cross only  vs  Base = fresh 1D cross + Green channel.

  A  cross + Green   (the old rule)
  B  cross only      (the current rule, MMT 2026-10-07)
  R  cross while Red (B minus A: what the new rule adds)

Signal on a CLOSED 1D bar i (close > Upper and previous close <= previous Upper); fill at the next 1D open
(the 08:55 HKT entry is just after the 08:00 daily close). Two daily-bar exit models (price terms, no leverage):
  filter : first 1D close below the 1D Filter
  lower  : first 1D close below the 1D Lower
Both also stop at MAX_HOLD_DAYS. Metric: per-trade Sharpe = mean / stdev of trade returns, plus win rate,
average, worst trade and a bootstrap 95% interval for Sharpe(B) - Sharpe(A). Small samples are flagged.

Usage:  python3 scripts/bt_base_green_vs_cross.py [--coins BTC,ETH,...] [--top 60]
Needs network access to api.hyperliquid.xyz (public candle data only).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from statistics import mean, pstdev
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scan_gc_radar import compute_gc, hl_post  # noqa: E402

WARMUP = 150            # bars before the first usable signal (GC period 144)
MAX_HOLD_DAYS = 60
MIN_N = 30              # below this the comparison is flagged "sample too small"


def fetch_1d(coin: str, n_bars: int = 1500) -> List[dict]:
    end = int(time.time() * 1000)
    start = end - (n_bars + 1) * 86_400_000
    time.sleep(0.25)
    raw = hl_post({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1d", "startTime": start, "endTime": end}})
    bars = []
    for c in raw if isinstance(raw, list) else []:
        try:
            bars.append({"t": int(c["t"]), "open": float(c["o"]), "high": float(c["h"]),
                         "low": float(c["l"]), "close": float(c["c"])})
        except (KeyError, TypeError, ValueError):
            pass
    bars.sort(key=lambda b: b["t"])
    return bars[:-1] if bars else bars        # drop the still-forming bar


def trades_for(bars: List[dict]) -> List[dict]:
    """All fresh-cross signals on closed bars with both exit models. Pure function of the bars."""
    n = len(bars)
    if n < WARMUP + 5:
        return []
    gc = compute_gc([b["high"] for b in bars], [b["low"] for b in bars], [b["close"] for b in bars],
                    poles=4, period=144, mult=1.414, reduced_lag=False, fast_response=False)
    out = []
    for i in range(WARMUP, n - 1):
        up, pup = gc[i]["upper"], gc[i - 1]["upper"]
        if not (bars[i]["close"] > up and bars[i - 1]["close"] <= pup):
            continue
        green = gc[i]["filter"] > gc[i - 1]["filter"]
        entry = bars[i + 1]["open"]
        rec = {"t": bars[i]["t"], "green": green}
        for model, key in (("filter", "filter"), ("lower", "lower")):
            exit_px, held = bars[min(n - 1, i + MAX_HOLD_DAYS)]["close"], min(n - 1 - (i + 1), MAX_HOLD_DAYS)
            for j in range(i + 1, min(n, i + 1 + MAX_HOLD_DAYS)):
                if bars[j]["close"] < gc[j][key]:
                    exit_px, held = bars[j]["close"], j - (i + 1)
                    break
            rec[model] = exit_px / entry - 1.0
            rec[model + "_days"] = held
        out.append(rec)
    return out


def stats(rets: List[float]) -> dict:
    if not rets:
        return {"n": 0}
    sd = pstdev(rets) if len(rets) > 1 else 0.0
    return {"n": len(rets), "win_rate": sum(r > 0 for r in rets) / len(rets), "avg": mean(rets),
            "worst": min(rets), "best": max(rets), "sharpe": (mean(rets) / sd) if sd > 0 else None}


def _sharpe(r: List[float]) -> Optional[float]:
    if len(r) < 2:
        return None
    sd = pstdev(r)
    return mean(r) / sd if sd > 0 else None


def sharpe_diff_ci(a: List[float], b: List[float], iters: int = 2000, seed: int = 7):
    """Bootstrap 95% interval for Sharpe(b) - Sharpe(a)."""
    rnd, diffs = random.Random(seed), []
    for _ in range(iters):
        sa, sb = _sharpe([rnd.choice(a) for _ in a]), _sharpe([rnd.choice(b) for _ in b])
        if sa is not None and sb is not None:
            diffs.append(sb - sa)
    if len(diffs) < 50:
        return None
    diffs.sort()
    return diffs[int(0.025 * len(diffs))], diffs[int(0.975 * len(diffs))]


def report(all_trades: List[dict]) -> dict:
    res = {}
    for model in ("filter", "lower"):
        A = [t[model] for t in all_trades if t["green"]]
        B = [t[model] for t in all_trades]
        R = [t[model] for t in all_trades if not t["green"]]
        res[model] = {"A_cross_green": stats(A), "B_cross_only": stats(B), "R_cross_red": stats(R),
                      "sharpe_diff_B_minus_A_95ci": sharpe_diff_ci(A, B),
                      "small_sample": min(len(A), len(B)) < MIN_N}
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="")
    ap.add_argument("--top", type=int, default=60)
    a = ap.parse_args()
    coins = [c for c in a.coins.split(",") if c]
    if not coins:
        top = json.load(open(os.path.join(ROOT, "data", "hl_volume_top100.json"))).get("top100") or []
        coins = [(x.get("symbol") if isinstance(x, dict) else x) for x in top][: a.top]
    allt, used = [], 0
    for c in coins:
        t = trades_for(fetch_1d(c))
        if t:
            used += 1
            allt += t
    print(json.dumps({"coins_with_signals": used, "coins_requested": len(coins), "signals": len(allt),
                      "result": report(allt)}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
