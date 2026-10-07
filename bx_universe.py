#!/usr/bin/env python3
"""Bitunix (BX) universe rules — pure functions, no network (Harbor-approved design, 2026-09-29).

Display / shadow only. Nothing here is read by executor.py, pending_worker.py, exit_worker.py,
entry_candidates.py or hl_exec.py (enforced by test_bx_isolation.py).

  match_hl()          BX contract -> HL perp name (exact / 1000X->kX / 1M prefix / alias / override),
                      accepted only if prices agree within MATCH_PX_TOL after the unit scale.
  asset_class()       crypto | stock | commodity | index_etf | unknown (fail-closed: unknown never counts)
  listing_age()       contract_age_days, new_contract, asset_first_seen, asset_age
  liq_tier()          tradeable | watch | exclude
  ignition()          24h USD volume / mean of the 7 prior closed 1D bars
  gc_tf_for()         longest GC timeframe the bar history supports (1d > 4h > 1h > none)
  shadow_slippage_bp  max(5 bp, spread / 2)
  session_gap()       underlying TradFi market shut during the bar
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

DAY_MS = 86_400_000

# --- Harbor-approved thresholds -------------------------------------------------------------
NEW_CONTRACT_DAYS = 30
NEW_TOKEN_DAYS = 30            # asset first seen < 30 days before the BX launch -> new token
VOL_TRADEABLE = 2_000_000.0
VOL_WATCH_MIN = 200_000.0
SPREAD_TRADEABLE_BP = 10.0
MAX_SIZE_OF_VOL = 0.005        # shadow notional <= 0.5% of 24h volume
IGNITION_X = 3.0
IGNITION_LOOKBACK = 7
MIN_SLIPPAGE_BP = 5.0
MATCH_PX_TOL = 0.03            # BX vs HL price agreement for an automatic symbol match
CG_PX_TOL = 0.05               # BX vs CoinGecko price agreement for a CoinGecko match
# GC periods (locked, same as scan_gc_radar.TF_CONFIG) and the same warm-up rule as the HL scan
GC_PERIOD = {"1d": 144, "4h": 72, "1h": 48}
GC_WARMUP = 20
COUNTED_GC_TFS = ("1d", "4h")  # Harbor: gc_tf=1h signals are watch-only, excluded from the 30-signal test

ASSET_CLASSES = ("crypto", "stock", "commodity", "index_etf", "unknown")
TRADFI = ("stock", "commodity", "index_etf")

# HL lists 1000x memes with a k-prefix and a few names differently (kept in sync with scan_gc_radar)
HL_ALIASES = {"SPX6900": "SPX", "BONK": "kBONK", "PEPE": "kPEPE", "SHIB": "kSHIB", "FLOKI": "kFLOKI",
              "LUNC": "kLUNC", "NEIRO": "kNEIRO", "DOGS": "kDOGS"}
_MULT_PREFIX = re.compile(r"^(1000000|1000|1M|10000|100)(?=[A-Z])")


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


# ---------------------------------------------------------------------------------------------
# Symbol parsing + BX <-> HL match
# ---------------------------------------------------------------------------------------------
def split_multiplier(base: str) -> Tuple[str, int]:
    """'1000PEPE' -> ('PEPE', 1000); '1MBABYDOGE' -> ('BABYDOGE', 1_000_000); 'BTC' -> ('BTC', 1)."""
    b = str(base or "").strip().upper()
    m = _MULT_PREFIX.match(b)
    if not m:
        return b, 1
    pre = m.group(1)
    return b[len(pre):], (1_000_000 if pre in ("1M", "1000000") else int(pre))


def hl_multiplier(hl_name: str) -> Tuple[str, int]:
    """HL 'kPEPE' -> ('PEPE', 1000); anything else -> (name, 1). Only a lowercase k marks a multiplier,
    so real names that start with K (KAITO, KAS) are never stripped."""
    n = str(hl_name or "")
    if len(n) > 1 and n[0] == "k" and n[1:].isupper():
        return n[1:], 1000
    return n.upper(), 1


def match_hl(base: str, bx_price: Optional[float], hl_mids: Dict[str, float],
             overrides: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """-> {hl_name, match_rule, px_scale, px_diff} (hl_name None when no verified match).

    px_scale = BX price / HL price expected from the unit multipliers (1000PEPE vs kPEPE -> 1).
    An override is trusted without a price check (it is a human decision); every automatic rule needs
    |BX / (HL x scale) - 1| <= MATCH_PX_TOL, so a same-ticker different asset is rejected."""
    b = str(base or "").strip().upper()
    ov = (overrides or {}).get(b)
    if ov:
        return {"hl_name": ov if ov in hl_mids else None, "match_rule": "override",
                "px_scale": None, "px_diff": None}
    core, bx_mult = split_multiplier(b)
    names = list(hl_mids.keys())
    by_upper = {n.upper(): n for n in names if not (len(n) > 1 and n[0] == "k" and n[1:].isupper())}
    tries: List[Tuple[str, str]] = []
    if b in by_upper:
        tries.append((by_upper[b], "exact"))
    if bx_mult == 1000 and ("k" + core) in hl_mids:
        tries.append(("k" + core, "1000k"))
    if bx_mult > 1 and core in by_upper:
        tries.append((by_upper[core], "multiplier"))
    alias = HL_ALIASES.get(core) or HL_ALIASES.get(b)
    if alias and alias in hl_mids:
        tries.append((alias, "alias"))
    px = _f(bx_price)
    for name, rule in tries:
        hl_px = _f(hl_mids.get(name))
        _, hl_mult = hl_multiplier(name)
        scale = bx_mult / hl_mult
        if not px or not hl_px:
            continue
        diff = px / (hl_px * scale) - 1
        if abs(diff) <= MATCH_PX_TOL:
            return {"hl_name": name, "match_rule": rule, "px_scale": scale, "px_diff": round(diff, 5)}
    return {"hl_name": None, "match_rule": "none" if not tries else "price_mismatch",
            "px_scale": None, "px_diff": None}


def ex_label(on_hl: bool, on_bx: bool) -> str:
    return "HL+BX" if on_hl and on_bx else ("HL" if on_hl else "BX")


# ---------------------------------------------------------------------------------------------
# Asset class + session gaps
# ---------------------------------------------------------------------------------------------
def asset_class(base: str, seed: Dict[str, str], hl_matched: bool, cg_matched: bool) -> str:
    """Seed file wins (TradFi list reviewed by Harbor); then crypto if the asset is on HL or matched on
    CoinGecko by symbol + price; otherwise unknown (excluded from breadth and candidates until reviewed)."""
    core, _ = split_multiplier(base)
    for k in (str(base or "").upper(), core):
        cls = (seed or {}).get(k)
        if cls in ASSET_CLASSES:
            return cls
    if hl_matched or cg_matched:
        return "crypto"
    return "unknown"


def session_gap(cls: str, bar_open_ms: int, tf: str = "1d") -> bool:
    """True when the underlying TradFi market is shut for the whole bar (UTC, regular hours only;
    holidays are not modelled). Crypto / unknown -> False."""
    if cls not in TRADFI:
        return False
    d = datetime.fromtimestamp(bar_open_ms / 1000, tz=timezone.utc)
    if tf == "1d":
        if cls == "commodity":
            return d.weekday() == 5          # Saturday: CME energy / metals shut all day
        return d.weekday() >= 5              # stocks, indices, ETFs: Sat + Sun
    # intraday bar: open hours per class
    hours = {"1h": 1, "4h": 4}.get(tf, 1)
    for h in range(hours):
        x = datetime.fromtimestamp(bar_open_ms / 1000 + h * 3600, tz=timezone.utc)
        if _tradfi_open(cls, x):
            return False
    return True


def _tradfi_open(cls: str, t: datetime) -> bool:
    wd, hm = t.weekday(), t.hour * 60 + t.minute
    if cls == "commodity":   # CME Globex ~ Sun 22:00 - Fri 21:00 UTC, daily break 21:00-22:00 UTC
        if wd == 5 or (wd == 4 and hm >= 21 * 60) or (wd == 6 and hm < 22 * 60):
            return False
        return not (21 * 60 <= hm < 22 * 60)
    if wd >= 5:               # stocks / index / ETF regular session 13:30-20:00 UTC (US, standard time aside)
        return False
    return 13 * 60 + 30 <= hm < 20 * 60


# ---------------------------------------------------------------------------------------------
# Listing age
# ---------------------------------------------------------------------------------------------
def listing_age(launch_ms: Optional[int], first_bar_ms: Optional[int], now_ms: int, cls: str,
                first_seen_ms: Iterable[Optional[int]] = ()) -> Dict[str, Any]:
    """contract_age_days from launchTime (else first BX 1D bar); asset_first_seen = earliest of the
    given external dates (HL first candle, CoinGecko first price) and the BX launch.
    asset_age: old | old_asset_new_contract | new_token | unknown."""
    launch = launch_ms or first_bar_ms
    age = int((now_ms - launch) // DAY_MS) if launch else None
    new_contract = age is not None and age < NEW_CONTRACT_DAYS
    ext = [int(x) for x in first_seen_ms if x]
    first = min(ext + ([launch] if launch else [])) if (ext or launch) else None
    if cls in TRADFI:
        asset_age = "old_asset_new_contract" if new_contract else "old"
    elif not new_contract:
        asset_age = "old"
    elif not ext:
        asset_age = "unknown"      # no HL / CoinGecko history: treated like a new token, never tagged C
    elif launch and launch - min(ext) >= NEW_TOKEN_DAYS * DAY_MS:
        asset_age = "old_asset_new_contract"
    else:
        asset_age = "new_token"
    return {"contract_age_days": age, "new_contract": new_contract,
            "asset_first_seen": datetime.fromtimestamp(first / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if first else None,
            "asset_age": asset_age}


def is_new_token(asset_age: str) -> bool:
    return asset_age in ("new_token", "unknown")


# ---------------------------------------------------------------------------------------------
# Liquidity, ignition, slippage, GC timeframe
# ---------------------------------------------------------------------------------------------
def ignition(vol24h: Optional[float], prior_daily_vols: List[float]) -> Optional[float]:
    """24h USD vol / mean of the last 7 CLOSED 1D volumes (needs >= 3 of them)."""
    v = _f(vol24h)
    prior = [x for x in (prior_daily_vols or [])[-IGNITION_LOOKBACK:] if _f(x) is not None and x > 0]
    if v is None or len(prior) < 3:
        return None
    return round(v / (sum(prior) / len(prior)), 2)


def liq_tier(vol24h: Optional[float], status: str, delist_ms: Optional[int], cls: str,
             spread_bp: Optional[float], ign_x: Optional[float], is_cemetery: bool, new_tok: bool) -> str:
    """Harbor tiers. exclude: vol < $0.2M (unless ignition-flagged C / new token), not OPEN, delisting,
    unknown class. tradeable: vol >= $2M and spread <= 10 bp (unknown spread -> watch)."""
    v = _f(vol24h) or 0.0
    if str(status or "").upper() != "OPEN" or delist_ms or cls == "unknown":
        return "exclude"
    ign = ign_x is not None and ign_x >= IGNITION_X and (is_cemetery or new_tok)
    if v >= VOL_TRADEABLE and spread_bp is not None and spread_bp <= SPREAD_TRADEABLE_BP:
        return "tradeable"
    if v >= VOL_WATCH_MIN or ign:
        return "watch"
    return "exclude"


def max_notional_usd(vol24h: Optional[float]) -> Optional[float]:
    v = _f(vol24h)
    return round(v * MAX_SIZE_OF_VOL, 2) if v else None


def shadow_slippage_bp(spread_bp: Optional[float]) -> float:
    """Harbor: max(5 bp, half the spread). Unknown spread -> 5 bp."""
    s = _f(spread_bp)
    return max(MIN_SLIPPAGE_BP, (s or 0.0) / 2.0)


def min_bars(tf: str) -> int:
    return GC_PERIOD[tf] + GC_WARMUP


def gc_tf_for(n_bars_by_tf: Dict[str, int]) -> Optional[str]:
    """Longest TF with enough closed bars for a GC (same period+20 warm-up as the HL scan)."""
    for tf in ("1d", "4h", "1h"):
        if int(n_bars_by_tf.get(tf) or 0) >= min_bars(tf):
            return tf
    return None


def counts_in_test(gc_tf: Optional[str], tier: str) -> bool:
    """Only tradeable-tier signals on a 1D or 4H GC count toward the BX-vs-HL 30-signal test."""
    return tier == "tradeable" and gc_tf in COUNTED_GC_TFS


# ---------------------------------------------------------------------------------------------
# CoinGecko match (pure; data comes from cg_client)
# ---------------------------------------------------------------------------------------------
def cg_match(base: str, bx_price: Optional[float], cg_by_symbol: Dict[str, List[dict]]) -> Optional[dict]:
    """Pick the CoinGecko coin with the same symbol whose USD price agrees within CG_PX_TOL after the
    unit multiplier (1000PEPE -> PEPE x 1000). Ties -> highest market cap."""
    core, mult = split_multiplier(base)
    px = _f(bx_price)
    if not px:
        return None
    best = None
    for c in cg_by_symbol.get(core.lower(), []) or []:
        cp = _f(c.get("current_price"))
        if not cp:
            continue
        if abs(px / (cp * mult) - 1) <= CG_PX_TOL:
            if best is None or (_f(c.get("market_cap")) or 0) > (_f(best.get("market_cap")) or 0):
                best = c
    return best
