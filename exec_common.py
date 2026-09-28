#!/usr/bin/env python3
"""Shared, side-effect-free execution rules (SoT) for executor / exit_worker / serve.

Everything here is pure (no network) so it can be unit tested directly.

SoT (do not change without MMT):
- Mega/Large: primary exit = 4H close < 4H Filter; Hard SL = 4H Lower
- Small/Tiny: primary exit = 1H close < 1H Lower; Hard SL = 4H Filter (mid)
- Leverage 1-5x (and never above the coin's HL maxLeverage), min notional $10,
  total margin <= 80% equity, liquidation must lie beyond the Hard SL.

Sizing convention: ``size_pct`` = MARGIN as % of equity; notional = margin x leverage.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------- constants
MIN_NOTIONAL_USD = 10.0
MIN_LEVERAGE = 1.0
MAX_LEVERAGE = 5.0
MIN_SL_DIST_PCT = 1.5
MAX_MARGIN_UTILIZATION_PCT = 80.0
DEFAULT_MAX_CANDIDATE_AGE_H = 3.0  # 08:05 build -> 08:55 execute (+ slack)
HKT = timezone(timedelta(hours=8))


def is_live_mode() -> bool:
    """LIVE only when EXEC_DRY_RUN is explicitly 0/false/no AND HL_API_PRIVATE_KEY is present.

    Read at call time (not import time) so tests / subprocess env overrides work.
    """
    dry = (os.environ.get("EXEC_DRY_RUN", "1") or "1").strip().lower()
    key = (os.environ.get("HL_API_PRIVATE_KEY") or "").strip()
    return dry in ("0", "false", "no") and bool(key)


# ---------------------------------------------------------------- tier rules
def hard_sl_for_tier(tier: str, row_4h: Optional[dict]) -> Tuple[Optional[float], str]:
    """Return (hard_sl_level, label) for a LONG per tier SoT."""
    if not row_4h:
        return None, "missing 4H radar"
    if (tier or "").lower() in ("mega", "large"):
        return _f(row_4h.get("lower")), "4H Lower"
    return _f(row_4h.get("filter")), "4H Filter"


def entry_upper_ref(cand: dict, row_1d: Optional[dict], row_4h: Optional[dict]) -> Tuple[Optional[float], str]:
    """Signal-TF Upper for the at-entry guard (latest CLOSED-bar scan).

    Chase -> 4H Upper; Base (and anything else) -> 1D Upper. Prefers the current radar row
    (newest closed-bar scan) and falls back to the value frozen in the candidate."""
    if (cand.get("type") or "") == "Chase":
        v = _f((row_4h or {}).get("upper"))
        return (v if v else _f(cand.get("upper_4h"))), "4H Upper"
    v = _f((row_1d or {}).get("upper"))
    return (v if v else _f(cand.get("upper_1d"))), "1D Upper"


def above_upper_at_entry(mid: Optional[float], upper: Optional[float]) -> bool:
    """Fail-closed: entry only if live mid is strictly above the signal-TF Upper."""
    if not mid or mid <= 0 or not upper or upper <= 0:
        return False
    return mid > upper


# ---------------------------------------------------------------- liquidation
def maintenance_rate(max_leverage: Optional[float], leverage: float) -> float:
    """HL maintenance margin rate = 1 / (2 * maxLeverage).

    If the coin's maxLeverage is unknown we fall back to the chosen leverage, which is the
    most conservative consistent value (maxLeverage >= leverage always holds).
    """
    ml = max_leverage if (max_leverage and max_leverage > 0) else leverage
    ml = max(float(ml), float(leverage), 1.0)
    return 1.0 / (2.0 * ml)


def isolated_liq_price_long(entry_price: float, leverage: float, max_leverage: Optional[float]) -> Optional[float]:
    """Liquidation price of an ISOLATED long (HL formula).

    liq = price - side * margin_available / position_size / (1 - l * side)
    with side=+1, margin_available = notional/lev - notional*l, l = maintenance rate:
        liq = entry * (1 - (1/lev - l) / (1 - l))

    Independent of position size and of account equity (unlike the old cross formula that
    subtracted the whole account equity and went negative). Never negative.
    """
    if not entry_price or entry_price <= 0 or not leverage or leverage <= 0:
        return None
    lev = float(leverage)
    l = maintenance_rate(max_leverage, lev)
    frac = (1.0 / lev - l) / (1.0 - l)
    return max(0.0, entry_price * (1.0 - frac))


def liq_beyond_sl_long(liq_price: Optional[float], hard_sl: Optional[float]) -> bool:
    """LONG: safe iff liquidation strictly below the Hard SL (SL fires first)."""
    if liq_price is None or not hard_sl:
        return False
    return liq_price < hard_sl


# ---------------------------------------------------------------- sizing
def clamp_leverage(requested: Any, coin_max_leverage: Optional[float]) -> int:
    """Clamp to SoT 1-5x AND the coin's HL maxLeverage; HL needs an integer leverage."""
    try:
        req = float(requested) if requested is not None else 2.0
    except (TypeError, ValueError):
        req = 2.0
    cap = MAX_LEVERAGE
    if coin_max_leverage and coin_max_leverage > 0:
        cap = min(cap, float(coin_max_leverage))
    lev = max(MIN_LEVERAGE, min(cap, req))
    return max(1, int(math.floor(lev + 1e-9)))


def floor_to_decimals(x: float, decimals: int) -> float:
    if decimals <= 0:
        return float(math.floor(x + 1e-12))
    q = 10 ** decimals
    return math.floor(x * q + 1e-9) / q


def order_qty(notional_usd: float, price: float, sz_decimals: int) -> float:
    """Coin quantity = notional / price, floored to the coin's szDecimals."""
    if not price or price <= 0 or notional_usd <= 0:
        return 0.0
    return floor_to_decimals(notional_usd / price, int(sz_decimals or 0))


def round_price(px: float, sz_decimals: int) -> float:
    """HL perp price rules: <= 5 significant figures and <= (6 - szDecimals) decimals."""
    if not px or px <= 0:
        return px
    px = float(f"{px:.5g}")
    return round(px, max(0, 6 - int(sz_decimals or 0)))


def margin_cap_ok(margin_used: float, new_margin: float, equity: float) -> Tuple[bool, float]:
    """Return (ok, utilization_pct) for total margin after adding new_margin."""
    if equity <= 0:
        return False, 100.0
    util = (margin_used + new_margin) / equity * 100.0
    return util <= MAX_MARGIN_UTILIZATION_PCT + 1e-9, util


# ---------------------------------------------------------------- freshness
def parse_ts(ts: Any) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def hkt_date(dt: datetime) -> str:
    return dt.astimezone(HKT).strftime("%Y-%m-%d")


def candidates_fresh(candidates_data: dict, now: Optional[datetime] = None,
                     max_age_h: Optional[float] = None) -> Tuple[bool, str]:
    """Executor guard: candidates must be built today (HKT), recently, and not flagged stale."""
    now = now or datetime.now(timezone.utc)
    if max_age_h is None:
        try:
            max_age_h = float(os.environ.get("EXEC_MAX_CANDIDATE_AGE_H") or DEFAULT_MAX_CANDIDATE_AGE_H)
        except ValueError:
            max_age_h = DEFAULT_MAX_CANDIDATE_AGE_H
    if not candidates_data:
        return False, "entry_candidates_latest.json missing/empty"
    gen = parse_ts(candidates_data.get("generated_at"))
    if not gen:
        return False, "candidates have no generated_at"
    if hkt_date(gen) != hkt_date(now):
        return False, f"candidates from {hkt_date(gen)} HKT, not today {hkt_date(now)}"
    age_h = (now - gen).total_seconds() / 3600.0
    if age_h > max_age_h:
        return False, f"candidates {age_h:.1f}h old > {max_age_h}h"
    if age_h < -0.1:
        return False, "candidates generated_at is in the future"
    if candidates_data.get("stale"):
        return False, "candidates flagged stale (radar too old)"
    return True, "ok"


# ---------------------------------------------------------------- misc
def sig(x: Any, n: int = 6) -> Any:
    """Round floats to n significant figures (compact logs); pass through others."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return x
    if isinstance(x, int):
        return x
    if x == 0 or not math.isfinite(x):
        return 0 if x == 0 else None
    return float(f"{x:.{n}g}")


def _f(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
