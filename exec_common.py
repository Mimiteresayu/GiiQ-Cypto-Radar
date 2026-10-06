#!/usr/bin/env python3
"""Shared, side-effect-free execution rules (SoT) for executor / exit_worker / serve.

Everything here is pure (no network) so it can be unit tested directly.

SoT (do not change without MMT):
- Mega/Large: primary exit = 4H close < 4H Filter; Hard SL = 4H Lower
- Small/Tiny: primary exit = 1H close < 1H Lower; Hard SL = 4H Filter (mid)
- Leverage (GIIQ-SoT-2): 3-5x isolated chosen by size_by_margin (never above the coin HL maxLeverage), min notional $10,
  total margin <= 80% equity, liquidation must lie beyond the Hard SL.
- GIIQ-SoT-4 (MMT 2026-10-03): total isolated margin <= 70% NAV (80% stays the outer hard cap);
  Tiny tier (incl. unknown mcap) max 3x leverage and max 2% NAV margin per trade.

Sizing convention: ``size_pct`` = MARGIN as % of equity; notional = margin x leverage.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------- constants
# SoT version id. Bump (GIIQ-SoT-2, ...) whenever an executor rule changes and add an entry to
# docs/SOT_CHANGELOG.md. Shown in the cockpit header, executor/pending run reports and DESK_DATA.
SOT_ID = "GIIQ-SoT-4"

MIN_NOTIONAL_USD = 10.0  # Hyperliquid minimum order value (USD)
MIN_LEVERAGE = 1.0
MAX_LEVERAGE = 5.0
MIN_SL_DIST_PCT = 1.5
MAX_MARGIN_UTILIZATION_PCT = 80.0
DEFAULT_MAX_CANDIDATE_AGE_H = 3.0  # 08:05 build -> 08:55 execute (+ slack)
HKT = timezone(timedelta(hours=8))

# ---------------------------------------------------------------- guardrails (GIIQ-SoT-1)
# Radar row-count check. Closed-bar scans on 2026-09-28 had 173 (1D) / 176 (4H) / 177 (1H)
# rows = the whole HL liquid universe (dayNtlVlm >= $75k, ~160-180 names). A scan is treated
# as broken (fail-closed, no entries) when it has fewer than RADAR_MIN_ROWS rows (~70% of the
# normal count) OR fewer than RADAR_MIN_UNIVERSE_FRAC of the symbols it asked for
# (payload `universe_requested`), i.e. >15% of the requested coins failed to load.
RADAR_MIN_ROWS = 120
RADAR_MIN_UNIVERSE_FRAC = 0.85
# Price sanity (ticker collision / bad data): skip a coin if the HL live mid differs from the
# radar price by more than this (radar price = latest closed 4H close, fallback 1D close).
PRICE_SANITY_MAX_DIFF_PCT = 50.0
# Minimum order: notional must be >= max(HL minimum $10, 1% of the run's NAV snapshot).
MIN_ORDER_NAV_PCT = 1.0

# ---------------------------------------------------------------- GIIQ-SoT-2 sizing (MMT 2026-09-28)
# Per-trade risk = the isolated margin itself: 2-4% NAV per coin (hard cap 4%). NOT sized by SL
# distance (SL/exits are dynamic, tier-based). Leverage 3-5x isolated; the isolated liquidation
# price must sit beyond (below, for a LONG) the Hard SL - step leverage down toward 3x, skip if
# impossible at 3x. Existing 80% total margin cap unchanged. ADD_ON keeps the existing leverage.
# AI size/leverage are maximums inside these bands.
SOT2_MIN_LEV = 3
SOT2_MAX_LEV = 5
SOT2_MIN_MARGIN_PCT = 2.0
SOT2_MAX_MARGIN_PCT = 4.0
# ---------------------------------------------------------------- GIIQ-SoT-3 (MMT 2026-09-28)
# Portfolio caps on top of the per-trade 2-4% margin risk (alts move with BTC, so cap the total):
# MMT 2026-10-03 13:35-13:45 HKT (AIQ-0022): total cap raised 30% -> 70% NAV. The 80% margin
# utilization cap (MAX_MARGIN_UTILIZATION_PCT) stays as the outermost hard cap; 20% coin cap unchanged.
MAX_TOTAL_MARGIN_NAV_PCT = 70.0   # all isolated margin (existing + new, cumulative) <= 70% NAV
MAX_COIN_NOTIONAL_NAV_PCT = 20.0  # one coin's notional (existing + add) <= 20% NAV (also after ADD_ON)
MAX_NEW_ENTRIES_PER_DAY = 3       # new fills per HKT day: Base + CONTINUATION + ADD_ON together
# ADD_ON: the base position must be a real winner in PRICE terms (Signum 1x meaning), not leveraged ROE:
# at 3-5x, +10% ROE is only a +2-3.3% move.
ADDON_MIN_PRICE_GAIN_PCT = 10.0
# Fallback decisions (Harbor 08:40, only when Claude's POST never arrived): Base only, 2% margin.
FALLBACK_MARGIN_PCT = 2.0
# Tiny tier limits (MMT 2026-10-03 13:35-13:45 HKT, AIQ-0022): leverage <= 3x, margin <= 2% NAV per
# trade. Tiny = mcap < $200M or unknown (mcap_tiers.tier_for), so any tier other than
# mega/large/small (incl. "unknown" / empty) is treated as Tiny (fail-safe).
TINY_MAX_LEV = 3
TINY_MAX_MARGIN_PCT = 2.0
NON_TINY_TIERS = ("mega", "large", "small")


def is_tiny_tier(tier: Optional[str]) -> bool:
    """True for Tiny and for any unknown / empty tier (unknown mcap = Tiny)."""
    return (tier or "").strip().lower() not in NON_TINY_TIERS


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


# ---------------------------------------------------------------- guardrails
def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


def radar_rowcount_ok(radar: Optional[dict], tf: str = "") -> Tuple[bool, str]:
    """Fail-closed row-count check for a closed-bar radar payload (see RADAR_MIN_ROWS).

    Thresholds can be overridden with EXEC_RADAR_MIN_ROWS / EXEC_RADAR_MIN_UNIVERSE_FRAC."""
    min_rows = int(_env_float("EXEC_RADAR_MIN_ROWS", RADAR_MIN_ROWS))
    frac = _env_float("EXEC_RADAR_MIN_UNIVERSE_FRAC", RADAR_MIN_UNIVERSE_FRAC)
    label = f"{tf.upper()} radar" if tf else "radar"
    if min_rows <= 0:  # EXEC_RADAR_MIN_ROWS=0 disables the check (unit tests with tiny fixtures only)
        return True, f"{label} row-count check disabled (EXEC_RADAR_MIN_ROWS=0)"
    if not isinstance(radar, dict) or not radar:
        return False, f"{label} missing"
    rows = radar.get("rows")
    n = len(rows) if isinstance(rows, list) else 0
    uni = radar.get("universe_requested")
    n_uni = len(uni) if isinstance(uni, list) else 0
    if n < min_rows:
        return False, f"{label} has {n} rows < minimum {min_rows} (normal ~170+) - scan looks broken"
    if n_uni and n < math.ceil(frac * n_uni):
        return False, (f"{label} has {n} rows < {frac:.0%} of the {n_uni} requested symbols "
                       f"- scan looks broken")
    return True, f"{label} {n} rows" + (f" / {n_uni} requested" if n_uni else "") + f" (min {min_rows})"


def radar_ref_price(row_4h: Optional[dict], row_1d: Optional[dict] = None) -> Optional[float]:
    """Radar price for the price-sanity check: latest closed 4H close, else latest closed 1D close."""
    for row in (row_4h, row_1d):
        v = _f((row or {}).get("close"))
        if v and v > 0:
            return v
    return None


def price_sane(mid: Optional[float], radar_px: Optional[float]) -> Tuple[bool, Optional[float], str]:
    """(ok, diff_pct, detail). Fail-closed: both prices required, |mid/radar - 1| <= 50%."""
    if not mid or mid <= 0:
        return False, None, "no HL live mid"
    if not radar_px or radar_px <= 0:
        return False, None, "no radar price for price-sanity check"
    diff = abs(mid - radar_px) / radar_px * 100.0
    lim = _env_float("EXEC_PRICE_SANITY_MAX_DIFF_PCT", PRICE_SANITY_MAX_DIFF_PCT)
    if diff > lim:
        return False, diff, (f"price sanity: HL mid {mid:.6g} vs radar {radar_px:.6g} differ {diff:.1f}% > "
                             f"{lim:g}% (ticker collision / bad data)")
    return True, diff, f"price ok ({diff:.1f}% vs radar)"


def min_order_usd(nav: float) -> float:
    """Minimum entry notional = max(HL minimum $10, 1% of the NAV snapshot)."""
    return max(MIN_NOTIONAL_USD, max(0.0, float(nav or 0.0)) * MIN_ORDER_NAV_PCT / 100.0)


def _usdc_row(spot: dict) -> dict:
    for bal in (spot or {}).get("balances", []) or []:
        if bal.get("coin") == "USDC":
            return bal
    return {}


def nav_snapshot(spot: dict, perp: dict, abstraction: Optional[str] = None,
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    """NAV used for ALL sizing in one run (taken once per run, never refreshed mid-run).

    NAV definition (GIIQ-SoT-1):
    - Unified account (HL userAbstraction == "unifiedAccount", our main wallet): NAV = spot USDC
      `total`. In unified mode the perp collateral (perp marginSummary.accountValue, incl. the
      isolated margin + uPnL of open positions) is already inside spot USDC as `hold`, so adding
      accountValue again would double count.
    - Standard (split) account: NAV = perp marginSummary.accountValue + spot USDC total.
    - Unknown mode (abstraction lookup failed): NAV = max(spot USDC total, perp accountValue)
      - never double counts, equals the unified value for our wallet.
    Non-USDC spot tokens are not counted (the account holds none)."""
    usdc = _usdc_row(spot)
    spot_total = _f(usdc.get("total")) or 0.0
    spot_hold = _f(usdc.get("hold")) or 0.0
    ms = (perp or {}).get("marginSummary") or {}
    perp_av = _f(ms.get("accountValue")) or 0.0
    margin_used = _f(ms.get("totalMarginUsed")) or 0.0
    ab = (abstraction or "").strip()
    if ab == "unifiedAccount":
        nav, source = spot_total, "unified: spot USDC total (perp accountValue already included as hold)"
    elif ab and ab not in ("unifiedAccount", "unknown"):
        nav, source = perp_av + spot_total, f"{ab}: perp accountValue + spot USDC total"
    else:
        nav, source = max(spot_total, perp_av), "abstraction unknown: max(spot USDC total, perp accountValue)"
    return {"nav": round(nav, 6), "source": source, "abstraction": ab or "unknown",
            "spot_usdc_total": spot_total, "spot_usdc_hold": spot_hold, "perp_account_value": perp_av,
            "margin_used": margin_used, "ts": (now or datetime.now(timezone.utc)).isoformat()}


def build_run_report(run: str, result: Dict[str, Any], nav: Optional[dict] = None,
                     approved: Optional[Dict[str, dict]] = None) -> Dict[str, Any]:
    """Compact per-run report: executed / skipped / downsized / failed with reasons.

    - executed: orders filled (LIVE) or that WOULD be placed (DRY_RUN, `dry_run`: true)
    - skipped:  guard skips (no order attempted) with reason
    - failed:   LIVE order attempted but not executed (no fill, leverage/entry/SL failure)
    - downsized: executed/would-execute entries whose final size % or leverage is below the
                 AI-approved value (SoT band / coin maxLeverage clamp / BTC-bearish fixed size)"""
    approved = approved or {}
    rep: Dict[str, Any] = {"sot": SOT_ID, "run": run, "mode": result.get("mode"),
                           "status": result.get("status"), "message": result.get("message"),
                           "ts": result.get("timestamp"),
                           "nav": (nav or {}).get("nav"), "nav_source": (nav or {}).get("source"),
                           "executed": [], "skipped": [], "downsized": [], "failed": []}
    if result.get("sequence"):
        rep["sequence"] = result["sequence"]

    def _ex(x: dict, dry: bool) -> dict:
        lr = x.get("live_result") or {}
        return {"symbol": x.get("symbol"), "kind": x.get("kind") or x.get("entry_type"),
                "qty": lr.get("filled_sz") or x.get("qty"), "px": lr.get("avg_px") or x.get("limit_px"),
                "size_pct": x.get("size_pct"), "leverage": x.get("leverage"),
                "notional_usd": x.get("notional_usd"), "hard_sl": x.get("hard_sl"), "dry_run": dry,
                "mid": x.get("mid"), "zone": x.get("zone"), "upper_ref": x.get("entry_upper_ref")}

    done = [(x, False) for x in result.get("executed", []) or []]
    done += [(x, True) for x in result.get("actions", []) or []]
    done += [(x, bool(x.get("dry_run"))) for x in result.get("filled", []) or []]
    for x, dry in done:
        rep["executed"].append(_ex(x, dry))
        a = approved.get(x.get("symbol")) or {}
        a_sz, a_lev = _f(a.get("size_pct")), _f(a.get("leverage"))
        why = []
        if a_sz is not None and x.get("size_pct") is not None and float(x["size_pct"]) < a_sz - 1e-9:
            why.append(f"size {a_sz:g}% -> {float(x['size_pct']):g}%")
        if a_lev is not None and x.get("leverage") is not None and float(x["leverage"]) < a_lev - 1e-9:
            why.append(f"leverage {a_lev:g}x -> {float(x['leverage']):g}x")
        if why:
            rep["downsized"].append({"symbol": x.get("symbol"), "reason": "; ".join(why) + " (SoT clamp)"})
    for s in result.get("skipped", []) or []:
        item = {"symbol": s.get("symbol"), "reason": s.get("reason")}
        (rep["failed"] if s.get("live_result") else rep["skipped"]).append(item)
    for c in result.get("checked", []) or []:  # pending worker: waits / cancels are "skipped" today
        if c.get("result") in ("filled", "would_fill"):
            continue
        rep["skipped"].append({"symbol": c.get("symbol"), "kind": c.get("kind"),
                               "reason": f"{c.get('result') or c.get('action')}: {c.get('reason')}"})
    for p in result.get("pending", []) or []:
        rep.setdefault("pending_created", []).append(
            {"symbol": p.get("symbol"), "kind": p.get("kind"), "zone": [p.get("zone_lower"), p.get("zone_filter")],
             "band_tf": p.get("band_tf"), "stored": bool(p.get("id")), "created": p.get("created")})
    # pending disabled: Chase approvals acknowledged with no entry - not counted as skips
    for n in result.get("no_entry", []) or []:
        rep.setdefault("no_entry", []).append({"symbol": n.get("symbol"), "reason": n.get("reason")})
    if result.get("pending_disabled"):
        rep["pending_disabled"] = result["pending_disabled"]
    if result.get("pending_cancelled"):
        rep["pending_cancelled"] = list(result["pending_cancelled"])
    for a in result.get("alerts", []) or []:
        rep.setdefault("alerts", []).append(str(a))
    if result.get("status") in ("fail_closed", "error") and not (rep["executed"] or rep["skipped"] or rep["failed"]):
        for sym in sorted(approved):
            rep["skipped"].append({"symbol": sym, "reason": f"run {result.get('status')}: {result.get('message')}"})
    return rep


# ---------------------------------------------------------------- GIIQ-SoT-2 sizing
def size_by_margin(nav: float, entry_px: float, hard_sl: float, coin_max_leverage: Optional[float],
                   ai_size_pct: Any = None, ai_leverage: Any = None, fixed_leverage: Optional[int] = None,
                   max_margin_pct: Optional[float] = None, liq_ref_px: Optional[float] = None,
                   tier: Optional[str] = None) -> Dict[str, Any]:
    """GIIQ-SoT-2: choose (leverage, margin %) for a LONG entry. Risk = the isolated margin.

    - margin = AI size clamped to 2-4% NAV (AI value = maximum; missing -> the 4% cap; an AI value
      below 2% is lifted to the 2% floor). `max_margin_pct` can lower the cap (ADD_ON 5.5% room);
      room < 2% -> skip.
    - leverage: from min(5x, AI leverage [floored at 3x], coin maxLeverage) down to 3x, the first
      whose isolated liq (from the worst-case entry / liq_ref_px) is strictly below the Hard SL.
      fixed_leverage (ADD_ON = existing position leverage) is not stepped. Impossible -> skip.
    - tier (MMT 2026-10-03, AIQ-0022): when given and Tiny (or unknown), leverage is capped at
      TINY_MAX_LEV (3x) and margin at TINY_MAX_MARGIN_PCT (2% NAV); a Tiny ADD_ON whose existing
      leverage is above 3x is refused. tier=None keeps the old (tier-blind) behaviour.
    Returns {"ok", "leverage", "margin_pct", "notional_usd", "liq", "reason", "notes"}."""
    notes: list = []
    if not nav or nav <= 0 or not entry_px or entry_px <= 0 or not hard_sl or hard_sl <= 0 or hard_sl >= entry_px:
        return {"ok": False, "reason": "sizing inputs invalid (NAV / entry / Hard SL)", "notes": notes}
    m = SOT2_MAX_MARGIN_PCT
    a_sz = _f(ai_size_pct)
    if a_sz is not None and a_sz > 0:
        if a_sz < SOT2_MIN_MARGIN_PCT:
            notes.append(f"AI size {a_sz:g}% < {SOT2_MIN_MARGIN_PCT:g}% floor -> {SOT2_MIN_MARGIN_PCT:g}%")
        m = min(m, max(SOT2_MIN_MARGIN_PCT, a_sz))
    if max_margin_pct is not None:
        m = min(m, max_margin_pct)
    tiny = tier is not None and is_tiny_tier(tier)
    if tiny and m > TINY_MAX_MARGIN_PCT:
        notes.append(f"Tiny tier: margin {m:g}% -> {TINY_MAX_MARGIN_PCT:g}% NAV cap")
        m = TINY_MAX_MARGIN_PCT
    if m < SOT2_MIN_MARGIN_PCT - 1e-9:
        return {"ok": False, "reason": f"margin room {m:.2f}% < {SOT2_MIN_MARGIN_PCT:g}% minimum", "notes": notes}
    coin_cap = int(math.floor(float(coin_max_leverage))) if coin_max_leverage and coin_max_leverage > 0 else SOT2_MAX_LEV
    if fixed_leverage:
        if tiny and int(fixed_leverage) > TINY_MAX_LEV:
            return {"ok": False, "reason": (f"Tiny tier: existing leverage {int(fixed_leverage)}x > "
                                            f"{TINY_MAX_LEV}x cap"), "notes": notes}
        levs = [int(fixed_leverage)]
    else:
        a_lev = _f(ai_leverage)
        l_max = SOT2_MAX_LEV
        if a_lev is not None and a_lev > 0:
            if a_lev < SOT2_MIN_LEV:
                notes.append(f"AI leverage {a_lev:g}x < {SOT2_MIN_LEV}x floor -> {SOT2_MIN_LEV}x")
            l_max = min(l_max, max(SOT2_MIN_LEV, int(math.floor(a_lev + 1e-9))))
        l_max = min(l_max, coin_cap)
        if tiny and l_max > TINY_MAX_LEV:
            notes.append(f"Tiny tier: leverage {l_max}x -> {TINY_MAX_LEV}x cap")
            l_max = TINY_MAX_LEV
        if l_max < SOT2_MIN_LEV:
            return {"ok": False, "reason": f"coin maxLeverage {coin_max_leverage} < {SOT2_MIN_LEV}x minimum", "notes": notes}
        levs = list(range(l_max, SOT2_MIN_LEV - 1, -1))
    fails = []
    for L in levs:
        liq = isolated_liq_price_long(liq_ref_px or entry_px, L, coin_max_leverage)
        if not liq_beyond_sl_long(liq, hard_sl):
            fails.append(f"{L}x liq {liq:.6g} not below Hard SL {hard_sl:.6g}")
            continue
        return {"ok": True, "leverage": L, "margin_pct": round(m, 4), "notional_usd": nav * m / 100.0 * L,
                "liq": liq, "sl_dist_pct": round((entry_px - hard_sl) / entry_px * 100, 3), "notes": notes + fails}
    return {"ok": False, "reason": "SoT-2 leverage impossible: " + "; ".join(fails), "notes": notes}


def addon_gates(pos: Optional[dict], nav: float, mid: Optional[float] = None,
                leverage: Optional[float] = None) -> Tuple[bool, str, float]:
    """ADD_ON (GIIQ-SoT-3): base position PRICE gain >= +10% and the coin's notional after the add
    stays <= 20% NAV. Returns (ok, reason, max_add_margin_pct) where the margin room is expressed at
    the leverage the add will use (ADD_ON keeps the existing leverage)."""
    if not pos:
        return False, "ADD_ON base position not found", 0.0
    entry = _f(pos.get("entry_px"))
    px = _f(mid)
    size = _f(pos.get("size"))
    if not entry or entry <= 0 or not px or px <= 0:
        return False, "ADD_ON: entry price / live mid unknown", 0.0
    gain = (px / entry - 1.0) * 100.0
    if gain < ADDON_MIN_PRICE_GAIN_PCT - 1e-9:
        return False, f"ADD_ON: price gain {gain:+.1f}% < +{ADDON_MIN_PRICE_GAIN_PCT:g}% (vs entry {entry:.6g})", 0.0
    lev = _f(leverage) or _f(pos.get("leverage"))
    if not lev or lev <= 0:
        return False, "ADD_ON: existing position leverage unknown", 0.0
    if not nav or nav <= 0:
        return False, "ADD_ON: NAV unavailable", 0.0
    coin_ntl_pct = (size or 0.0) * px / nav * 100.0
    room_ntl_pct = MAX_COIN_NOTIONAL_NAV_PCT - coin_ntl_pct
    room_margin_pct = min(SOT2_MAX_MARGIN_PCT, room_ntl_pct / lev)
    if room_margin_pct < SOT2_MIN_MARGIN_PCT - 1e-9:
        return False, (f"ADD_ON: coin notional {coin_ntl_pct:.1f}% NAV, room {max(room_ntl_pct, 0):.1f}% "
                       f"< {SOT2_MIN_MARGIN_PCT:g}% margin x {lev:g}x (cap {MAX_COIN_NOTIONAL_NAV_PCT:g}% NAV)"), room_margin_pct
    return True, (f"price gain {gain:+.1f}%, coin notional {coin_ntl_pct:.1f}% NAV "
                  f"(room {room_ntl_pct:.1f}% = {room_margin_pct:.2f}% margin @ {lev:g}x)"), room_margin_pct


def total_margin_nav_ok(margin_used: float, new_margin: float, nav: float) -> Tuple[bool, float]:
    """GIIQ-SoT-4: total isolated margin (existing + new) <= 70% NAV (was 30%). Returns (ok, pct)."""
    if not nav or nav <= 0:
        return False, 100.0
    pct = (margin_used + new_margin) / nav * 100.0
    return pct <= MAX_TOTAL_MARGIN_NAV_PCT + 1e-9, pct


def coin_notional_ok(existing_notional: float, new_notional: float, nav: float) -> Tuple[bool, float]:
    """GIIQ-SoT-3: one coin's notional (existing + new) <= 20% NAV. Returns (ok, pct)."""
    if not nav or nav <= 0:
        return False, 100.0
    pct = (existing_notional + new_notional) / nav * 100.0
    return pct <= MAX_COIN_NOTIONAL_NAV_PCT + 1e-9, pct


def daily_entry_cap_ok(entries_today: int, entries_this_run: int) -> Tuple[bool, str]:
    n = int(entries_today or 0) + int(entries_this_run or 0)
    if n >= MAX_NEW_ENTRIES_PER_DAY:
        return False, f"daily new-entry cap reached ({n}/{MAX_NEW_ENTRIES_PER_DAY} today)"
    return True, f"{n}/{MAX_NEW_ENTRIES_PER_DAY} entries today"


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
