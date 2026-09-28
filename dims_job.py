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


def backfill(days: int = 150) -> dict:
    """Historical replay (shadow): rebuild the 08:05 Base/Chase candidates for each past day from the
    closed-bar candle caches (1D ~280 bars, 4H ~450 bars), score the dimensions that can be rebuilt
    without look-ahead (trend w/o 1H, extension, rel_strength, liquidity, btc_regime), then fill 7-day
    outcomes. Written to a SEPARATE ledger (giiq_ledger_backfill.db) so it never touches live rows.
    Not reconstructable (skipped): crowding (funding history), narrative, concentration, smart_money."""
    import scan_gc_radar as sgr
    try:
        from mcap_tiers import tier_for
    except Exception:  # pragma: no cover
        tier_for = lambda s: "unknown"  # noqa: E731
    from exec_common import hard_sl_for_tier
    DAY, H4 = 86_400_000, 14_400_000
    now_ms = int(time.time() * 1000)
    b1d, b4h = _bars("1d"), _bars("4h")
    if not b1d:
        return {"status": "error", "message": "no 1D candle cache on the volume"}

    def series(bars, period, bar_ms):
        rows = sorted((b for b in bars or [] if int(b[0]) + bar_ms <= now_ms), key=lambda b: b[0])
        if len(rows) < period + 20:
            return None
        h = [float(b[2]) for b in rows]
        lo = [float(b[3]) for b in rows]
        c = [float(b[4]) for b in rows]
        return {"t": [int(b[0]) for b in rows], "h": h, "l": lo, "c": c, "v": [float(b[5] or 0) for b in rows],
                "gc": sgr.compute_gc(h, lo, c, period=period), "bars": rows,
                "idx": {int(b[0]): i for i, b in enumerate(rows)}}

    s1 = {k: x for k, v in b1d.items() if (x := series(v, sgr.gc_period_for_tf("1d"), DAY))}
    s4 = {k: x for k, v in b4h.items() if (x := series(v, sgr.gc_period_for_tf("4h"), H4))}
    warm1, warm4 = 150, 80  # skip IIR warm-up bars

    def state(S, i):
        g, gp, c = S["gc"][i], S["gc"][i - 1], S["c"]
        return {"close": c[i], "filter": g["filter"], "upper": g["upper"], "lower": g["lower"],
                "trend": "Green" if g["filter"] > gp["filter"] else "Red",
                "dcu": c[i] > g["upper"] and c[i - 1] <= gp["upper"]}

    btc1, btc4 = s1.get("BTC"), s4.get("BTC")
    all_t = sorted({t for S in s1.values() for t in S["t"]})
    day_ts = [t for t in all_t if t + DAY <= now_ms][-days:]
    conn = L.connect(L.backfill_db_path())
    n_sig = 0
    for t1 in day_ts:
        signal_date = datetime.fromtimestamp((t1 + DAY) / 1000, timezone.utc).strftime("%Y-%m-%d")
        t4 = t1 + DAY - H4  # the 4H bar that closes at the 08:00 HKT 1D close
        rad1 = {"rows": []}
        rad4 = {"rows": []}
        if btc1 and t1 in btc1["idx"] and btc1["idx"][t1] >= warm1:
            b = state(btc1, btc1["idx"][t1])
            rad1["rows"].append({"symbol": "BTC", "close": b["close"], "filter": b["filter"]})
        if btc4 and t4 in btc4["idx"] and btc4["idx"][t4] >= warm4:
            b = state(btc4, btc4["idx"][t4])
            rad4["rows"].append({"symbol": "BTC", "close": b["close"], "filter": b["filter"]})
        regime = D.btc_regime(rad1, rad4)
        cands, scored = [], []
        for sym, S in s1.items():
            i = S["idx"].get(t1)
            if i is None or i < warm1:
                continue
            d1 = state(S, i)
            S4 = s4.get(sym)
            j = S4["idx"].get(t4) if S4 else None
            d4 = state(S4, j) if (S4 and j is not None and j >= warm4) else None
            base = d1["dcu"] and d1["trend"] == "Green"
            chase = bool(d4) and d1["trend"] == "Green" and d4["trend"] == "Green" and d4["dcu"]
            if not base and not chase:
                continue
            tier = tier_for(sym) or "unknown"
            cand = {"symbol": sym, "type": "Chase" if chase else "Base", "is_base": base, "is_chase": chase,
                    "tier": tier, "trend_1d": d1["trend"], "trend_4h": d4["trend"] if d4 else "",
                    "close_1d": d1["close"], "upper_1d": d1["upper"], "filter_1d": d1["filter"],
                    "lower_1d": d1["lower"], "close_4h": d4["close"] if d4 else None,
                    "upper_4h": d4["upper"] if d4 else None, "filter_4h": d4["filter"] if d4 else None,
                    "lower_4h": d4["lower"] if d4 else None, "dayNtlVlm": S["v"][i] * S["c"][i]}
            cand["entry_ref"] = cand["upper_4h"] if chase else cand["upper_1d"]
            if d4:
                cand["hard_sl"], _ = hard_sl_for_tier(tier, {"lower": d4["lower"], "filter": d4["filter"]})
            dims = {
                "trend": D.score_trend(cand, None),
                "extension": D.score_extension(cand),
                "rel_strength": D.score_rel_strength(S["bars"][: i + 1],
                                                     btc1["bars"][: btc1["idx"][t1] + 1] if btc1 and t1 in btc1["idx"] else None,
                                                     t1 + DAY),
                "liquidity": D.score_liquidity({"day_ntl_vlm": cand["dayNtlVlm"]}, cand),
                "btc_regime": regime,
            }
            cands.append(cand)
            sc = [d["score"] for d in dims.values() if d["score"] is not None]
            scored.append({"symbol": sym, "dims": dims, "total": sum(sc)})
        if cands:
            L.record_signals(conn, signal_date, cands, {"dim_version": D.DIM_VERSION + "-backfill", "candidates": scored},
                             {c["symbol"]: t1 for c in cands}, sot="backfill")
            n_sig += len(cands)
    stats = L.fill_outcomes(conn, b1d, now_ms)
    rep = L.report(conn)
    rep["note"] = ("Historical replay from the Railway candle caches. Dimensions rebuilt without look-ahead: trend "
                   "(1D+4H alignment, no 1H), extension, rel_strength, liquidity, btc_regime. Not rebuildable: crowding, "
                   "narrative, concentration, smart_money (live data only). Outcome = 7 closed days from the signal "
                   "close or the tier Hard SL if hit first (Chase is a proxy: live Chase waits for a pullback).")
    conn.close()
    _atomic("dimensions_report_backfill.json", rep)
    return {"status": "success", "days": len(day_ts), "signals": n_sig, "outcomes": stats,
            "with_outcome": rep["signals_with_outcome"],
            "top_dims": [(d["dim"], d["n"], d["ic"], d["veto_lift"], d["verdict"]) for d in rep["dimensions"]]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["snapshot", "outcomes", "report", "backfill"])
    ap.add_argument("--days", type=int, default=150)
    ap.add_argument("--no-whales", action="store_true")
    ap.add_argument("--since")
    ap.add_argument("--type", dest="sig_type")
    a = ap.parse_args()
    try:
        if a.mode == "snapshot":
            res = snapshot(a.no_whales)
        elif a.mode == "outcomes":
            res = outcomes()
        elif a.mode == "backfill":
            res = backfill(a.days)
        else:
            res = report(a.since, a.sig_type)
    except Exception as e:  # never crash silently: report as error
        res = {"status": "error", "message": f"{type(e).__name__}: {e}"}
    print(json.dumps(res, default=str))
    return 0 if res.get("status") in ("success", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
