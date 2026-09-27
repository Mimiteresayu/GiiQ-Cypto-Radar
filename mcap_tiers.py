#!/usr/bin/env python3
"""Circulating-mcap size tiers for Harbor exits (LOCKED 2026-09-21).

Tier = circulating mcap ONLY (no liquidity demotion).
Unknown mcap → Tiny.

Optional override: out/mcap_cache.json
  { "BTC": 1800000000000, "ETH": 400000000000, ... }  # USD
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "out" / "mcap_cache.json"
# Baked seed for Railway (out/ is often an empty volume mount)
DATA_CACHE = ROOT / "data" / "mcap_cache.json"

# Thresholds (USD circulating)
MEGA_MIN = 50_000_000_000      # ≥ $50B
LARGE_MIN = 2_000_000_000      # $2B – <$50B
SMALL_MIN = 200_000_000        # $200M – <$2B
# Tiny: <$200M

# Static seed mcaps (approx USD) — MVP; refresh via mcap_cache.json when available
SEED_MCAP_USD: Dict[str, float] = {
    "BTC": 1_800_000_000_000,
    "ETH": 400_000_000_000,
    "SOL": 100_000_000_000,
    "TRX": 25_000_000_000,
    "HYPE": 12_000_000_000,
    "ZEC": 4_000_000_000,
    "AVAX": 10_000_000_000,
    "BRETT": 150_000_000,
    "NIL": 80_000_000,
    "AVNT": 50_000_000,
}

# Canonical primary exit rule labels (match SoT)
PRIMARY_RULE = {
    "mega": "4h_filter_cross_down",
    "large": "4h_filter_cross_down",  # same as Mega (LOCKED Mega=Large)
    "small": "1h_lower_cross_down",   # out of channel (LOCKED Small=Tiny)
    "tiny": "1h_lower_cross_down",
}

HARD_SL_BY_TIER = {
    "mega": "4h_lower",   # 4H Lower (period 72)
    "large": "4h_lower",  # same as Mega
    "small": "4h_filter",  # 4H Filter (mid, period 72)
    "tiny": "4h_filter",
}
HARD_SL_RULE = "4h_filter"  # default for unknown tiers; per-tier SoT above (= exec_common.hard_sl_for_tier)



def _norm(symbol: str) -> str:
    return str(symbol or "").strip().upper().lstrip("$")


def _merge_cache_file(m: Dict[str, float], path: Path) -> None:
    if not path.is_file():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict):
        return
    for k, v in raw.items():
        if str(k).startswith("_"):
            continue
        try:
            m[_norm(k)] = float(v)
        except (TypeError, ValueError):
            pass


def load_mcap_map() -> Dict[str, float]:
    """Seed + data/mcap_cache.json (baked) + out/mcap_cache.json overrides (USD)."""
    m = {k.upper(): float(v) for k, v in SEED_MCAP_USD.items()}
    _merge_cache_file(m, DATA_CACHE)
    _merge_cache_file(m, CACHE)
    return m


def mcap_usd(symbol: str, override: Optional[float] = None) -> Optional[float]:
    if override is not None:
        try:
            return float(override)
        except (TypeError, ValueError):
            return None
    return load_mcap_map().get(_norm(symbol))


def tier_from_mcap(usd: Optional[float]) -> str:
    """Map USD circulating mcap → mega|large|small|tiny. None/invalid → tiny."""
    if usd is None:
        return "tiny"
    try:
        x = float(usd)
    except (TypeError, ValueError):
        return "tiny"
    if x < 0 or x != x:  # NaN
        return "tiny"
    if x >= MEGA_MIN:
        return "mega"
    if x >= LARGE_MIN:
        return "large"
    if x >= SMALL_MIN:
        return "small"
    return "tiny"


def tier_for(symbol: str, mcap_override: Optional[float] = None) -> str:
    """Return mega|large|small|tiny for symbol. Unknown → tiny."""
    return tier_from_mcap(mcap_usd(symbol, mcap_override))


def tier_info(symbol: str, mcap_override: Optional[float] = None) -> Dict[str, object]:
    usd = mcap_usd(symbol, mcap_override)
    t = tier_from_mcap(usd)
    return {
        "symbol": _norm(symbol),
        "tier": t,
        "mcap_usd": usd,
        "mcap_known": usd is not None,
        "primary_rule": PRIMARY_RULE[t],
        "hard_sl_rule": HARD_SL_BY_TIER.get(t, HARD_SL_RULE),
    }


def sample_table(symbols=None) -> list:
    syms = symbols or [
        "BTC", "ETH", "SOL", "TRX", "HYPE", "ZEC", "AVAX", "BRETT", "NIL", "AVNT",
    ]
    return [tier_info(s) for s in syms]


if __name__ == "__main__":
    for row in sample_table():
        m = row["mcap_usd"]
        m_s = f"${m/1e9:.1f}B" if m and m >= 1e9 else (f"${m/1e6:.0f}M" if m else "?")
        print(f"{row['symbol']:<6} {row['tier']:<6} mcap≈{m_s:<10} primary={row['primary_rule']}")
