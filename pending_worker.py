#!/usr/bin/env python3
"""Pending pullback worker, run on closed 4H bars (Railway 4H :10 job, right after the 4H scan).
See pending_entries.py for the rules.

For each ACTIVE pending entry (created by the executor from AI-approved Chase decisions):
  expire (7d) / cancel (band TF closed below Lower, CONTINUATION already held, ADD_ON base gone)
  / wait / trigger when the just-closed 4H bar's low touched the zone (<= Filter) with its close
  above the zone Lower, and the live HL mid is within [Lower, Filter*1.01] (band TF trend Green).
On trigger the SAME fail-closed SoT checks as the executor run again at fill time:
  radar freshness, all open positions liq beyond tier Hard SL, Hard SL per tier (4H radar),
  SL distance >= 1.5% from live mid, SoT size band + 1-5x/coin maxLeverage, min notional,
  cumulative 80% margin cap, isolated liq beyond Hard SL. LIVE entry = hl_exec.enter_long_with_sl
  (IOC + reduce-only Hard SL, fill closed if the SL fails).
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
    MAX_MARGIN_UTILIZATION_PCT,
    MIN_NOTIONAL_USD,
    MIN_SL_DIST_PCT,
    clamp_leverage,
    hard_sl_for_tier,
    is_live_mode,
    isolated_liq_price_long,
    liq_beyond_sl_long,
    margin_cap_ok,
    order_qty,
    parse_ts,
    round_price,
)
from pending_entries import ACTIVE, ADD_ON, band, evaluate, load_pending, save_pending, summary  # noqa: E402

# SoT size bands (margin % of equity). CONTINUATION = SoT "Continuation" 2-4%.
# ADD_ON has no explicit SoT band yet -> same conservative 2-4% (confirm with MMT).
PENDING_SIZE_BANDS = {"CONTINUATION": (2.0, 4.0), "ADD_ON": (2.0, 4.0)}
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


def closed_4h_bar(hl: Any, coin: str, now: datetime) -> Optional[dict]:
    """Just-closed 4H candle from HL candleSnapshot -> {"t","l","c"} or None (fail-closed)."""
    bar_ms = 4 * 3600 * 1000
    now_ms = int(now.timestamp() * 1000)
    try:
        bars = hl.info({"type": "candleSnapshot",
                        "req": {"coin": coin, "interval": "4h", "startTime": now_ms - 3 * bar_ms, "endTime": now_ms}})
    except Exception as e:  # noqa: BLE001
        _log(f"{coin}: 4H candle fetch failed: {e}")
        return None
    closed = [b for b in (bars or []) if int(b.get("t", 0)) + bar_ms <= now_ms]
    if not closed:
        return None
    b = closed[-1]
    try:
        return {"t": int(b["t"]), "l": float(b["l"]), "c": float(b["c"])}
    except (KeyError, TypeError, ValueError):
        return None


def run_pending(hl: Any = None, radar_1d: Optional[dict] = None, radar_4h: Optional[dict] = None,
                now: Optional[datetime] = None, entries: Optional[List[dict]] = None,
                log_entry_fn: Any = None, bar_fn: Any = None) -> Dict[str, Any]:
    live = is_live_mode()
    mode = "LIVE" if live else "DRY_RUN"
    now = now or datetime.now(timezone.utc)
    res: Dict[str, Any] = {"mode": mode, "status": "success", "timestamp": now.isoformat(),
                           "checked": [], "filled": [], "cancelled": [], "alerts": [], "pending_active": []}
    persist = entries is None
    entries = load_pending() if entries is None else entries
    act = [e for e in entries if e.get("status") == ACTIVE]
    if not act:
        res["message"] = "no active pending entries"
        return res

    radar_1d = _load_json("gc_radar_1d.json") if radar_1d is None else radar_1d
    radar_4h = _load_json("gc_radar_4h.json") if radar_4h is None else radar_4h
    rows_1d = {r.get("symbol"): r for r in radar_1d.get("rows", []) or []}
    rows_4h = {r.get("symbol"): r for r in radar_4h.get("rows", []) or []}

    if hl is None:
        from hl_exec import HLClient
        hl = HLClient(os.environ.get("HL_ADDRESS") or None)
    try:
        from executor import _check_all_positions_liq_safe, _parse_account
        perp_raw = hl.perp_state()
        account = _parse_account(hl.spot_state(), perp_raw)
        meta = hl.meta()
        mids = hl.all_mids()
    except Exception as e:  # noqa: BLE001
        res.update(status="error", message=f"HL state failed: {e}")
        return res
    equity, cum_margin = account["equity"], account["margin_used"]
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

    for rec in act:
        sym, kind = rec["symbol"], rec["kind"]
        bnd = band(kind, rows_1d.get(sym), rows_4h.get(sym))
        bar4h = (bar_fn or closed_4h_bar)(hl, sym, now)
        try:  # fresh live mid right before deciding
            mid = (hl.all_mids() or mids).get(sym)
        except Exception:  # noqa: BLE001
            mid = None
        action, why = evaluate(rec, bnd, mid, now, held_long, bar4h)
        check = {"id": rec["id"], "symbol": sym, "kind": kind, "action": action, "reason": why,
                 "zone": [bnd["lower"], bnd["filter"]], "band_tf": bnd["tf"], "mid": mid, "bar4h": bar4h}
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
                rec["last_check"] = {"at": now.isoformat(), "reason": reason, "mid": mid,
                                     "zone": [bnd["lower"], bnd["filter"]]}
                dirty = True

        if action in ("expire", "cancel"):
            mark("expired" if action == "expire" else "cancelled", why)
            res["cancelled"].append({"id": rec["id"], "symbol": sym, "reason": why})
            continue
        if action == "wait":
            note(why)
            continue

        # ---------------- trigger: same fail-closed SoT checks as the executor, at fill time
        age = _radar_age_h(radar_4h, now)
        age_b = _radar_age_h(radar_1d if bnd["tf"] == "1d" else radar_4h, now)
        if age is None or age > RADAR_MAX_AGE_H["4h"] or age_b is None or age_b > RADAR_MAX_AGE_H[bnd["tf"]]:
            note(f"in zone but radar stale (4h age={age}, {bnd['tf']} age={age_b}) - fail-closed")
            continue
        if not liq_safe:
            note(f"in zone but unsafe liq on open positions {unsafe} - fail-closed")
            continue
        cm = meta.get(sym)
        if not cm:
            note("coin not in HL meta")
            continue
        sz_dec, coin_max = int(cm.get("szDecimals", 0)), cm.get("maxLeverage")
        tier = rec.get("tier") or "unknown"
        hard_sl, sl_label = hard_sl_for_tier(tier, rows_4h.get(sym))
        if not hard_sl or hard_sl <= 0:
            note(f"no Hard SL level ({sl_label})")
            continue
        lo_b, hi_b = PENDING_SIZE_BANDS[kind]
        try:
            size_pct = float(rec.get("size_pct") if rec.get("size_pct") is not None else lo_b)
        except (TypeError, ValueError):
            size_pct = lo_b
        size_pct = max(lo_b, min(hi_b, size_pct))
        leverage = clamp_leverage(rec.get("leverage", 2.0), coin_max)
        lev_note = ""
        if kind == ADD_ON and lev_by_coin.get(sym) and lev_by_coin[sym] != leverage:
            lev_note = f" (approved {leverage}x -> existing isolated {lev_by_coin[sym]}x)"
            leverage = clamp_leverage(lev_by_coin[sym], coin_max)
        sl_dist = (mid - hard_sl) / mid * 100.0
        if sl_dist < MIN_SL_DIST_PCT:
            note(f"in zone but SL distance {sl_dist:.2f}% to {sl_label} {hard_sl:.6g} < {MIN_SL_DIST_PCT}%")
            continue
        limit_px = round_price(mid * (1 + slip / 100.0), sz_dec)
        qty = order_qty(equity * size_pct / 100.0 * leverage, limit_px, sz_dec)
        notional = qty * mid
        if notional < MIN_NOTIONAL_USD:
            note(f"notional ${notional:.2f} < ${MIN_NOTIONAL_USD}")
            continue
        margin_usd = notional / leverage
        ok_cap, util = margin_cap_ok(cum_margin, margin_usd, equity)
        if not ok_cap:
            note(f"margin utilization {util:.1f}% > {MAX_MARGIN_UTILIZATION_PCT}% (cumulative)")
            continue
        ref_px = max(limit_px, (pos_by_coin.get(sym) or {}).get("entry_px") or 0)  # add-on: worst of both
        est_liq = isolated_liq_price_long(ref_px, leverage, coin_max)
        if not liq_beyond_sl_long(est_liq, hard_sl):
            note(f"isolated liq {est_liq:.6g} not below Hard SL {hard_sl:.6g}")
            continue
        intent = {"id": rec["id"], "symbol": sym, "kind": kind, "mid": mid, "limit_px": limit_px, "qty": qty,
                  "size_pct": size_pct, "leverage": leverage, "notional_usd": round(notional, 2),
                  "margin_usd": round(margin_usd, 2), "hard_sl": hard_sl, "hard_sl_label": sl_label,
                  "sl_dist_pct": round(sl_dist, 3), "estimated_liq": est_liq, "margin_util_after_pct": round(util, 2),
                  "zone": [bnd["lower"], bnd["filter"]], "note": lev_note.strip()}
        trade_id = f"{sym}_{kind}_{now.strftime('%Y%m%d_%H%M%S')}"
        if not live:
            cum_margin += margin_usd
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
            mark("filled", f"filled {r.get('filled_sz')} @ {r.get('avg_px')}")
            rec["fill"] = {"qty": r.get("filled_sz"), "avg_px": r.get("avg_px"), "sl_oid": r.get("sl_oid"),
                           "hard_sl": hard_sl, "leverage": leverage, "at": now.isoformat()}
            res["filled"].append(intent)
            held_long.add(sym)
            log_entry_fn(trade_id=trade_id, symbol=sym, entry_type=kind, tier=tier, trend_1d="", trend_4h="",
                         sl_dist_pct=sl_dist, entry_price=r.get("avg_px") or limit_px,
                         entry_size=r.get("filled_sz") or qty, entry_leverage=leverage,
                         ai_decision_reason=rec.get("reason", ""), dry_run=False)
        elif st == "no_fill":
            note("in zone but IOC did not fill; stays pending")
        else:
            note(f"live entry {st}; stays pending")
            res["alerts"].append(f"{sym} {kind}: {st}")
            if st and st.startswith("sl_failed"):
                mark("cancelled", f"{st} (fill closed) - manual review")
            if st == "sl_failed_CLOSE_FAILED":
                res["status"] = "error"
                res["message"] = f"{sym}: SL failed AND fail-safe close failed - MANUAL ACTION"
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
    out = run_pending()
    print(json.dumps(out, indent=2, default=str))
    sys.exit(0 if out["status"] == "success" else 1)
