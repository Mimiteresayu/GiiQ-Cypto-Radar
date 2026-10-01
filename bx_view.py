#!/usr/bin/env python3
"""Bitunix view payload for the cockpit Bitunix tab (display only). Built where the BX files live: in the
Singapore bx-exec service when BX_SERVICE_URL is set (the cockpit proxies it), else in the cockpit."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

KEEP = ("symbol", "bx_symbol", "ex", "asset_class", "close", "price", "trend", "filter", "upper", "lower",
        "dual_cross_up", "above_upper", "bar_time", "vol24h_usd", "ign_x", "ign", "spread_bp", "liq_tier",
        "contract_age_days", "new_contract", "asset_age", "cat_tags", "drop_from_ath_pct", "tier", "gc_tf",
        "session_gap", "narrative")


def _read(out_dir: Path, name: str) -> dict:
    try:
        return json.loads((out_dir / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def build(out_dir: Path) -> Dict[str, Any]:
    meta = _read(out_dir, "bx_meta.json")
    out: Dict[str, Any] = {
        "ok": bool(meta), "ts": meta.get("ts"), "counts": meta.get("counts"),
        "n_contracts": meta.get("n_contracts"), "n_scanned": meta.get("n_scanned"),
        "review": (meta.get("review") or [])[:200],
        # HL names also listed on Bitunix -> the Bitunix tab labels those HL rows HL+BX
        "overlap": sorted({r.get("hl_name") for r in meta.get("catalog") or [] if r.get("ex") == "HL+BX" and r.get("hl_name")}),
        "served_from": os.environ.get("RAILWAY_REPLICA_REGION") or "local",
    }
    for tf in ("1d", "4h", "1h"):
        r = _read(out_dir, f"bx_radar_{tf}.json")
        out[f"radar_{tf}"] = {"ts": r.get("ts"), "breadth": r.get("breadth"),
                              "rows": [{k: x.get(k) for k in KEEP} for x in r.get("rows") or []]}
    tf_r = _read(out_dir, "bx_tradfi_radar.json")
    out["tradfi"] = {"ts": tf_r.get("ts"), "rows": [{k: x.get(k) for k in KEEP + ("tf",)} for x in tf_r.get("rows") or []]}
    try:
        import bx_live
        import bx_shadow
        conn = bx_live.connect()
        try:
            out["shadow"] = {"compare": bx_shadow.compare(conn),
                             "open": [dict(r) for r in conn.execute(
                                 "SELECT symbol, kind, gc_tf, counted, mode, entry_time, entry_px, size_pct_nav, hard_sl, "
                                 "exit_rule FROM shadow_trades WHERE status='open' ORDER BY entry_time DESC")],
                             "closed": [dict(r) for r in conn.execute(
                                 "SELECT symbol, kind, gc_tf, counted, mode, entry_time, exit_time, exit_reason, ret_pct, "
                                 "pnl_nav_pct, pnl_usd FROM shadow_trades WHERE status='closed' ORDER BY exit_time DESC LIMIT 20")]}
            out["live"] = bx_live.status(conn)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        out["shadow"] = {"error": str(e)[:200]}
    return out
