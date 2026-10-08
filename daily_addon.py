#!/usr/bin/env python3
"""Daily ADD_ON top-up, Signum-style (MMT 2026-10-07, updated 2026-10-08). Replaces the CONT / ADD_ON pullback pendings.

There is no CONT any more: a coin you do not hold enters only through the Base (fresh daily cross).
For a coin you ALREADY HOLD LONG, once a day (08:57 HKT, after the 08:55 executor) this adds to the winner when
  1. the latest CLOSED 1D bar's close is still above the 1D Upper (Signum: "close still above the upper band"),
  2. the position's ROE (unrealized PnL / margin) is >= +20%,
  3. the coin's total margin (base + add) stays <= 5.5% NAV,
  4. the position has NOT been topped up before (ONCE PER POSITION, not once per day - tracked by symbol + entry).
The add uses the position's existing isolated leverage and 2% NAV margin (the SoT floor; no desk decision is needed,
the rule is mechanical like Signum). The same fail-closed checks as every entry apply: radar row-count + freshness,
liquidation beyond the Hard SL for ALL open positions, Hard SL per tier, SL distance >= 1.5%, price sanity, minimum
order, total margin <= 80% NAV, coin notional cap. LIVE order = hl_exec.enter_long_with_sl (IOC + reduce-only Hard SL).

OFF by default: set DAILY_ADDON_ENABLED=1 to run it. DRY_RUN unless EXEC_DRY_RUN=0 and HL_API_PRIVATE_KEY is set
(exec_common.is_live_mode): DRY_RUN only reports what it would do. Prints one JSON object; exit 0 ok, 1 error.
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

from exec_common import (  # noqa: E402
    HKT, MAX_MARGIN_UTILIZATION_PCT, MAX_TOTAL_MARGIN_NAV_PCT, MIN_SL_DIST_PCT, SOT_ID, addon_gates,
    build_run_report, coin_notional_ok, hard_sl_for_tier, is_live_mode, margin_cap_ok, min_order_usd,
    order_qty, parse_ts, price_sane, radar_ref_price, radar_rowcount_ok, round_price, size_by_margin,
    total_margin_nav_ok, MAX_COIN_NOTIONAL_NAV_PCT,
)

ENABLE_ENV = "DAILY_ADDON_ENABLED"
TOPUP_MARGIN_PCT = 2.0          # SoT floor: 2% NAV isolated margin per top-up
MIN_ROE_PCT = 20.0              # MMT 2026-10-08: ROE >= +20% to trigger top-up
MAX_TOTAL_COIN_MARGIN_PCT = 5.5 # MMT 2026-10-08: total coin margin cap (base + add)
RADAR_MAX_AGE_H = {"1d": 26.0, "4h": 5.0}


def _log(msg: str) -> None:
    sys.stderr.write(f"[DAILY_ADDON] {msg}\n")
    sys.stderr.flush()


def enabled() -> bool:
    return str(os.environ.get(ENABLE_ENV, "0")).strip().lower() in ("1", "true", "yes", "on")


def _load_json(name: str) -> dict:
    try:
        return json.loads((ROOT / "out" / name).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _state_path() -> Path:
    return Path(os.environ.get("DAILY_ADDON_STATE") or (ROOT / "out" / "daily_addon_state.json"))


def _is_position_done(sym: str) -> bool:
    """Check if this symbol's current position has already been topped up.
    A position is defined as holding a LONG in a symbol; it persists until fully closed."""
    try:
        doc = json.loads(_state_path().read_text(encoding="utf-8"))
        return sym in doc.get("positions", {})
    except Exception:  # noqa: BLE001
        return False


def _mark_position_done(sym: str) -> None:
    """Mark this symbol's position as topped up. State persists until position closes."""
    p = _state_path()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        doc = {}
    
    positions = doc.get("positions", {})
    positions[sym] = {"topped_up_at": datetime.now(timezone.utc).isoformat()}
    doc["positions"] = positions
    
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    os.replace(tmp, p)


def cleanup_closed_positions(open_symbols: set) -> None:
    """Remove state for positions that are no longer open (cleanup on each run)."""
    p = _state_path()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        positions = doc.get("positions", {})
        # Keep only positions for symbols still open
        cleaned = {sym: v for sym, v in positions.items() if sym in open_symbols}
        if len(cleaned) < len(positions):
            doc["positions"] = cleaned
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc), encoding="utf-8")
            os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        pass


def _age_h(radar: dict, now: datetime) -> Optional[float]:
    ts = parse_ts(radar.get("ts"))
    return (now - ts).total_seconds() / 3600.0 if ts else None


def topup_signal(row_1d: Optional[dict]) -> (bool, str):
    """Rule 1 (pure): the latest CLOSED 1D bar is still above the 1D Upper."""
    if not row_1d:
        return False, "no 1D radar row"
    close, upper = row_1d.get("close"), row_1d.get("upper")
    try:
        close, upper = float(close), float(upper)
    except (TypeError, ValueError):
        return False, "1D close/upper missing"
    if close > upper:
        return True, f"1D close {close:.6g} above 1D Upper {upper:.6g}"
    return False, f"1D close {close:.6g} not above 1D Upper {upper:.6g}"


def run_daily_addons(hl: Any = None, radar_1d: Optional[dict] = None, radar_4h: Optional[dict] = None,
                     now: Optional[datetime] = None, log_entry_fn: Any = None,
                     tier_fn: Any = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    live = is_live_mode()
    mode = "LIVE" if live else "DRY_RUN"
    res: Dict[str, Any] = {"mode": mode, "status": "success", "timestamp": now.isoformat(), "sot": SOT_ID,
                           "checked": [], "filled": [], "skipped": [], "alerts": []}
    if not enabled():
        res["message"] = f"daily ADD_ON top-up is OFF ({ENABLE_ENV}=1 enables it)"
        return res
    day = now.astimezone(HKT).strftime("%Y-%m-%d")
    radar_1d = _load_json("gc_radar_1d.json") if radar_1d is None else radar_1d
    radar_4h = _load_json("gc_radar_4h.json") if radar_4h is None else radar_4h
    for tf, rd in (("1d", radar_1d), ("4h", radar_4h)):
        ok_rc, why_rc = radar_rowcount_ok(rd, tf)
        age = _age_h(rd, now)
        if not ok_rc or age is None or age > RADAR_MAX_AGE_H[tf]:
            why = why_rc if not ok_rc else f"{tf} radar stale (age={age})"
            res.update(status="fail_closed", message=why)
            res["alerts"].append(why)
            _log(f"{mode} fail_closed: {why}")
            return res
    rows_1d = {r.get("symbol"): r for r in radar_1d.get("rows", []) or []}
    rows_4h = {r.get("symbol"): r for r in radar_4h.get("rows", []) or []}

    if hl is None:
        from hl_exec import HLClient
        hl = HLClient(os.environ.get("HL_ADDRESS") or None)
    try:
        from executor import _check_all_positions_liq_safe, _parse_account, _user_abstraction
        perp_raw = hl.perp_state()
        account = _parse_account(hl.spot_state(), perp_raw, _user_abstraction(hl))
        meta, mids = hl.meta(), hl.all_mids()
    except Exception as e:  # noqa: BLE001
        res.update(status="error", message=f"HL state failed: {e}")
        return res
    equity, cum_margin = account["equity"], account["margin_used"]
    res["nav_snapshot"] = account["nav"]
    if equity <= 0:
        res.update(status="error", message="Equity is 0 / unavailable")
        return res
    liq_safe, unsafe = _check_all_positions_liq_safe(account["positions"], {}, radar_4h)
    if not liq_safe:
        res.update(status="fail_closed", message=f"Unsafe liquidation prices for: {', '.join(unsafe)}")
        return res
    lev_by_coin: Dict[str, int] = {}
    for g in perp_raw.get("assetPositions", []) or []:
        p = g.get("position") or {}
        try:
            lev_by_coin[p.get("coin")] = int((p.get("leverage") or {}).get("value") or 0)
        except (TypeError, ValueError):
            pass
    if tier_fn is None:
        from mcap_tiers import tier_for as tier_fn  # noqa: N813
    if log_entry_fn is None:
        from trade_log import log_entry as log_entry_fn  # noqa: N813
    try:
        slip = max(0.0, min(2.0, float(os.environ.get("EXEC_ENTRY_SLIPPAGE_PCT") or 0.5)))
    except ValueError:
        slip = 0.5
    
    # Cleanup state for closed positions
    open_symbols = {p["coin"] for p in account["positions"] if p.get("side") == "LONG"}
    cleanup_closed_positions(open_symbols)

    for pos in account["positions"]:
        if pos.get("side") != "LONG":
            continue
        sym = pos["coin"]
        
        chk = {"symbol": sym}
        res["checked"].append(chk)

        def skip(why: str, **kw: Any) -> None:
            chk["result"] = "skipped"
            chk["reason"] = why
            res["skipped"].append({"symbol": sym, "reason": why, **kw})

        # Check if this position has already been topped up (once per position lifetime)
        if _is_position_done(sym):
            skip("position already topped up")
            continue
        ok_sig, why_sig = topup_signal(rows_1d.get(sym))
        chk["signal"] = why_sig
        if not ok_sig:
            skip(why_sig)
            continue
        
        # Check ROE >= +20% (unrealized PnL / margin)
        try:
            upnl = float(pos.get("unrealized_pnl") or pos.get("unrealizedPnl") or 0)
            margin = float(pos.get("margin_used") or pos.get("marginUsed") or 0)
            roe_pct = (upnl / margin * 100.0) if margin > 0 else 0
        except (TypeError, ValueError, ZeroDivisionError):
            skip("ROE calculation failed")
            continue
        if roe_pct < MIN_ROE_PCT - 1e-9:
            skip(f"ROE {roe_pct:+.1f}% < +{MIN_ROE_PCT:g}% required")
            continue
        chk["roe_pct"] = round(roe_pct, 2)
        
        try:  # fresh live mid right before deciding
            mid = (hl.all_mids() or mids).get(sym)
        except Exception:  # noqa: BLE001
            mid = mids.get(sym)
        fixed_lev = lev_by_coin.get(sym) or pos.get("leverage")
        if not fixed_lev:
            skip("existing position leverage unknown")
            continue
        
        # Check total coin margin cap (5.5% NAV instead of addon_gates' 20% notional)
        try:
            current_margin = float(pos.get("marginUsed") or 0)
            current_margin_pct = (current_margin / equity * 100.0) if equity > 0 else 0
            room_margin_pct = MAX_TOTAL_COIN_MARGIN_PCT - current_margin_pct
            if room_margin_pct < TOPUP_MARGIN_PCT - 1e-9:
                skip(f"total coin margin {current_margin_pct:.2f}% + {TOPUP_MARGIN_PCT:g}% > {MAX_TOTAL_COIN_MARGIN_PCT:g}% cap")
                continue
        except (TypeError, ValueError):
            skip("margin calculation failed")
            continue
        
        ok_add, why_add, room = addon_gates(pos, equity, mid, fixed_lev)
        if not ok_add:
            skip(why_add)
            continue
        cm = meta.get(sym)
        if not cm:
            skip("coin not in HL meta")
            continue
        sz_dec, coin_max = int(cm.get("szDecimals", 0)), cm.get("maxLeverage")
        tier = tier_fn(sym) or "unknown"
        hard_sl, sl_label = hard_sl_for_tier(tier, rows_4h.get(sym))
        if not hard_sl or hard_sl <= 0:
            skip(f"no Hard SL level ({sl_label})")
            continue
        ok_px, _d, why_px = price_sane(mid, radar_ref_price(rows_4h.get(sym), rows_1d.get(sym)))
        if not ok_px:
            skip(why_px)
            continue
        sl_dist = (mid - hard_sl) / mid * 100.0
        if sl_dist < MIN_SL_DIST_PCT:
            skip(f"SL distance {sl_dist:.2f}% to {sl_label} {hard_sl:.6g} < {MIN_SL_DIST_PCT}%")
            continue
        limit_px = round_price(mid * (1 + slip / 100.0), sz_dec)
        ref_px = max(limit_px, pos.get("entry_px") or 0)
        sz = size_by_margin(equity, limit_px, hard_sl, coin_max, TOPUP_MARGIN_PCT, fixed_lev,
                            fixed_leverage=fixed_lev, max_margin_pct=room, liq_ref_px=ref_px, tier=tier)
        if not sz["ok"]:
            skip(sz["reason"])
            continue
        size_pct, leverage = sz["margin_pct"], sz["leverage"]
        qty = order_qty(equity * size_pct / 100.0 * leverage, limit_px, sz_dec)
        notional = qty * mid
        if notional < min_order_usd(equity):
            skip(f"notional ${notional:.2f} < minimum ${min_order_usd(equity):.2f}")
            continue
        margin_usd = notional / leverage
        ok_cap, util = margin_cap_ok(cum_margin, margin_usd, equity)
        if not ok_cap:
            skip(f"margin utilization {util:.1f}% > {MAX_MARGIN_UTILIZATION_PCT}%")
            continue
        ok_tot, tot_pct = total_margin_nav_ok(cum_margin, margin_usd, equity)
        if not ok_tot:
            skip(f"total margin {tot_pct:.1f}% NAV > {MAX_TOTAL_MARGIN_NAV_PCT:g}% cap")
            continue
        ok_coin, coin_pct = coin_notional_ok((pos.get("size") or 0.0) * mid, notional, equity)
        if not ok_coin:
            skip(f"coin notional {coin_pct:.1f}% NAV > {MAX_COIN_NOTIONAL_NAV_PCT:g}% cap")
            continue
        intent = {"symbol": sym, "kind": "ADD_ON", "rule": "signum_topup", "mid": mid, "limit_px": limit_px,
                  "qty": qty, "size_pct": size_pct, "leverage": leverage, "notional_usd": round(notional, 2),
                  "margin_usd": round(margin_usd, 2), "hard_sl": hard_sl, "hard_sl_label": sl_label,
                  "sl_dist_pct": round(sl_dist, 3), "estimated_liq": sz["liq"], "why": f"{why_sig}; {why_add}"}
        if not live:
            cum_margin += margin_usd
            chk["result"] = "would_fill"
            res["filled"].append({**intent, "dry_run": True})
            _log(f"DRY_RUN would top up {sym}: qty={qty} limit={limit_px} {leverage}x | {why_sig}; {why_add}")
            continue
        from hl_exec import enter_long_with_sl
        _log(f"LIVE ADD_ON {sym}: BUY qty={qty} IOC limit={limit_px} {leverage}x, Hard SL {hard_sl}")
        r = enter_long_with_sl(hl, sym, qty, limit_px, leverage, hard_sl, sz_dec, coin_max_leverage=coin_max, now=now)
        intent["live_result"] = {k: v for k, v in r.items() if k != "raw"}
        st = r.get("status")
        if st == "executed":
            cum_margin += (r.get("filled_sz") or qty) * (r.get("avg_px") or limit_px) / leverage
            _mark_position_done(sym)
            chk["result"] = "filled"
            res["filled"].append(intent)
            log_entry_fn(trade_id=f"{sym}_ADD_ON_{now.strftime('%Y%m%d_%H%M%S')}", symbol=sym, entry_type="ADD_ON",
                         tier=tier, trend_1d="", trend_4h="", sl_dist_pct=sl_dist,
                         entry_price=r.get("avg_px") or limit_px, entry_size=r.get("filled_sz") or qty,
                         entry_leverage=leverage, ai_decision_reason="signum top-up", dry_run=False)
        else:
            skip(f"live top-up {st}", live_result=intent["live_result"])
            res["alerts"].append(f"{sym} ADD_ON: {st}")
            if st and (st.startswith("sl_failed") or st.startswith("reconcile_failed") or st in ("leverage_failed", "entry_failed")):
                res["status"] = "error"
                res["message"] = "; ".join(res["alerts"])
    try:
        res["run_report"] = build_run_report("daily_addon", res, res.get("nav_snapshot"), {})
    except Exception as e:  # noqa: BLE001
        res["run_report"] = {"sot": SOT_ID, "run": "daily_addon", "error": str(e)}
    return res


if __name__ == "__main__":
    out = run_daily_addons()
    print(json.dumps(out, indent=2, default=str))
    sys.exit(0 if out["status"] in ("success", "fail_closed") else 1)
