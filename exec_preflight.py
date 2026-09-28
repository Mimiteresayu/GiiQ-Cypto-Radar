#!/usr/bin/env python3
"""Pre-trade preflight for the LIVE executor (runs on Railway at boot and 08:45 HKT).

Surfaces, BEFORE the 08:55 executor, every reason LIVE entries would fail:
  1. mode: EXEC_DRY_RUN / HL_API_PRIVATE_KEY present (secret never printed)
  2. agent: key address is an approved, unexpired agent of HL_ADDRESS (HL info extraAgents);
     warns when fewer than AGENT_EXPIRY_WARN_DAYS remain
  3. account: HL clearinghouse / spot state reachable, equity > 0
  4. signing: side-effect-free signed probe (cancel of a non-existent oid) is accepted by HL
  5. decisions/candidates (informational before 08:55): today's approvals, candidate freshness,
     approved coins listed on HL with maxLeverage >= requested (clamped) leverage, and the
     at-entry Upper guard (Base only: live mid vs 1D Upper; Chase approvals become pending)
  6. pending pullback entries (ADD_ON / CONTINUATION) with trigger zones
  7. GIIQ-SoT-1 guardrails preview: radar row-count (1D/4H/1H; entries fail closed below the
     threshold), NAV snapshot definition, minimum order = max($10, 1% NAV)

Prints one JSON object. Exit 0 = ok (or DRY_RUN), 1 = a check that would block LIVE entries failed.
Never places, modifies or cancels a real order.
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
    SOT2_MAX_LEV,
    SOT2_MIN_LEV,
    min_order_usd,
    nav_snapshot,
    radar_rowcount_ok,
    above_upper_at_entry,
    candidates_fresh,
    clamp_leverage,
    entry_upper_ref,
    is_live_mode,
)


def _load(name: str) -> dict:
    try:
        return json.loads((ROOT / "out" / name).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _log(msg: str) -> None:
    sys.stderr.write(f"[PREFLIGHT] {msg}\n")
    sys.stderr.flush()


def run_preflight(hl: Any = None, signed_probe: bool = True, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    checks: List[Dict[str, Any]] = []
    res: Dict[str, Any] = {"ok": True, "mode": "LIVE" if is_live_mode() else "DRY_RUN",
                           "ts": now.isoformat(), "checks": checks, "warnings": [], "sot": SOT_ID}

    def add(name: str, ok: bool, detail: str, blocking: bool = True) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "blocking": blocking})
        if not ok and blocking:
            res["ok"] = False
        if not ok and not blocking:
            res["warnings"].append(f"{name}: {detail}")
        _log(f"{'OK  ' if ok else ('FAIL' if blocking else 'WARN')} {name}: {detail}")

    dry = (os.environ.get("EXEC_DRY_RUN", "1") or "1").strip()
    key_present = bool((os.environ.get("HL_API_PRIVATE_KEY") or "").strip())
    if not is_live_mode():
        add("mode", False, f"DRY_RUN (EXEC_DRY_RUN={dry}, HL_API_PRIVATE_KEY {'set' if key_present else 'MISSING'}) "
                           "- executor will NOT place orders", blocking=True)
        return res
    add("mode", True, "LIVE (EXEC_DRY_RUN=0, HL_API_PRIVATE_KEY set)")

    if hl is None:
        from hl_exec import HLClient
        hl = HLClient(os.environ.get("HL_ADDRESS") or None)
    res["account"] = hl.address

    try:
        from eth_account import Account
        api_addr = Account.from_key(os.environ["HL_API_PRIVATE_KEY"].strip()).address
    except Exception as e:  # noqa: BLE001  (never echo the key)
        add("key", False, f"HL_API_PRIVATE_KEY is not a valid private key ({type(e).__name__})")
        return res
    res["api_wallet"] = api_addr
    add("key", True, f"HL_API_PRIVATE_KEY -> address {api_addr}")

    st = hl.agent_status(api_addr)
    res["agent"] = {k: st.get(k) for k in ("name", "valid_until_ms", "days_left", "reason")}
    add("agent", bool(st.get("ok")), f"{st.get('reason')} (days_left={st.get('days_left')})")
    if not st.get("ok"):
        return res
    try:
        from hl_exec import AGENT_EXPIRY_WARN_DAYS
    except Exception:  # noqa: BLE001
        AGENT_EXPIRY_WARN_DAYS = 14
    dl = st.get("days_left")
    if dl is not None and dl < AGENT_EXPIRY_WARN_DAYS:
        add("agent_expiry", False, f"agent '{st.get('name')}' expires in {dl} days - renew on HL", blocking=False)
    hint = (os.environ.get("HL_API_WALLET_ADDRESS") or "").strip()
    if hint and hint.lower() != api_addr.lower():
        add("api_wallet_hint", False, f"HL_API_WALLET_ADDRESS={hint} != key address (informational only)", blocking=False)

    held: set = set()
    try:
        perp = hl.perp_state()
        spot = hl.spot_state()
        ab = None
        if callable(getattr(hl, "user_abstraction", None)):
            try:
                ab = hl.user_abstraction()
            except Exception:  # noqa: BLE001
                ab = None
        nav = nav_snapshot(spot, perp, ab, now)
        res["nav"] = nav
        eq = nav["nav"]
        for g in perp.get("assetPositions", []) or []:
            p = g.get("position") or {}
            if abs(float(p.get("szi") or 0)) > 0:
                held.add(p.get("coin"))
        res["equity"] = eq
        add("account", eq > 0, f"equity ${eq:,.2f}, open positions {sorted(held) or 'none'}")
        add("nav", eq > 0, f"NAV snapshot ${eq:,.2f} = {nav['source']}; min order ${min_order_usd(eq):,.2f} "
            f"(max of $10, 1% NAV)", blocking=False)
    except Exception as e:  # noqa: BLE001
        add("account", False, f"HL account state failed: {e}")

    if signed_probe:
        pr = hl.probe_signing()
        add("signing", bool(pr.get("ok")), "HL accepted signed probe (agent can trade for account)"
            if pr.get("ok") else f"HL rejected signed probe: {pr.get('error')}")

    # Radar row-count guardrail (entries fail closed if 1D/4H below threshold; 1H informational)
    for tf in ("1d", "4h", "1h"):
        ok_rc, why_rc = radar_rowcount_ok(_load(f"gc_radar_{tf}.json"), tf)
        add(f"radar_rows:{tf}", ok_rc, why_rc + ("" if ok_rc or tf == "1h" else " -> executor/pending FAIL CLOSED"),
            blocking=False)

    # Informational: today's decisions / candidates / leverage feasibility
    try:
        from decisions import get_decisions_for_today
        decisions = get_decisions_for_today() or {}
    except Exception as e:  # noqa: BLE001
        decisions = {}
        add("decisions", False, f"cannot read decisions: {e}", blocking=False)
    approved = {s: r for s, r in decisions.items() if r.get("decision") == "approve"}
    res["approved"] = sorted(approved)
    if approved:
        try:
            cand = json.loads((ROOT / "out" / "entry_candidates_latest.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cand = {}
        fresh, why = candidates_fresh(cand, now=now)
        add("candidates", fresh, why, blocking=False)
        try:
            meta = hl.meta()
            for sym, rec in sorted(approved.items()):
                cm = meta.get(sym)
                if not cm:
                    add(f"lev:{sym}", False, "not in HL meta (delisted/unknown)", blocking=False)
                    continue
                ml = cm.get("maxLeverage") or 0
                add(f"lev:{sym}", ml >= SOT2_MIN_LEV,
                    f"{SOT_ID}: isolated {SOT2_MIN_LEV}-{min(SOT2_MAX_LEV, int(ml)) if ml else '?'}x chosen by risk at order "
                    f"time (AI max {rec.get('leverage')}x / {rec.get('size_pct')}%; coin maxLeverage {ml:g})"
                    + (" (already held -> executor will skip)" if sym in held else "")
                    + ("" if ml >= SOT2_MIN_LEV else f" -> maxLeverage < {SOT2_MIN_LEV}x: executor will SKIP"),
                    blocking=False)
        except Exception as e:  # noqa: BLE001
            add("meta", False, f"HL meta failed: {e}", blocking=False)
        # At-entry Upper guard preview (the executor re-checks with a fresh mid at order time)
        try:
            mids = hl.all_mids()
            rows_1d = {r.get("symbol"): r for r in (_load("gc_radar_1d.json").get("rows") or [])}
            rows_4h = {r.get("symbol"): r for r in (_load("gc_radar_4h.json").get("rows") or [])}
            cmap = {c.get("symbol"): c for c in (cand.get("candidates") or [])}
            guard = {}
            for sym in sorted(approved):
                c = cmap.get(sym)
                if not c:
                    guard[sym] = {"type": None, "note": "not in current candidate list"}
                    add(f"guard:{sym}", False, "approved but not in the current candidate list -> executor will skip",
                        blocking=False)
                    continue
                if c.get("type") == "Chase" and not c.get("is_base"):
                    kind = "ADD_ON" if sym in held else "CONTINUATION"
                    guard[sym] = {"type": "Chase", "pending_kind": kind}
                    add(f"guard:{sym}", True, f"Chase -> no 08:55 entry; becomes pending {kind} "
                        f"({'4H' if kind == 'ADD_ON' else '1D'} pullback zone, N+1 confirmation)", blocking=False)
                    continue
                c = dict(c, type="Base")
                up, label = entry_upper_ref(c, rows_1d.get(sym), rows_4h.get(sym))
                mid = mids.get(sym)
                ok = above_upper_at_entry(mid, up)
                guard[sym] = {"type": c.get("type"), "upper_label": label, "upper": up, "mid": mid, "above": ok}
                add(f"guard:{sym}", ok, f"{c.get('type') or '?'}: live mid {mid} {'>' if ok else '<='} {label} {up}"
                    + ("" if ok else " -> executor would SKIP (below Upper at entry)"), blocking=False)
            res["entry_guard"] = guard
        except Exception as e:  # noqa: BLE001
            add("guard", False, f"at-entry Upper guard preview failed: {e}", blocking=False)
    else:
        add("decisions", True, "no approvals stored yet for today (AI desk posts before 08:55)", blocking=False)
    # Pending pullback entries (ADD_ON / CONTINUATION): list + trigger zones (informational)
    try:
        from pending_entries import load_pending, summary as pending_summary
        rows_1d = {r.get("symbol"): r for r in (_load("gc_radar_1d.json").get("rows") or [])}
        rows_4h = {r.get("symbol"): r for r in (_load("gc_radar_4h.json").get("rows") or [])}
        try:
            pmids = hl.all_mids()
        except Exception:  # noqa: BLE001
            pmids = {}
        pend = pending_summary(load_pending(), rows_1d, rows_4h, pmids)
        res["pending"] = pend
        for p in pend:
            add(f"pending:{p['symbol']}", True,
                f"{p['kind']} zone {p['band_tf'].upper()} [{p['zone_lower']}, {p['zone_filter']}] mid {p['mid']}"
                f"{' IN ZONE' if p['in_zone'] else ''} exp {p['expires_at']}", blocking=False)
        if not pend:
            add("pending", True, "no active pending pullback entries", blocking=False)
    except Exception as e:  # noqa: BLE001
        add("pending", False, f"pending list failed: {e}", blocking=False)
    return res


if __name__ == "__main__":
    out = run_preflight()
    print(json.dumps(out, indent=2, default=str))
    sys.exit(0 if (out["ok"] or out["mode"] == "DRY_RUN") else 1)
