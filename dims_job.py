#!/usr/bin/env python3
"""Dimensions worker (run by serve.py as a subprocess, like executor.py). SHADOW ONLY — never trades.

  python dims_job.py snapshot [--no-whales]   08:08 HKT: whales + HL ctx + score today's candidates
                                              -> out/dimensions_latest.json + ledger rows
  python dims_job.py outcomes                 08:30 HKT: fill forward outcomes from closed 1D bars
  python dims_job.py report [--since YYYY-MM-DD] [--type Base|Chase]
                                              -> out/dimensions_report.json

Prints one JSON object to stdout (serve.py parses it for the job status line).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import dim_ledger as L  # noqa: E402
import dimensions as D  # noqa: E402

OUT_DIR = os.environ.get("OTR_OUT_DIR") or str(ROOT / "out")
HL_ADDRESS = (os.environ.get("HL_ADDRESS") or os.environ.get("HL_WALLET") or "").strip()
try:
    from exec_common import SOT_ID  # noqa: E402
except Exception:  # pragma: no cover
    SOT_ID = os.environ.get("SOT_ID", "")
WAIT_CANDIDATES_S = int(os.environ.get("DIMS_WAIT_CANDIDATES_S", "600"))


def _read(name: str):
    try:
        with open(os.path.join(OUT_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _bars(tf: str = "1d") -> dict:
    try:
        with gzip.open(os.path.join(OUT_DIR, f"candles_{tf}.json.gz"), "rt", encoding="utf-8") as f:
            return (json.load(f) or {}).get("bars") or {}
    except (OSError, ValueError):
        return {}


def _hkt(dt: datetime) -> str:
    return (dt + timedelta(hours=8)).strftime("%Y-%m-%d")


def _parse(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _hl_post():
    import scan_gc_radar as sgr  # retries + backoff already built in
    return sgr.hl_post


def _atomic(name: str, obj) -> None:
    p = os.path.join(OUT_DIR, name)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"), default=str)
    os.replace(p + ".tmp", p)


def snapshot(no_whales: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    today = _hkt(now)
    # wait for today's 08:05 candidates (scan can take a few minutes)
    deadline = time.time() + WAIT_CANDIDATES_S
    while True:
        cd = _read("entry_candidates_latest.json") or {}
        gen = _parse(cd.get("generated_at"))
        if gen and _hkt(gen) == today:
            break
        if time.time() > deadline:
            return {"status": "skipped", "message": "no candidates generated today (HKT)"}
        time.sleep(20)

    radar = {tf: _read(f"gc_radar_{tf}.json") or {} for tf in ("1d", "4h", "1h")}
    bars_1d = _bars("1d")
    hl_post = _hl_post()
    errors = []
    try:
        ctxs = D.parse_asset_ctxs(hl_post({"type": "metaAndAssetCtxs"}))
    except Exception as e:
        ctxs = {}
        errors.append(f"metaAndAssetCtxs: {e}")
    perp = {}
    if HL_ADDRESS:
        try:
            perp = hl_post({"type": "clearinghouseState", "user": HL_ADDRESS}) or {}
        except Exception as e:
            errors.append(f"clearinghouseState: {e}")

    whale_coins = prev_whale = whales_meta = None
    if not no_whales:
        try:
            import whales as W
            snap = W.build_snapshot(hl_post, OUT_DIR, now)
            prev = W.load_previous(OUT_DIR, snap["hkt_day"])
            whale_coins = snap.get("coins") or {}
            prev_whale = (prev or {}).get("coins")
            whales_meta = {"n_wallets": snap.get("n_wallets"), "n_manual": snap.get("n_manual"),
                           "ts": snap.get("ts"), "errors": (snap.get("errors") or [])[:5]}
        except Exception as e:
            errors.append(f"whales: {e}")

    conn = L.connect()
    prev_ctx = L.previous_market_ctx(conn, today)
    if ctxs:
        L.record_market_ctx(conn, today, ctxs)

    # executor-style Hard SL on each candidate (tier rule) so outcomes can test SL hits
    try:
        from exec_common import hard_sl_for_tier
    except Exception:
        hard_sl_for_tier = None
    cands = []
    for c in cd.get("candidates") or []:
        c = dict(c)
        if hard_sl_for_tier and c.get("hard_sl") is None:
            try:
                c["hard_sl"], _ = hard_sl_for_tier(c.get("tier", ""), {"lower": c.get("lower_4h"), "filter": c.get("filter_4h")})
            except Exception:
                pass
        cands.append(c)

    result = D.compute_all(
        cands, radar_1d=radar["1d"], radar_4h=radar["4h"], radar_1h=radar["1h"], bars_1d=bars_1d,
        asset_ctxs=ctxs, prev_ctxs=prev_ctx, narrative_wl=_read("narrative_watchlist.json") or {},
        perp_state=perp, whale_coins=whale_coins, prev_whale_coins=prev_whale,
        now_ms=int(now.timestamp() * 1000))
    result.update(generated_at=now.isoformat(), signal_date=today, whales_meta=whales_meta,
                  candidates_generated_at=cd.get("generated_at"), errors=errors)

    bar_times = {r.get("symbol"): r.get("bar_time") for r in radar["1d"].get("rows") or []}
    ids = L.record_signals(conn, today, cands, result, bar_times, sot=SOT_ID)
    conn.close()
    _atomic("dimensions_latest.json", result)
    top = [f"{c['symbol']}:{c['total']:+d}" for c in result["candidates"][:8]]
    return {"status": "success", "signal_date": today, "candidates": result["n"], "ledger_rows": len(ids),
            "whales": whales_meta, "errors": errors, "top": top}


def outcomes() -> dict:
    now_ms = int(time.time() * 1000)
    hl_post = None

    def fetch(sym: str, start_ms: int):
        nonlocal hl_post
        hl_post = hl_post or _hl_post()
        raw = hl_post({"type": "candleSnapshot", "req": {"coin": sym, "interval": "1d",
                                                          "startTime": start_ms, "endTime": now_ms}})
        return [[int(b["t"]), float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b.get("v") or 0)]
                for b in raw or []]

    conn = L.connect()
    stats = L.fill_outcomes(conn, _bars("1d"), now_ms, fetch_bars=fetch)
    conn.close()
    return {"status": "success", **stats}


def report(since=None, sig_type=None) -> dict:
    conn = L.connect()
    rep = L.report(conn, since=since, sig_type=sig_type)
    conn.close()
    _atomic("dimensions_report.json", rep)
    return {"status": "success", "signals": rep["signals_total"], "with_outcome": rep["signals_with_outcome"],
            "top_dims": [(d["dim"], d["ic"], d["verdict"]) for d in rep["dimensions"][:5]]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["snapshot", "outcomes", "report"])
    ap.add_argument("--no-whales", action="store_true")
    ap.add_argument("--since")
    ap.add_argument("--type", dest="sig_type")
    a = ap.parse_args()
    try:
        if a.mode == "snapshot":
            res = snapshot(a.no_whales)
        elif a.mode == "outcomes":
            res = outcomes()
        else:
            res = report(a.since, a.sig_type)
    except Exception as e:  # never crash silently: report as error
        res = {"status": "error", "message": f"{type(e).__name__}: {e}"}
    print(json.dumps(res, default=str))
    return 0 if res.get("status") in ("success", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
