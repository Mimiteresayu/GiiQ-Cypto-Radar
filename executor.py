#!/usr/bin/env python3
"""Auto-executor for AI-approved entry candidates (08:55 HKT).

SoT enforcement (see exec_common.py):
- Candidates must be fresh (built today HKT, <= EXEC_MAX_CANDIDATE_AGE_H old, not stale)
- Min notional max($10, 1% NAV); leverage GIIQ-SoT-2 3-5x isolated AND <= coin HL maxLeverage (integer)
- SL distance >= 1.5% from the LIVE mid; Hard SL per tier (Mega/Large 4H Lower, Small/Tiny 4H Filter)
- Isolated liq price must lie below the Hard SL
- Base (fresh 1D dual-cross-up) enters now, guarded: a live HL mid fetched right before the
  order must be ABOVE the 1D Upper from the latest closed-bar scan, else skip
- CONT / ADD_ON approvals (radar type "Chase") never enter at 08:55: they become 3-step watch records
  (pending_entries.py): CONT = no Base position in the coin, ADD_ON = Base position held. Entry only when
  1D dual cross up above 1D Upper -> 4H retrace to 4H Filter/Lower -> 4H dual cross up above 4H Upper;
  pending_worker.py checks every 4h at :10 HKT (after the 4H exits) and places an immediate IOC entry with
  the Hard SL (no resting orders), with the same SoT checks.
  Watch records are only written in LIVE mode (DRY_RUN reports "would_create"). Old N/N+1 records are
  cancelled here too (OLD_STYLE_REPLACED_BY_3STEP), so they can never block a new 3-step record.
  DISABLED by default (Cove HEALTH FAIL 2026-10-05): CONT/ADD_ON approvals are acknowledged with no entry
  (result "no_entry", no record) and active CONT / ADD_ON records are cancelled (LIVE),
  unless PENDING_CONTINUATION_DISABLED=0. Base is unchanged.
- Approvals are matched against today's frozen decision snapshot entry_candidates_decision_YYYYMMDD.json
  (HKT), falling back to entry_candidates_latest.json only when it is missing; the freshness guard
  below still runs on the latest list.
- Total margin (existing + all new entries, cumulative) <= 80% equity (outer hard cap) AND <= 70% NAV (GIIQ-SoT-4; was 30%)
- GIIQ-SoT-3: coin notional <= 20% NAV; max 3 new fills per HKT day (Base + pending together);
  fallback decisions (Harbor, only when Claude's POST never arrived) = Base only at 2% margin;
  loud alert when Claude's POST is missing (RED after 2 consecutive days)
- All open positions must already have liq beyond their tier Hard SL
- BTC 4H close < 4H Filter -> fixed 4% per coin
- size_pct = margin % of equity; notional = margin x leverage; qty = notional / price

Guardrails (GIIQ-SoT-1, see docs/SOT_CHANGELOG.md; no strategy change):
- Radar row-count: 1D and 4H closed-bar radars must have >= RADAR_MIN_ROWS (120) rows and
  >= 85% of the requested universe, else the whole run fails closed
- NAV snapshot ONCE per run (exec_common.nav_snapshot) = "equity" for all sizing / margin cap
- Price sanity: skip a coin if the HL live mid differs from the radar price by > 50%
- Minimum order: skip if notional < max(HL $10 minimum, 1% of NAV)
- run_report: executed / skipped / downsized / failed with reasons (cockpit + DESK_DATA)

Sizing (GIIQ-SoT-2, exec_common.size_by_margin; replaces the old 2x / P-band sizing):
- per-trade risk = the isolated margin: 2-4% NAV per coin (hard cap 4%); NOT sized by SL distance
- isolated 3-5x (<= coin maxLeverage); AI size/leverage = maximums inside the bands
- isolated liq must sit below the Hard SL; else step leverage down toward 3x; impossible at 3x -> skip
- existing 80% total margin cap

Entry: IOC limit buy at live mid + EXEC_ENTRY_SLIPPAGE_PCT (default 0.5%), isolated margin,
then an immediate reduce-only stop-market Hard SL for the filled size. If the SL cannot be
placed the fill is closed at once (fail-safe). See hl_exec.enter_long_with_sl.

DRY_RUN by default (EXEC_DRY_RUN=1). LIVE only if EXEC_DRY_RUN=0 AND HL_API_PRIVATE_KEY present.
Exit code: 0 = success or fail_closed (normal "nothing to do"), 1 = error.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from exec_common import (  # noqa: E402
    SOT_ID,
    build_run_report,
    min_order_usd,
    nav_snapshot,
    price_sane,
    radar_ref_price,
    radar_rowcount_ok,
    size_by_margin,
    MAX_LEVERAGE,
    MAX_MARGIN_UTILIZATION_PCT,
    MIN_LEVERAGE,
    MIN_NOTIONAL_USD,
    MIN_SL_DIST_PCT,
    candidates_fresh,
    above_upper_at_entry,
    clamp_leverage,
    entry_upper_ref,
    hard_sl_for_tier,
    is_live_mode,
    isolated_liq_price_long,
    liq_beyond_sl_long,
    margin_cap_ok,
    order_qty,
    round_price,
    coin_notional_ok,
    count_entries_today_for_reporting,
    total_margin_nav_ok,
    FALLBACK_MARGIN_PCT,
    FALLBACK_LEVERAGE,
    MAX_COIN_NOTIONAL_NAV_PCT,
    MAX_TOTAL_MARGIN_NAV_PCT,
    hkt_date,
)

from pending_entries import band as pending_band  # noqa: E402
from pending_entries import classify_chase, create_pending, load_pending, save_pending  # noqa: E402
from pending_entries import summary as pending_summary  # noqa: E402
from pending_entries import DISABLE_ENV, DISABLED_REASON, cancel_active_pending, pending_disabled  # noqa: E402
from pending_entries import cancel_old_style  # noqa: E402
from entry_candidates import load_decision_candidates  # noqa: E402

try:
    from decisions import get_decisions_for_today
    from trade_log import log_entry, count_entries_today
    from mcap_tiers import tier_for
except ImportError as e:  # pragma: no cover
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)

HL_ADDRESS = os.environ.get("HL_ADDRESS", "0xcFCda0F8576a268BaA17935368081F4e687dB122").strip()


def _entry_slippage_pct() -> float:
    try:
        return max(0.0, min(2.0, float(os.environ.get("EXEC_ENTRY_SLIPPAGE_PCT") or 0.5)))
    except ValueError:
        return 0.5


def _log(msg: str) -> None:
    sys.stderr.write(f"[EXECUTOR] {msg}\n")
    sys.stderr.flush()


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _load_radar(tf: str) -> dict:
    return _load_json(ROOT / "out" / f"gc_radar_{tf}.json")


def _load_candidates() -> dict:
    return _load_json(ROOT / "out" / "entry_candidates_latest.json")


def _parse_account(spot: dict, perp: dict, abstraction: Optional[str] = None) -> dict:
    """equity = NAV snapshot (exec_common.nav_snapshot: unified -> spot USDC total);
    margin from perp marginSummary."""
    nav = nav_snapshot(spot, perp, abstraction)
    equity = nav["nav"]
    try:
        margin_used = float((perp.get("marginSummary") or {}).get("totalMarginUsed", 0))
    except (TypeError, ValueError):
        margin_used = 0.0
    positions = []
    for grp in perp.get("assetPositions", []) or []:
        p = grp.get("position") or {}
        try:
            szi = float(p.get("szi", 0))
        except (TypeError, ValueError):
            continue
        if abs(szi) < 1e-12:
            continue
        positions.append({
            "coin": p.get("coin", ""),
            "side": "LONG" if szi > 0 else "SHORT",
            "size": abs(szi),
            "entry_px": float(p.get("entryPx") or 0),
            "liquidation_px": float(p.get("liquidationPx") or 0),
            "margin_used": float(p.get("marginUsed") or 0),
            "unrealized_pnl": float(p.get("unrealizedPnl") or 0),
            "roe_pct": (float(p["returnOnEquity"]) * 100.0 if p.get("returnOnEquity") not in (None, "")
                        else (float(p.get("unrealizedPnl") or 0) / float(p["marginUsed"]) * 100.0
                              if float(p.get("marginUsed") or 0) > 0 else None)),
            "leverage": int(float((p.get("leverage") or {}).get("value") or 0)) or None,
        })
    return {"equity": equity, "margin_used": margin_used, "free_margin": max(0.0, equity - margin_used),
            "positions": positions, "nav": nav}


def _user_abstraction(hl: Any) -> Optional[str]:
    fn = getattr(hl, "user_abstraction", None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None




def _check_liq_beyond_sl(entry_price: float, hard_sl: float, estimated_liq_price: Optional[float]) -> bool:
    """LONG: liquidation strictly below Hard SL is safe."""
    return liq_beyond_sl_long(estimated_liq_price, hard_sl)


def _check_all_positions_liq_safe(positions: List[dict], radar_1h: dict, radar_4h: dict) -> Tuple[bool, List[str]]:
    """Every open LONG must have its liquidation below its TIER Hard SL
    (Mega/Large: 4H Lower; Small/Tiny: 4H Filter)."""
    r4h_map = {r["symbol"]: r for r in radar_4h.get("rows", []) if r.get("symbol")}
    unsafe = []
    for pos in positions:
        coin = pos["coin"]
        liq_px = pos.get("liquidation_px") or 0
        if liq_px <= 0:
            continue
        r4h = r4h_map.get(coin) or r4h_map.get(f"{coin}-PERP")
        hard_sl, _ = hard_sl_for_tier(tier_for(coin), r4h)
        if not hard_sl:
            continue
        if pos["side"] == "LONG" and liq_px >= hard_sl:
            unsafe.append(coin)
        elif pos["side"] == "SHORT" and liq_px <= hard_sl:
            unsafe.append(coin)
    return len(unsafe) == 0, unsafe


def execute_approved_candidates(
    hl: Any = None,
    candidates_data: Optional[dict] = None,
    decisions: Optional[Dict[str, dict]] = None,
    radar_1h: Optional[dict] = None,
    radar_4h: Optional[dict] = None,
    now: Optional[datetime] = None,
    radar_1d: Optional[dict] = None,
) -> Dict[str, Any]:
    """Run one execution pass. All inputs injectable for tests; defaults read disk/HL.
    The result always carries `sot` and a `run_report` (executed/skipped/downsized/failed)."""
    result = _execute(hl, candidates_data, decisions, radar_1h, radar_4h, now, radar_1d)
    result["sot"] = SOT_ID
    try:
        result["run_report"] = build_run_report("executor", result, result.get("nav_snapshot"),
                                                result.get("approved_decisions"))
    except Exception as e:  # noqa: BLE001  (report must never break the run)
        result["run_report"] = {"sot": SOT_ID, "run": "executor", "error": str(e)}
    return result


def _execute(
    hl: Any = None,
    candidates_data: Optional[dict] = None,
    decisions: Optional[Dict[str, dict]] = None,
    radar_1h: Optional[dict] = None,
    radar_4h: Optional[dict] = None,
    now: Optional[datetime] = None,
    radar_1d: Optional[dict] = None,
) -> Dict[str, Any]:
    live = is_live_mode()
    mode = "LIVE" if live else "DRY_RUN"
    now = now or datetime.now(timezone.utc)
    result: Dict[str, Any] = {
        "mode": mode, "status": "success", "executed": [], "skipped": [], "actions": [], "alerts": [],
        "pending": [], "no_entry": [], "timestamp": now.isoformat(),
    }

    latest_data = candidates_data
    if candidates_data is None:
        latest_data = _load_candidates()
        candidates_data, cand_label, frozen = load_decision_candidates(now)
        result["candidates_source"] = cand_label
        _log(f"{mode} candidates: {'decision snapshot' if frozen else 'NO decision snapshot'} -> {cand_label}")
    if decisions is None:
        decisions = get_decisions_for_today()
        # GIIQ-SoT-3: loud when Claude's ENTRY_DESK POST never arrived (fallback-only day)
        try:
            from decisions import days_without_claude
            miss = days_without_claude(now)
            result["claude_post"] = {"missing_days": miss}
            if miss >= 2:
                result["alerts"].append(f"RED: no Claude ENTRY_DESK POST for {miss} days in a row "
                                        f"(POST_BLOCKED?) - fallback only (all candidates, "
                                        f"{FALLBACK_MARGIN_PCT:g}% / {FALLBACK_LEVERAGE}x)")
            elif miss == 1:
                result["alerts"].append(f"Claude ENTRY_DESK POST missing today - fallback only "
                                        f"(all candidates, {FALLBACK_MARGIN_PCT:g}% / {FALLBACK_LEVERAGE}x)")
        except Exception as e:  # noqa: BLE001
            result["alerts"].append(f"claude POST check failed: {e}")
    approved = [s for s, rec in decisions.items() if rec.get("decision") == "approve"]
    result["approved_decisions"] = {s: {"size_pct": decisions[s].get("size_pct"),
                                        "leverage": decisions[s].get("leverage")} for s in approved}
    if not approved:
        result.update(status="fail_closed", message="No approved candidates")
        _log(f"{mode} fail_closed: no approved candidates")
        return result

    fresh, why = candidates_fresh(latest_data, now=now)
    if not fresh:
        result.update(status="fail_closed", message=f"Candidates not fresh: {why}")
        _log(f"{mode} fail_closed: {why}")
        return result
    candidates = candidates_data.get("candidates", []) or []

    radar_1h = _load_radar("1h") if radar_1h is None else radar_1h
    radar_4h = _load_radar("4h") if radar_4h is None else radar_4h
    radar_1d = _load_radar("1d") if radar_1d is None else radar_1d
    rc_notes = []
    for tf, rd in (("1d", radar_1d), ("4h", radar_4h)):
        ok_rc, why_rc = radar_rowcount_ok(rd, tf)
        rc_notes.append(why_rc)
        if not ok_rc:
            result.update(status="fail_closed", message=f"Radar row-count check failed: {why_rc}")
            result["alerts"].append(why_rc)
            _log(f"{mode} fail_closed: {why_rc}")
            return result
    result["radar_rowcount"] = rc_notes

    if hl is None:
        from hl_exec import HLClient
        hl = HLClient(HL_ADDRESS)
    try:
        account = _parse_account(hl.spot_state(), hl.perp_state(), _user_abstraction(hl))
        meta = hl.meta()
        mids = hl.all_mids()
    except Exception as e:  # noqa: BLE001
        result.update(status="error", message=f"Failed to get HL state: {e}")
        _log(f"error: HL state: {e}")
        return result

    # NAV snapshot: taken ONCE here and used for every size / margin-cap / min-order check below
    result["nav_snapshot"] = account["nav"]
    equity = account["equity"]
    _log(f"{mode} NAV snapshot ${equity:,.2f} ({account['nav']['source']})")
    cum_margin = account["margin_used"]
    held = {p["coin"] for p in account["positions"]}
    held_long = {p["coin"] for p in account["positions"] if p["side"] == "LONG"}
    pend_entries = load_pending()
    pend_dirty = False
    old_style = cancel_old_style(pend_entries, now, tag="HL", log=_log)   # housekeeping, any mode
    if old_style:
        pend_dirty = True
        result["pending_cancelled_old_style"] = [e.get("id") for e in old_style]
    disabled = pending_disabled()
    if disabled:
        result["pending_disabled"] = DISABLED_REASON
        if live:
            gone = cancel_active_pending(pend_entries, now)
            if gone:
                pend_dirty = True
                result["pending_cancelled"] = [e.get("id") for e in gone]
                _log(f"LIVE cancelled active CONT/ADD_ON ({DISABLED_REASON}): {result['pending_cancelled']}")
    if equity <= 0:
        result.update(status="error", message="Equity is 0 / unavailable")
        return result

    liq_safe, unsafe = _check_all_positions_liq_safe(account["positions"], radar_1h, radar_4h)
    if not liq_safe:
        result.update(status="fail_closed", message=f"Unsafe liquidation prices for: {', '.join(unsafe)}")
        _log(f"{mode} fail_closed: unsafe liq {unsafe}")
        return result

    if live and hasattr(hl, "exchange"):
        # Build + authenticate the signing client ONCE, before any coin. A key/agent problem is
        # an executor ERROR (loud, shown in the cockpit), not three silent per-coin skips.
        try:
            hl.exchange()
        except Exception as e:  # noqa: BLE001
            msg = f"LIVE signing client refused: {e}"
            result.update(status="error", message=msg)
            result["alerts"].append(msg)
            result["skipped"] = [{"symbol": s, "reason": "signing client refused"} for s in approved]
            _log(f"LIVE ERROR: {msg}")
            return result

    slip = _entry_slippage_pct()
    try:  # GIIQ-SoT-5: daily cap removed, count kept for reporting only
        entries_today = count_entries_today(now, dry_run=False)
    except Exception:  # noqa: BLE001
        entries_today = 0
    entries_run = 0
    result["entries_today_before"] = entries_today
    
    # GIIQ-SoT-5: process approvals in desk priority order (CONT/ADD_ON first), then candidate order
    approved_list = sorted(approved, key=lambda s: (
        0 if (next((c for c in candidates if c.get("symbol") == s), {}) or {}).get("type") == "Chase" else 1,
        s
    ))
    
    for symbol in approved_list:
        cand = next((c for c in candidates if c.get("symbol") == symbol), None)
        if not cand:
            continue
        decision = decisions.get(symbol, {})
        entry_type = cand.get("type", "Base")
        tier = cand.get("tier") or tier_for(symbol)

        def skip(reason: str, **extra: Any) -> None:
            result["skipped"].append({"symbol": symbol, "reason": reason, **extra})
            _log(f"{mode} SKIP {symbol}: {reason}")

        is_base = bool(cand.get("is_base")) or entry_type == "Base"
        fallback = str(decision.get("source") or "") == "fallback"
        # GIIQ-SoT-5: fallback approves every executable candidate at floor size (not just Base)
        # CONT / ADD_ON (radar type "Chase") -> 3-step watch record, never an immediate entry
        if not is_base and entry_type == "Chase":
            if disabled:
                why = (f"CONT/ADD_ON acknowledged, no entry: 3-step entries disabled ({DISABLED_REASON}; "
                       f"re-enable {DISABLE_ENV}=0)")
                result["no_entry"].append({"symbol": symbol, "type": classify_chase(symbol, held_long), "reason": why})
                _log(f"{mode} NO ENTRY {symbol}: {why}")
                continue
            # CONT / ADD_ON -> 3-step watch record (entry only on the 4H BO), never an 08:55 order
            if symbol in held and symbol not in held_long:
                skip("CONT/ADD_ON signal on a SHORT position: no add-on")
                continue
            kind = classify_chase(symbol, held_long)
            r1d_p = next((r for r in radar_1d.get("rows", []) if r.get("symbol") == symbol), None)
            r4h_p = next((r for r in radar_4h.get("rows", []) if r.get("symbol") == symbol), None)
            bnd = pending_band(kind, r1d_p, r4h_p)
            d4h = bnd.get("4h", {})
            info = {"symbol": symbol, "kind": kind, "4h_lower": d4h.get("lower"),
                    "4h_filter": d4h.get("filter"), "size_pct": decision.get("size_pct"),
                    "leverage": decision.get("leverage")}
            if live:
                rec, created = create_pending(pend_entries, symbol, kind, decision, cand, bnd, now)
                pend_dirty = pend_dirty or created
                info.update(id=rec["id"], created=created, status=rec.get("status"),
                            expires_at=rec.get("expires_at"))
            else:
                info["would_create"] = True
            result["pending"].append(info)
            _log(f"{mode} PENDING {kind} {symbol}: 3-step rule (1D breakout → 4H retrace → 4H cross-up)"
                 f"{'' if live else ' (DRY_RUN: not stored)'}")
            continue
        if not is_base:
            skip(f"unknown entry type {entry_type!r} (fail-closed)")
            continue
        entry_type = "Base"
        cand = dict(cand, type="Base")  # Base guard = 1D Upper even if the row is also a 4H CONT/ADD_ON signal

        if symbol in held:
            skip("already holding a position")
            continue
        cm = meta.get(symbol)
        if not cm:
            skip("coin not in HL meta (delisted/unknown)")
            continue
        sz_dec = int(cm.get("szDecimals", 0))
        coin_max = cm.get("maxLeverage")
        r4h = next((r for r in radar_4h.get("rows", []) if r.get("symbol") == symbol), None)
        hard_sl, sl_label = hard_sl_for_tier(tier, r4h or {"lower": cand.get("lower_4h"), "filter": cand.get("filter_4h")})
        if not hard_sl or hard_sl <= 0:
            skip(f"no Hard SL level ({sl_label})")
            continue

        size_pct, leverage = None, None
        try:  # fresh live mid at order time (fail-closed if unavailable)
            mids = hl.all_mids() or mids
        except Exception as e:  # noqa: BLE001
            skip(f"live mid refresh failed: {e}")
            continue
        mid = mids.get(symbol)
        if not mid or mid <= 0:
            skip("no live mid price")
            continue
        r1d_px = next((r for r in radar_1d.get("rows", []) if r.get("symbol") == symbol), None)
        # radar price: latest closed 4H / 1D radar close, else the 1D close frozen in the candidate
        ok_px, _diff, why_px = price_sane(mid, radar_ref_price(r4h, r1d_px) or radar_ref_price(
            {"close": cand.get("close_1d") or cand.get("close")}))
        if not ok_px:
            skip(why_px, mid=mid)
            continue
        sl_dist_pct = (mid - hard_sl) / mid * 100.0
        if sl_dist_pct < MIN_SL_DIST_PCT:
            skip(f"SL distance {sl_dist_pct:.2f}% from live mid < {MIN_SL_DIST_PCT:g}%", size_pct=size_pct, leverage=leverage)
            continue

        limit_px = round_price(mid * (1 + slip / 100.0), sz_dec)
        # GIIQ-SoT-5: daily entry cap removed
        # GIIQ-SoT-5: a fallback decision is always floor size, 2% margin at 2x (whatever the record says)
        ai_size = FALLBACK_MARGIN_PCT if fallback else decision.get("size_pct")
        ai_lev = FALLBACK_LEVERAGE if fallback else decision.get("leverage")
        sz = size_by_margin(equity, limit_px, hard_sl, coin_max, ai_size, ai_lev, tier=tier)
        if not sz["ok"]:
            skip(sz["reason"], mid=mid)
            continue
        size_pct, leverage = sz["margin_pct"], sz["leverage"]
        margin_usd = equity * size_pct / 100.0
        notional_target = margin_usd * leverage
        qty = order_qty(notional_target, limit_px, sz_dec)
        notional = qty * mid
        min_usd = min_order_usd(equity)
        if notional < min_usd:
            skip(f"Notional ${notional:.2f} < minimum ${min_usd:.2f} (HL minimum order)",
                 size_pct=size_pct, leverage=leverage)
            continue
        margin_usd = notional / leverage  # actual margin after lot rounding
        ok_cap, util = margin_cap_ok(cum_margin, margin_usd, equity)
        if not ok_cap:
            skip(f"Margin utilization {util:.1f}% > {MAX_MARGIN_UTILIZATION_PCT}% (cumulative)", size_pct=size_pct, leverage=leverage)
            continue
        ok_tot, tot_pct = total_margin_nav_ok(cum_margin, margin_usd, equity)
        if not ok_tot:
            skip(f"total margin {tot_pct:.1f}% NAV > {MAX_TOTAL_MARGIN_NAV_PCT:g}% cap (cumulative)",
                 size_pct=size_pct, leverage=leverage)
            continue
        ok_coin, coin_pct = coin_notional_ok(0.0, notional, equity)
        if not ok_coin:
            skip(f"coin notional {coin_pct:.1f}% NAV > {MAX_COIN_NOTIONAL_NAV_PCT:g}% cap", size_pct=size_pct, leverage=leverage)
            continue
        est_liq = sz["liq"]  # worst-case (highest) entry; strictly below the Hard SL

        r1d = next((r for r in radar_1d.get("rows", []) if r.get("symbol") == symbol), None)
        upper_ref, upper_label = entry_upper_ref(cand, r1d, r4h)
        if not above_upper_at_entry(mid, upper_ref):
            why = (f"below {upper_label} at entry (live mid {mid:.6g} <= {upper_ref:.6g})" if upper_ref
                   else f"no {upper_label} available for at-entry guard")
            skip(why, size_pct=size_pct, leverage=leverage, mid=mid, upper_ref=upper_ref)
            result["alerts"].append(f"{symbol}: {why}")
            continue

        trade_id = f"{symbol}_{now.strftime('%Y%m%d_%H%M%S')}"
        intent = {
            "symbol": symbol, "trade_id": trade_id, "entry_type": entry_type, "tier": tier,
            "mid": mid, "limit_px": limit_px, "qty": qty, "size_pct": size_pct,
            "margin_usd": round(margin_usd, 2), "leverage": leverage, "margin_mode": "isolated",
            "notional_usd": round(notional, 2), "hard_sl": hard_sl, "hard_sl_label": sl_label,
            "sl_dist_pct": round(sl_dist_pct, 3), "estimated_liq": est_liq, "coin_max_leverage": coin_max,
            "margin_util_after_pct": round(util, 2),
            "entry_upper_ref": upper_ref, "entry_upper_label": upper_label,
            "risk_margin_pct": round(margin_usd / equity * 100.0, 3), "sizing_notes": sz.get("notes"),
            "ai_size_pct": decision.get("size_pct"), "ai_leverage": decision.get("leverage"),
            "decision_source": decision.get("source") or "claude", "total_margin_nav_pct": round(tot_pct, 2),
        }

        if not live:
            cum_margin += margin_usd
            entries_run += 1
            result["actions"].append(intent)
            _log(
                f"DRY_RUN would place: BUY {symbol} qty={qty} IOC limit={limit_px} (mid {mid}) {leverage}x isolated "
                f"notional=${notional:.2f} margin=${margin_usd:.2f} | Hard SL {sl_label} {hard_sl} ({sl_dist_pct:.2f}%) "
                f"| liq~{est_liq:.6g} | util {util:.1f}%"
            )
            log_entry(trade_id=trade_id, symbol=symbol, entry_type=entry_type, tier=tier,
                      trend_1d=cand.get("trend_1d", ""), trend_4h=cand.get("trend_4h", ""),
                      sl_dist_pct=sl_dist_pct, entry_price=limit_px, entry_size=qty, entry_leverage=leverage,
                      ai_decision_reason=decision.get("reason", ""), dry_run=True)
            continue

        # ---------------- LIVE
        from hl_exec import enter_long_with_sl
        _log(f"LIVE placing: BUY {symbol} qty={qty} IOC limit={limit_px} {leverage}x isolated, Hard SL {hard_sl}")
        r = enter_long_with_sl(hl, symbol, qty, limit_px, leverage, hard_sl, sz_dec, coin_max_leverage=coin_max, now=now)
        intent["live_result"] = r
        st = r.get("status")
        if st == "executed":
            # GIIQ-SoT-5: use actual_margin_used from reconciliation if available
            actual_margin = r.get("actual_margin_used")
            if actual_margin is not None:
                cum_margin += actual_margin
            else:
                cum_margin += (r.get("filled_sz") or qty) * (r.get("avg_px") or limit_px) / leverage
            entries_run += 1
            result["executed"].append(intent)
            log_entry(trade_id=trade_id, symbol=symbol, entry_type=entry_type, tier=tier,
                      trend_1d=cand.get("trend_1d", ""), trend_4h=cand.get("trend_4h", ""),
                      sl_dist_pct=sl_dist_pct, entry_price=r.get("avg_px") or limit_px,
                      entry_size=r.get("filled_sz") or qty, entry_leverage=leverage,
                      ai_decision_reason=decision.get("reason", ""), dry_run=False)
        else:
            skip(f"live entry {st}", live_result=r)
            if st in ("leverage_failed", "entry_failed"):
                result["alerts"].append(f"{symbol}: {st}")
                if result["status"] == "success":
                    result["status"] = "error"
                    result["message"] = f"LIVE entry failures: {', '.join(result['alerts'])}"
                else:
                    result["message"] = f"LIVE entry failures: {', '.join(result['alerts'])}"
            if st and (st.startswith("sl_failed") or st.startswith("reconcile_failed")):
                probs = "; ".join((r.get("reconcile") or {}).get("problems") or [])
                result["alerts"].append(f"{symbol}: {st}" + (f" ({probs})" if probs else ""))
                if st.endswith("CLOSE_FAILED"):
                    result["status"] = "error"
                    result["message"] = (f"{symbol}: {'SL' if st.startswith('sl_') else 'post-fill reconciliation'} "
                                         f"failed AND fail-safe close failed - MANUAL ACTION")
    if pend_dirty:
        try:
            save_pending(pend_entries)
        except Exception as e:  # noqa: BLE001
            result["alerts"].append(f"pending store write failed: {e}")
            result["status"] = "error"
            result["message"] = f"pending store write failed: {e}"
    try:
        rows_1d = {r.get("symbol"): r for r in radar_1d.get("rows", []) or []}
        rows_4h = {r.get("symbol"): r for r in radar_4h.get("rows", []) or []}
        result["pending_active"] = pending_summary(pend_entries, rows_1d, rows_4h, mids)
    except Exception:  # noqa: BLE001
        result["pending_active"] = []
    return result


if __name__ == "__main__":
    res = execute_approved_candidates()
    print(json.dumps(res, indent=2, default=str))
    sys.exit(0 if res["status"] in ("success", "fail_closed") else 1)
