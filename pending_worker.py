#!/usr/bin/env python3
"""Pending CONT / ADD_ON worker — 3-step trigger evaluation + immediate IOC entry (MMT 2026-10-07).

Run in the Railway 4H :10 job (right after the 4H scan + exits).
DISABLED by default (Cove HEALTH FAIL 2026-10-05): the pass only cancels active pendings (LIVE) and
never evaluates or fills, unless PENDING_CONTINUATION_DISABLED=0.

For each ACTIVE pending entry (created by the executor from AI-approved Chase decisions):
  3-step evaluation (see pending_entries.py):
    1. 1D breakout: 1D dual cross up above 1D Upper
    2. 4H retrace: 4H close down to 4H Filter or Lower
    3. 4H breakout: 4H dual cross up above 4H Upper → TRIGGER
  On trigger: IMMEDIATE IOC entry with Hard SL (no resting orders), using the same fail-closed
  checks as the Base executor (hl_exec.enter_long_with_sl).

On trigger the SAME fail-closed SoT checks as the executor run again at fill time:
  radar freshness, all open positions liq beyond tier Hard SL, Hard SL per tier (4H radar),
  SL distance >= 1.5% from live mid, GIIQ-SoT-2 margin 2-4% NAV + 3-5x (liq below Hard SL), min notional,
  total margin <= 70% NAV (and <= 80% utilisation), isolated liq beyond Hard SL. LIVE entry = hl_exec.enter_long_with_sl
  (IOC + reduce-only Hard SL, fill closed if the SL fails).
Guardrails (GIIQ-SoT-1): radar row-count check on 1D + 4H (fail-closed, no state change),
NAV snapshot once per run, price sanity (HL mid vs radar price <= 50%), minimum order
max($10, 1% NAV), run_report (executed/skipped/downsized/failed). In the 4H job this runs only
after the exit worker finished OK (exits -> re-fetch positions here -> entries); serve.py passes
--after-exits <ts>.
Sizing (GIIQ-SoT-2): exec_common.size_by_margin (risk = isolated margin 2-4% NAV, 3-5x isolated,
liq strictly below the Hard SL - step leverage down toward 3x, skip if impossible; total margin
<= 70% NAV (GIIQ-SoT-4; was 30%) and <= 80% utilisation).
ADD_ON keeps the existing position's leverage and additionally needs
base position PRICE gain >= +10% and coin notional after the add <= 20% NAV (GIIQ-SoT-3,
exec_common.addon_gates); total margin <= 70% NAV; max 3 new fills per HKT day; otherwise it stays pending with
the reason in the run report.
DRY_RUN unless EXEC_DRY_RUN=0 AND HL_API_PRIVATE_KEY (exec_common.is_live_mode). In DRY_RUN the
store is not modified. Prints one JSON object; exit 0 ok, 1 error.
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
    SOT_ID,
    build_run_report,
    min_order_usd,
    price_sane,
    radar_ref_price,
    radar_rowcount_ok,
    addon_gates,
    size_by_margin,
    MAX_MARGIN_UTILIZATION_PCT,
    MIN_NOTIONAL_USD,
    MIN_SL_DIST_PCT,
    clamp_leverage,
    hard_sl_for_tier,
    is_live_mode,
    isolated_liq_price_long,
    liq_beyond_sl_long,
    margin_cap_ok,
    coin_notional_ok,
    count_entries_today_for_reporting,
    total_margin_nav_ok,
    MAX_COIN_NOTIONAL_NAV_PCT,
    MAX_TOTAL_MARGIN_NAV_PCT,
    order_qty,
    parse_ts,
    round_price,
)
from pending_entries import ACTIVE, ADD_ON, CONT, band, evaluate, load_pending, save_pending, summary  # noqa: E402
from pending_entries import CONT_STAIRCASE, migrate_cont_to_staircase  # noqa: E402
from pending_entries import cont_staircase_enabled, cont_staircase_mode  # noqa: E402
from pending_entries import DISABLE_ENV, DISABLED_REASON, cancel_active_pending, pending_disabled  # noqa: E402
from pending_entries import cancel_old_style_pending  # noqa: E402

# SoT size bands (margin % of equity). CONT / ADD_ON both use 2-4%.
PENDING_SIZE_BANDS = {"CONT": (2.0, 4.0), "CONTINUATION": (2.0, 4.0), "ADD_ON": (2.0, 4.0)}
# CONT_STAIRCASE uses risk-based sizing: 0.5% NAV risk, margin clamped 2-8% NAV
CONT_STAIRCASE_RISK_PCT = 0.5
CONT_STAIRCASE_LEVERAGE = 3
CONT_STAIRCASE_MIN_MARGIN_PCT = 2.0
CONT_STAIRCASE_MAX_MARGIN_PCT = 8.0
RADAR_MAX_AGE_H = {"4h": 5.0, "1d": 26.0}


def _log(msg: str) -> None:
    sys.stderr.write(f"[PENDING] {msg}\n")
    sys.stderr.flush()


def _load_json(name: str) -> dict:
    try:
        return json.loads((ROOT / "out" / name).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _radar_age_h(radar: dict, now: datetime) -> Optional[float]:
    ts = parse_ts(radar.get("ts"))
    return (now - ts).total_seconds() / 3600.0 if ts else None


def run_pending(hl: Any = None, radar_1d: Optional[dict] = None, radar_4h: Optional[dict] = None,
                now: Optional[datetime] = None, entries: Optional[List[dict]] = None,
                log_entry_fn: Any = None, bar_fn: Any = None, after_exits: Optional[str] = None) -> Dict[str, Any]:
    """One pending pass. Result carries `sot` and `run_report`."""
    res = _run_pending(hl, radar_1d, radar_4h, now, entries, log_entry_fn, bar_fn, after_exits)
    res["sot"] = SOT_ID
    try:
        approved = {c.get("symbol"): {"size_pct": c.get("approved_size_pct"), "leverage": c.get("approved_leverage")}
                    for c in res.get("filled", []) or []}
        res["run_report"] = build_run_report("pending", res, res.get("nav_snapshot"), approved)
    except Exception as e:  # noqa: BLE001
        res["run_report"] = {"sot": SOT_ID, "run": "pending", "error": str(e)}
    return res


def _run_pending(hl: Any = None, radar_1d: Optional[dict] = None, radar_4h: Optional[dict] = None,
                 now: Optional[datetime] = None, entries: Optional[List[dict]] = None,
                 log_entry_fn: Any = None, bar_fn: Any = None, after_exits: Optional[str] = None) -> Dict[str, Any]:
    live = is_live_mode()
    mode = "LIVE" if live else "DRY_RUN"
    now = now or datetime.now(timezone.utc)
    res: Dict[str, Any] = {"mode": mode, "status": "success", "timestamp": now.isoformat(),
                           "checked": [], "filled": [], "cancelled": [], "alerts": [], "pending_active": []}
    if after_exits:
        res["sequence"] = f"exits (done {after_exits}) -> positions re-fetched -> pending entries"
    persist = entries is None
    entries = load_pending() if entries is None else entries
    
    # Cancel old-style (N/N+1) pendings at the start of every 4H worker run
    old_gone = cancel_old_style_pending(entries, now)
    if old_gone and live and persist:
        try:
            save_pending(entries)
            _log(f"{mode} Cancelled {len(old_gone)} old-style (N/N+1) pendings: {[e.get('id') for e in old_gone]}")
        except Exception as e:  # noqa: BLE001
            res.update(status="error", message=f"pending store write failed during old-style cleanup: {e}")
            return res
    
    # Migrate CONT pendings to CONT_STAIRCASE (MMT 2026-10-08, once per pending)
    migrated = migrate_cont_to_staircase(entries, now)
    if migrated and live and persist:
        try:
            save_pending(entries)
            _log(f"{mode} Migrated {len(migrated)} CONT pendings to CONT_STAIRCASE: {[e.get('id') for e in migrated]}")
        except Exception as e:  # noqa: BLE001
            res.update(status="error", message=f"pending store write failed during CONT->STAIRCASE migration: {e}")
            return res
    
    if pending_disabled():
        # no evaluation, no fills: cancel what is still active (LIVE) and stop
        res["disabled"] = DISABLED_REASON
        if live:
            gone = cancel_active_pending(entries, now)
        else:
            gone = [e for e in entries if e.get("status") == ACTIVE]
        res["cancelled"] = [{"id": e.get("id"), "symbol": e.get("symbol"), "kind": e.get("kind"),
                             "reason": DISABLED_REASON + ("" if live else " (DRY_RUN: would cancel)")} for e in gone]
        if live and gone and persist:
            try:
                save_pending(entries)
            except Exception as e:  # noqa: BLE001
                res.update(status="error", message=f"pending store write failed: {e}")
                return res
        res["message"] = (f"CONT/ADD_ON pending DISABLED ({DISABLED_REASON}; re-enable: {DISABLE_ENV}=0)"
                          + (f"; cancelled {len(gone)}" if gone else ""))
        _log(f"{mode} {res['message']}" + (f": {[e.get('id') for e in gone]}" if gone else ""))
        res["pending_active"] = summary(entries, {}, {}, {})
        return res
    act = [e for e in entries if e.get("status") == ACTIVE]
    if not act:
        res["message"] = "no active pending entries"
        return res

    radar_1d = _load_json("gc_radar_1d.json") if radar_1d is None else radar_1d
    radar_4h = _load_json("gc_radar_4h.json") if radar_4h is None else radar_4h
    rows_1d = {r.get("symbol"): r for r in radar_1d.get("rows", []) or []}
    rows_4h = {r.get("symbol"): r for r in radar_4h.get("rows", []) or []}
    for tf, rd in (("1d", radar_1d), ("4h", radar_4h)):
        ok_rc, why_rc = radar_rowcount_ok(rd, tf)
        if not ok_rc:  # fail-closed: no state change, no fills this pass
            res.update(status="fail_closed", message=f"Radar row-count check failed: {why_rc}")
            res["alerts"].append(why_rc)
            _log(f"{mode} fail_closed: {why_rc}")
            return res

    if hl is None:
        from hl_exec import HLClient
        hl = HLClient(os.environ.get("HL_ADDRESS") or None)
    try:
        from executor import _check_all_positions_liq_safe, _parse_account, _user_abstraction
        perp_raw = hl.perp_state()  # fresh positions (after the exit worker in the 4H job)
        account = _parse_account(hl.spot_state(), perp_raw, _user_abstraction(hl))
        meta = hl.meta()
        mids = hl.all_mids()
    except Exception as e:  # noqa: BLE001
        res.update(status="error", message=f"HL state failed: {e}")
        return res
    equity, cum_margin = account["equity"], account["margin_used"]  # NAV snapshot, fixed for the run
    res["nav_snapshot"] = account["nav"]
    held_long = {p["coin"] for p in account["positions"] if p["side"] == "LONG"}
    pos_by_coin = {p["coin"]: p for p in account["positions"]}
    lev_by_coin: Dict[str, int] = {}
    for g in perp_raw.get("assetPositions", []) or []:
        p = g.get("position") or {}
        try:
            lev_by_coin[p.get("coin")] = int((p.get("leverage") or {}).get("value") or 0)
        except (TypeError, ValueError):
            pass
    liq_safe, unsafe = _check_all_positions_liq_safe(account["positions"], {}, radar_4h)

    if log_entry_fn is None:
        from trade_log import log_entry as log_entry_fn  # noqa: N813
    slip = 0.5
    try:
        slip = max(0.0, min(2.0, float(os.environ.get("EXEC_ENTRY_SLIPPAGE_PCT") or 0.5)))
    except ValueError:
        pass
    dirty = False
    try:  # GIIQ-SoT-3 daily cap: real fills already made today (08:55 executor + earlier pending fills)
        from trade_log import count_entries_today
        entries_today = count_entries_today(now, dry_run=False)
    except Exception:  # noqa: BLE001
        entries_today = 0
    entries_run = 0

    for rec in act:
        sym, kind = rec["symbol"], rec["kind"]
        bnd = band(kind, rows_1d.get(sym), rows_4h.get(sym))
        # New 3-step rule: no bar fetching needed, evaluate() uses radar rows directly
        try:  # fresh live mid right before deciding
            mid = (hl.all_mids() or mids).get(sym)
        except Exception:  # noqa: BLE001
            mid = None
        action, why, upd = evaluate(rec, bnd, mid, now, held_long)
        if live and upd:
            rec.update(upd)
            dirty = True
        d1d, d4h = bnd.get("1d", {}), bnd.get("4h", {})
        check = {"id": rec["id"], "symbol": sym, "kind": kind, "action": action, "reason": why,
                 "1d_upper": d1d.get("upper"), "1d_lower": d1d.get("lower"), "1d_close": d1d.get("close"),
                 "4h_upper": d4h.get("upper"), "4h_filter": d4h.get("filter"), "4h_lower": d4h.get("lower"),
                 "4h_close": d4h.get("close"), "mid": mid,
                 "breakout_1d": rec.get("breakout_1d"), "retrace_touched": rec.get("retrace_touched")}
        res["checked"].append(check)

        def mark(status: str, reason: str) -> None:
            nonlocal dirty
            check["result"] = status
            if live:
                rec["status"] = status
                rec["closed_at"] = now.isoformat()
                rec["close_reason"] = reason
                dirty = True
            _log(f"{mode} {sym} {kind} -> {status}: {reason}")

        def note(reason: str) -> None:
            nonlocal dirty
            check["result"] = "waiting"
            check["reason"] = reason
            if live:
                rec["last_check"] = {"at": now.isoformat(), "reason": reason, "mid": mid}
                dirty = True

        if action in ("expire", "cancel"):
            mark("expired" if action == "expire" else "cancelled", why)
            res["cancelled"].append({"id": rec["id"], "symbol": sym, "reason": why})
            continue
        if action == "wait":
            note(why)
            continue

        # ---------------- trigger: same fail-closed SoT checks as the executor, at fill time
        # CONT_STAIRCASE enabled check
        if kind == CONT_STAIRCASE:
            if not cont_staircase_enabled():
                note("CONT_STAIRCASE triggered but CONT_STAIRCASE_ENABLED=0 (not enabled)")
                continue
            staircase_mode = cont_staircase_mode()
            if staircase_mode == "paper":
                _log(f"CONT_STAIRCASE {sym}: paper mode - would trigger but not executing (CONT_STAIRCASE_MODE=paper)")
                note("CONT_STAIRCASE triggered but CONT_STAIRCASE_MODE=paper (log only)")
                continue
        
        # New 3-step rule always uses both 1D and 4H radar
        age_1d = _radar_age_h(radar_1d, now)
        age_4h = _radar_age_h(radar_4h, now)
        if age_1d is None or age_1d > RADAR_MAX_AGE_H["1d"] or age_4h is None or age_4h > RADAR_MAX_AGE_H["4h"]:
            note(f"triggered but radar stale (1d age={age_1d}, 4h age={age_4h}) - fail-closed")
            continue
        if not liq_safe:
            note(f"triggered but unsafe liq on open positions {unsafe} - fail-closed")
            continue
        cm = meta.get(sym)
        if not cm:
            note("coin not in HL meta")
            continue
        sz_dec, coin_max = int(cm.get("szDecimals", 0)), cm.get("maxLeverage")
        tier = rec.get("tier") or "unknown"
        
        # CONT_STAIRCASE uses swing low SL, others use tier-based Hard SL
        if kind == CONT_STAIRCASE:
            from exec_common import swing_low_4h_bars
            hard_sl = swing_low_4h_bars(sym, 12)
            sl_label = "SW12 (lowest low of last 12 4H bars)"
            if not hard_sl or hard_sl <= 0:
                note(f"CONT_STAIRCASE: no swing low SL available")
                continue
        else:
            hard_sl, sl_label = hard_sl_for_tier(tier, rows_4h.get(sym))
            if not hard_sl or hard_sl <= 0:
                note(f"no Hard SL level ({sl_label})")
                continue
        
        # GIIQ-SoT-5: daily entry cap removed
        fixed_lev, room, lev_note = None, None, ""
        if kind == ADD_ON:
            fixed_lev = lev_by_coin.get(sym) or (pos_by_coin.get(sym) or {}).get("leverage")
            if not fixed_lev:
                note("ADD_ON: existing position leverage unknown")
                continue
            ok_add, why_add, room = addon_gates(pos_by_coin.get(sym), equity, mid, fixed_lev)
            if not ok_add:
                note(f"triggered but {why_add}")
                continue
            lev_note = f" (ADD_ON keeps existing isolated {fixed_lev}x; {why_add})"
        ok_px, _diff, why_px = price_sane(mid, radar_ref_price(rows_4h.get(sym), rows_1d.get(sym)))
        if not ok_px:
            note(f"triggered but {why_px}")
            continue
        sl_dist = (mid - hard_sl) / mid * 100.0
        if sl_dist < MIN_SL_DIST_PCT:
            note(f"triggered but SL distance {sl_dist:.2f}% to {sl_label} {hard_sl:.6g} < {MIN_SL_DIST_PCT}%")
            continue
        limit_px = round_price(mid * (1 + slip / 100.0), sz_dec)
        ref_px = max(limit_px, (pos_by_coin.get(sym) or {}).get("entry_px") or 0)  # add-on: worst of both
        
        # CONT_STAIRCASE uses risk-based sizing (0.5% NAV risk, 3x leverage, margin 2-8%)
        if kind == CONT_STAIRCASE:
            # Risk sizing: notional = 0.5% NAV / SL distance
            risk_notional = equity * CONT_STAIRCASE_RISK_PCT / 100.0 / (sl_dist / 100.0)
            # Apply coin notional cap (20% NAV)
            max_notional = equity * MAX_COIN_NOTIONAL_NAV_PCT / 100.0
            risk_notional = min(risk_notional, max_notional)
            # Compute margin at 3x leverage
            margin_usd_unclamped = risk_notional / CONT_STAIRCASE_LEVERAGE
            margin_pct_unclamped = margin_usd_unclamped / equity * 100.0
            # Clamp margin to 2-8% NAV
            margin_pct = max(CONT_STAIRCASE_MIN_MARGIN_PCT, 
                           min(CONT_STAIRCASE_MAX_MARGIN_PCT, margin_pct_unclamped))
            margin_usd = equity * margin_pct / 100.0
            notional = margin_usd * CONT_STAIRCASE_LEVERAGE
            # Check liquidation is beyond SL
            liq = isolated_liq_price_long(limit_px, CONT_STAIRCASE_LEVERAGE, coin_max)
            if not liq or not liq_beyond_sl_long(liq, hard_sl):
                note(f"CONT_STAIRCASE: liquidation {liq:.6g} not beyond SL {hard_sl:.6g}")
                continue
            leverage = CONT_STAIRCASE_LEVERAGE
            size_pct = margin_pct
            lev_note = f" (CONT_STAIRCASE: 0.5% NAV risk, 3x leverage, margin {margin_pct:.1f}% clamped to 2-8%)"
            qty = order_qty(notional, limit_px, sz_dec)
            est_liq = liq
            sz_notes = f"risk {CONT_STAIRCASE_RISK_PCT}% NAV / {sl_dist:.2f}% SL = ${risk_notional:.2f} notional; margin {margin_pct:.1f}% at {leverage}x"
        else:
            sz = size_by_margin(equity, limit_px, hard_sl, coin_max, rec.get("size_pct"), rec.get("leverage"),
                                fixed_leverage=fixed_lev, max_margin_pct=room, liq_ref_px=ref_px, tier=tier)
            if not sz["ok"]:
                note(f"triggered but {sz['reason']}")
                continue
            size_pct, leverage = sz["margin_pct"], sz["leverage"]
            qty = order_qty(equity * size_pct / 100.0 * leverage, limit_px, sz_dec)
            notional = qty * mid
            margin_usd = notional / leverage
            est_liq = sz["liq"]
            sz_notes = sz.get("notes")
        
        notional = qty * mid
        min_usd = min_order_usd(equity)
        if notional < min_usd:
            note(f"notional ${notional:.2f} < minimum ${min_usd:.2f} (max of HL ${MIN_NOTIONAL_USD:g}, 1% NAV)")
            continue
        margin_usd = notional / leverage
        ok_cap, util = margin_cap_ok(cum_margin, margin_usd, equity)
        if not ok_cap:
            note(f"margin utilization {util:.1f}% > {MAX_MARGIN_UTILIZATION_PCT}% (cumulative)")
            continue
        ok_tot, tot_pct = total_margin_nav_ok(cum_margin, margin_usd, equity)
        if not ok_tot:
            note(f"total margin {tot_pct:.1f}% NAV > {MAX_TOTAL_MARGIN_NAV_PCT:g}% cap (cumulative)")
            continue
        existing_ntl = ((pos_by_coin.get(sym) or {}).get("size") or 0.0) * mid if kind == ADD_ON else 0.0
        ok_coin, coin_pct = coin_notional_ok(existing_ntl, notional, equity)
        if not ok_coin:
            note(f"coin notional {coin_pct:.1f}% NAV > {MAX_COIN_NOTIONAL_NAV_PCT:g}% cap")
            continue
        intent = {"id": rec["id"], "symbol": sym, "kind": kind, "mid": mid, "limit_px": limit_px, "qty": qty,
                  "size_pct": size_pct, "leverage": leverage, "notional_usd": round(notional, 2),
                  "margin_usd": round(margin_usd, 2), "hard_sl": hard_sl, "hard_sl_label": sl_label,
                  "sl_dist_pct": round(sl_dist, 3), "estimated_liq": est_liq, "margin_util_after_pct": round(util, 2),
                  "note": lev_note.strip(),
                  "risk_margin_pct": round(margin_usd / equity * 100.0, 3), "sizing_notes": sz_notes,
                  "approved_size_pct": rec.get("size_pct"), "approved_leverage": rec.get("leverage")}
        trade_id = f"{sym}_{kind}_{now.strftime('%Y%m%d_%H%M%S')}"
        if not live:
            cum_margin += margin_usd
            entries_run += 1
            check["result"] = "would_fill"
            res["filled"].append({**intent, "dry_run": True})
            _log(f"DRY_RUN would fill {kind} {sym} qty={qty} limit={limit_px} {leverage}x | SL {hard_sl}{lev_note}")
            continue
        from hl_exec import enter_long_with_sl
        _log(f"LIVE {kind} {sym}: BUY qty={qty} IOC limit={limit_px} {leverage}x isolated, Hard SL {hard_sl}{lev_note}")
        r = enter_long_with_sl(hl, sym, qty, limit_px, leverage, hard_sl, sz_dec)
        intent["live_result"] = {k: v for k, v in r.items() if k != "raw"}
        st = r.get("status")
        if st == "executed":
            cum_margin += (r.get("filled_sz") or qty) * (r.get("avg_px") or limit_px) / leverage
            entries_run += 1
            mark("filled", f"filled {r.get('filled_sz')} @ {r.get('avg_px')}")
            rec["fill"] = {"qty": r.get("filled_sz"), "avg_px": r.get("avg_px"), "sl_oid": r.get("sl_oid"),
                           "hard_sl": hard_sl, "leverage": leverage, "at": now.isoformat()}
            res["filled"].append(intent)
            held_long.add(sym)
            # Tag CONT_STAIRCASE in trade log for exit_worker to recognize
            log_entry_fn(trade_id=trade_id, symbol=sym, entry_type=kind, tier=tier, trend_1d="", trend_4h="",
                         sl_dist_pct=sl_dist, entry_price=r.get("avg_px") or limit_px,
                         entry_size=r.get("filled_sz") or qty, entry_leverage=leverage,
                         ai_decision_reason=rec.get("reason", ""), dry_run=False)
        elif st == "no_fill":
            note("triggered but IOC did not fill; stays pending for re-arm")
        else:
            note(f"live entry {st}; stays pending")
            res["alerts"].append(f"{sym} {kind}: {st}")
            if st and (st.startswith("sl_failed") or st.startswith("reconcile_failed")):
                # the fill was closed (or the close failed): never re-arm automatically
                mark("cancelled", f"{st} (fill closed) - manual review")
            if st and st.endswith("CLOSE_FAILED"):
                res["status"] = "error"
                res["message"] = f"{sym}: {st}: fail-safe close failed - MANUAL ACTION"
            elif res["status"] == "success":
                res["status"] = "error"
                res["message"] = "; ".join(res["alerts"])

    if dirty and live and persist:
        try:
            save_pending(entries)
        except Exception as e:  # noqa: BLE001
            res.update(status="error", message=f"pending store write failed: {e}")
    res["pending_active"] = summary(entries, rows_1d, rows_4h, mids)
    return res


if __name__ == "__main__":
    _after = None
    if "--after-exits" in sys.argv:
        i = sys.argv.index("--after-exits")
        _after = sys.argv[i + 1] if i + 1 < len(sys.argv) else "ok"
    out = run_pending(after_exits=_after)
    print(json.dumps(out, indent=2, default=str))
    sys.exit(0 if out["status"] in ("success", "fail_closed") else 1)
