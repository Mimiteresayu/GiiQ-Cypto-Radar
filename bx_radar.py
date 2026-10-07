#!/usr/bin/env python3
"""Bitunix (BX) shadow radar — catalog, enrichment, GC scan. DISPLAY + SHADOW ONLY.

Harbor-approved 2026-09-29. Hard boundary: this job never places orders, never needs a Bitunix key and
writes only bx_* / cg_* files under out/. The HL order path (executor.py, pending_worker.py,
exit_worker.py, entry_candidates.py, hl_exec.py) never reads them (test_bx_isolation.py).

Jobs (serve.py, own lock, hard timeout):
  daily  08:20 HKT  catalog + tickers + HL mids + CoinGecko -> classify / match / tier;
                    1D + 4H candles for the scan set -> bx_radar_1d/4h.json, bx_tradfi_radar.json, bx_meta.json
  4h     :25        4H candles -> bx_radar_4h.json (Chase, 4H exits, 4H new-token signals)
  hourly :27        1H candles for 1H-GC new tokens + open shadow positions -> bx_radar_1h.json

Rows reuse the HL radar row fields (symbol, close, filter, upper, lower, trend, above_upper, dual_cross_up,
dual_cross_down_filter, bar_time, bars, tier, cat_tags, drop_from_ath_pct ...) plus the BX fields in
bx_universe (ex, bx_symbol, hl_name, asset_class, contract_age_days, asset_age, ign_x, spread_bp, liq_tier, gc_tf ...).
GC math = scan_gc_radar.compute_gc with the locked per-TF periods (1D 144, 4H 72, 1H 48), closed bars only.
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import bx_universe as U  # noqa: E402

OUT_DIR = Path(os.environ.get("BX_OUT_DIR") or (ROOT / "out"))
DATA_DIR = ROOT / "data"
BAR_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
HIST_BARS = {"1d": 300, "4h": 200, "1h": 200}   # 1D: 280 for a partial ATH + GC warm-up
CEMETERY_DROP_ATH_PCT = 70.0                     # same threshold as scan_gc_radar
PARTIAL_ATH_MIN_BARS = 280                       # partial (BX-only) ATH may tag C only with this much history
SCHEMA = "bx-radar-v1"


# ---------------------------------------------------------------------------------------------
# small io helpers
# ---------------------------------------------------------------------------------------------
def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def candles_path(tf: str) -> Path:
    return OUT_DIR / f"bx_candles_{tf}.json.gz"


def load_candles(tf: str) -> Dict[str, List[list]]:
    try:
        with gzip.open(candles_path(tf), "rt", encoding="utf-8") as f:
            return (json.load(f) or {}).get("bars") or {}
    except (OSError, json.JSONDecodeError, EOFError):
        return {}


def save_candles(tf: str, bars: Dict[str, List[list]]) -> None:
    p = candles_path(tf)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(p) + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump({"tf": tf, "ts": datetime.now(timezone.utc).isoformat(), "bars": bars}, f, separators=(",", ":"))
    os.replace(tmp, p)


def radar_path(tf: str) -> Path:
    return OUT_DIR / f"bx_radar_{tf}.json"


def load_radar(tf: str) -> dict:
    return _read_json(radar_path(tf))


def load_meta() -> dict:
    return _read_json(OUT_DIR / "bx_meta.json")


def _data_file(name: str) -> dict:
    d = _read_json(DATA_DIR / name)
    return {k: v for k, v in d.items() if not str(k).startswith("_")}


# ---------------------------------------------------------------------------------------------
# GC row on BX bars (closed bars only) — same fields and flags as scan_gc_radar.scan_symbol
# ---------------------------------------------------------------------------------------------
def gc_row(symbol: str, bars: List[list], tf: str, now_ms: int, gc_fn: Callable) -> Optional[dict]:
    """bars = [t,o,h,l,c,v] oldest first. None if fewer than period+20 CLOSED bars."""
    closed = [b for b in bars if int(b[0]) + BAR_MS[tf] <= now_ms]
    if len(closed) < U.min_bars(tf):
        return None
    highs = [float(b[2]) for b in closed]
    lows = [float(b[3]) for b in closed]
    closes = [float(b[4]) for b in closed]
    gc = gc_fn(highs, lows, closes, period=U.GC_PERIOD[tf])
    i = len(closed) - 1
    filt, pfilt = gc[i]["filter"], gc[i - 1]["filter"]
    up, pup, lo = gc[i]["upper"], gc[i - 1]["upper"], gc[i]["lower"]
    c, pc = closes[i], closes[i - 1]
    last_cross = None
    for j in range(i, 0, -1):
        if closes[j - 1] <= gc[j - 1]["upper"] and closes[j] > gc[j]["upper"]:
            last_cross = int(closed[j][0])
            break
    return {
        "symbol": symbol, "tf": tf,
        "close": round(c, 10), "high": round(highs[i], 10), "low": round(lows[i], 10),
        "filter": round(filt, 10), "upper": round(up, 10), "lower": round(lo, 10),
        "trend": "Green" if filt > pfilt else "Red",
        "above_upper": c > up,
        "dual_cross_up": c > up and pc <= pup,                      # LOCKED entry math
        "dual_cross_down_filter": c < filt and pc >= pfilt,
        "last_cross_up_at": last_cross,
        "bar_time": int(closed[i][0]), "bars": len(closed),
        "bx_high_max": round(max(highs), 10),
    }


# ---------------------------------------------------------------------------------------------
# Candle fetch with incremental cache
# ---------------------------------------------------------------------------------------------
def refresh_candles(tf: str, symbols: List[str], client, now_ms: int, log=None) -> Dict[str, Any]:
    """Update the tf cache for `symbols` (others are kept). One page per symbol when the cache is warm,
    paged history on first sight. Returns {ok, failed:[...], calls}."""
    cache = load_candles(tf)
    failed: List[str] = []
    for s in symbols:
        have = cache.get(s) or []
        try:
            if have and now_ms - int(have[-1][0]) < 150 * BAR_MS[tf]:
                # warm cache: one page from the last cached bar (<= 200 bars covers any gap we allow)
                page = client.klines(s, tf, start_ms=int(have[-1][0]), limit=client.KLINE_MAX)
                merged = {int(b[0]): b for b in have}
                for b in page:
                    merged[int(b[0])] = b
                cache[s] = [merged[t] for t in sorted(merged)][-HIST_BARS[tf]:]
            else:
                cache[s] = client.klines_history(s, tf, HIST_BARS[tf], now_ms=now_ms)
        except Exception as e:  # noqa: BLE001 - one bad symbol never stops the run
            failed.append(s)
            if log:
                log(f"[BX] {tf} candles {s} failed: {str(e)[:120]}")
    save_candles(tf, cache)
    return {"ok": len(failed) < max(3, len(symbols) // 10), "failed": failed, "n": len(symbols)}


# ---------------------------------------------------------------------------------------------
# Catalog + classification (daily)
# ---------------------------------------------------------------------------------------------
def _hl_first_seen(hl_name: Optional[str]) -> Optional[int]:
    """First HL 1D bar we know of for a matched coin (from the HL radar's bar count)."""
    if not hl_name:
        return None
    radar = _read_json(OUT_DIR / "gc_radar_1d.json")
    for r in radar.get("rows") or []:
        if r.get("symbol") == hl_name and r.get("bar_time") and r.get("bars"):
            return int(r["bar_time"]) - (int(r["bars"]) - 1) * BAR_MS["1d"]
    return None


def build_catalog(pairs: List[dict], tickers: List[dict], hl_mids: Dict[str, float], cg_coins: List[dict],
                  now_ms: int, overrides: Optional[Dict[str, str]] = None,
                  seed_class: Optional[Dict[str, str]] = None, narrative: Optional[set] = None,
                  first_seen_fn: Optional[Callable[[str], Optional[int]]] = None) -> Dict[str, Any]:
    """Pure: one meta row per USDT contract (USDC duplicates of the same base dropped).
    Spread / ignition / candle-derived fields are filled later by enrich_row."""
    from cg_client import by_symbol
    tick = {t.get("symbol"): t for t in tickers or []}
    cg_idx = by_symbol(cg_coins or [])
    narrative = {s.upper() for s in (narrative or set())}
    rows: List[dict] = []
    review: List[dict] = []
    seen_base: set = set()
    for p in sorted(pairs or [], key=lambda x: (x.get("quote") != "USDT", x.get("symbol") or "")):
        sym, quote = p.get("symbol"), p.get("quote")
        # the contract unit lives in the SYMBOL (1000PEPEUSDC has base "PEPE"), so parse it from there
        base = (sym[: -len(quote)] if sym and quote and sym.endswith(quote) else str(p.get("base") or "")).upper()
        if not sym or not base or quote not in ("USDT", "USDC") or base in seen_base:
            continue
        seen_base.add(base)
        t = tick.get(sym) or {}
        price = U._f(t.get("lastPrice")) or U._f(t.get("markPrice"))
        from bx_client import usd_volume
        vol = usd_volume(t.get("quoteVol"), t.get("baseVol"), price)
        m = U.match_hl(base, price, hl_mids, overrides)
        cg = U.cg_match(base, price, cg_idx)
        cls = U.asset_class(base, seed_class or {}, bool(m["hl_name"]), bool(cg))
        launch = p.get("launchTime")
        launch = int(launch) if launch not in (None, "", 0) else None
        delist = p.get("delistTime")
        delist = int(delist) if delist not in (None, "", 0) else None
        core, mult = U.split_multiplier(base)
        ext_first: List[Optional[int]] = [_hl_first_seen(m["hl_name"])]
        new_contract = bool(launch and now_ms - launch < U.NEW_CONTRACT_DAYS * U.DAY_MS)
        if new_contract and cg and first_seen_fn:
            ext_first.append(first_seen_fn(cg["id"]))
        age = U.listing_age(launch, None, now_ms, cls, ext_first)
        ath = U._f((cg or {}).get("ath"))
        row = {
            "symbol": base, "bx_symbol": sym, "quote": quote,
            "ex": U.ex_label(bool(m["hl_name"]), True), "hl_name": m["hl_name"],
            "match_rule": m["match_rule"], "px_scale": m["px_scale"], "px_diff": m["px_diff"],
            "asset_class": cls, "symbol_status": p.get("symbolStatus"), "launch_time": launch,
            "delist_time": delist, "max_leverage": p.get("maxLeverage"),
            "base_precision": p.get("basePrecision"), "quote_precision": p.get("quotePrecision"),
            "min_qty": p.get("minTradeVolume"), "api_supported": p.get("isApiSupported"),
            "price": price, "vol24h_usd": round(vol, 2) if vol else None,
            "cg_id": (cg or {}).get("id"), "mcap_usd": U._f((cg or {}).get("market_cap")),
            # CoinGecko market cap is per coin; a 1000x contract's ATH is per 1000 coins
            "ath_usd": round(ath * mult, 12) if ath else None, "ath_src": "cg" if ath else None,
            "narrative": core in narrative or base in narrative,
            **age,
        }
        rows.append(row)
        if cls == "unknown" or m["match_rule"] == "price_mismatch":
            review.append({"symbol": base, "bx_symbol": sym, "reason": "unknown class" if cls == "unknown"
                           else f"HL name found but price differs ({m['match_rule']})",
                           "price": price, "vol24h_usd": row["vol24h_usd"]})
    return {"rows": rows, "review": review}


def scan_set(meta_rows: List[dict]) -> List[dict]:
    """Contracts we pull candles for: BX-only crypto + all TradFi, OPEN, not delisting, not unknown class,
    and either >= $0.2M 24h volume or a new contract / cemetery candidate (ignition needs their candles)."""
    out = []
    for r in meta_rows:
        if r["ex"] != "BX" or r["asset_class"] == "unknown":
            continue
        if str(r.get("symbol_status") or "").upper() != "OPEN" or r.get("delist_time"):
            continue
        cem_cand = bool(r.get("ath_usd") and r.get("price")
                        and (r["ath_usd"] - r["price"]) / r["ath_usd"] * 100 >= CEMETERY_DROP_ATH_PCT)
        if (r.get("vol24h_usd") or 0) >= U.VOL_WATCH_MIN or r.get("new_contract") or cem_cand:
            out.append(r)
    return out


def enrich_row(meta: dict, bars_1d: List[list], spread: Optional[float], now_ms: int) -> dict:
    """Candle-derived fields: ignition, cemetery (C), CAT codes, liquidity tier, tier, max notional."""
    closed = [b for b in bars_1d or [] if int(b[0]) + BAR_MS["1d"] <= now_ms]
    prior_vols = [float(b[5]) for b in closed[-U.IGNITION_LOOKBACK:]]
    ign = U.ignition(meta.get("vol24h_usd"), prior_vols)
    price = meta.get("price")
    ath, src = meta.get("ath_usd"), meta.get("ath_src")
    if not ath and closed:
        ath, src = max(float(b[2]) for b in closed), "partial"
    drop = round((ath - price) / ath * 100, 2) if ath and price else None
    cem = bool(drop is not None and drop >= CEMETERY_DROP_ATH_PCT
               and (src == "cg" or len(closed) >= PARTIAL_ATH_MIN_BARS))
    new_tok = U.is_new_token(meta.get("asset_age") or "")
    tier = U.liq_tier(meta.get("vol24h_usd"), meta.get("symbol_status"), meta.get("delist_time"),
                      meta.get("asset_class"), spread, ign, cem, new_tok)
    cats = [c for c, on in (("N", meta.get("narrative")), ("C", cem),
                            ("V", (meta.get("vol24h_usd") or 0) >= U.VOL_WATCH_MIN)) if on]
    try:
        from mcap_tiers import tier_from_mcap
        size_tier = tier_from_mcap(meta.get("mcap_usd"))
    except Exception:  # pragma: no cover
        size_tier = "tiny"
    first_bar = int(closed[0][0]) if closed else None
    age_days = meta.get("contract_age_days")
    if age_days is None and first_bar:
        age_days = int((now_ms - first_bar) // U.DAY_MS)
    return dict(meta, ign_x=ign, ign=bool(ign is not None and ign >= U.IGNITION_X),
                spread_bp=spread, liq_tier=tier, drop_from_ath_pct=drop, ath_usd=ath, ath_src=src,
                cemetery=cem, cat_tags=" ".join(cats), tier=size_tier,
                max_notional_usd=U.max_notional_usd(meta.get("vol24h_usd")),
                contract_age_days=age_days,
                session_gap=U.session_gap(meta.get("asset_class"), int(closed[-1][0]), "1d") if closed else False)


# ---------------------------------------------------------------------------------------------
# Radar files
# ---------------------------------------------------------------------------------------------
def build_radar(tf: str, metas: List[dict], bars: Dict[str, List[list]], now_ms: int, gc_fn: Callable,
                tradfi: bool = False) -> dict:
    rows = []
    for m in metas:
        if (m["asset_class"] in U.TRADFI) != tradfi:
            continue
        r = gc_row(m["symbol"], bars.get(m["bx_symbol"]) or [], tf, now_ms, gc_fn)
        if not r:
            continue
        r = dict(m, **r)
        if tradfi:
            r["session_gap"] = U.session_gap(m["asset_class"], r["bar_time"], tf)
        rows.append(r)
    rows.sort(key=lambda r: (not r.get("dual_cross_up"), -(r.get("vol24h_usd") or 0)))
    green = sum(1 for r in rows if r.get("trend") == "Green")
    return {"schema": SCHEMA, "source": "bitunix", "tf": tf, "tradfi": tradfi,
            "ts": datetime.now(timezone.utc).isoformat(), "n": len(rows),
            "breadth": {"green_count": green, "n": len(rows),
                        "green_pct": round(100.0 * green / len(rows), 2) if rows else 0.0},
            "display_only": True, "rows": rows}


def gc_tf_by_symbol(metas: List[dict], now_ms: int) -> Dict[str, Optional[str]]:
    """Per contract: the longest GC TF its closed history supports (1d > 4h > 1h)."""
    caches = {tf: load_candles(tf) for tf in ("1d", "4h", "1h")}
    out = {}
    for m in metas:
        n = {tf: sum(1 for b in caches[tf].get(m["bx_symbol"]) or [] if int(b[0]) + BAR_MS[tf] <= now_ms)
             for tf in ("1d", "4h", "1h")}
        out[m["symbol"]] = U.gc_tf_for(n)
    return out


# ---------------------------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------------------------
def _log(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def run_daily(now_ms: Optional[int] = None, client=None, hl_mids_fn=None, cg=None) -> Dict[str, Any]:
    """08:20 HKT: catalog + enrichment + 1D and 4H scans. Returns a status dict (never raises)."""
    t0 = time.time()
    now_ms = now_ms or int(time.time() * 1000)
    import bx_client
    client = client or bx_client
    try:
        pairs, tickers = client.trading_pairs(), client.tickers()
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "message": f"Bitunix catalog unreachable: {e}"}
    try:
        if hl_mids_fn is None:
            from live_radar import fetch_all_mids, hl_perp_names
            mids = fetch_all_mids()
            hl_mids = {k: mids[k] for k in hl_perp_names(mids)}
        else:
            hl_mids = hl_mids_fn()
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "message": f"HL mids unavailable (needed for BX<->HL match): {e}"}
    if cg is None:
        import cg_client as cg
    mk = cg.markets()
    try:
        import scan_gc_radar as sgr
        narrative = sgr.load_narrative_tickers()
        gc_fn = sgr.compute_gc
    except Exception:  # pragma: no cover
        narrative, gc_fn = set(), None
    cat = build_catalog(pairs, tickers, hl_mids, mk.get("coins") or [], now_ms,
                        overrides=_data_file("bx_symbol_map.json"), seed_class=_data_file("bx_asset_class.json"),
                        narrative=narrative, first_seen_fn=cg.first_seen_ms)
    todo = scan_set(cat["rows"])
    syms = [m["bx_symbol"] for m in todo]
    r1d = refresh_candles("1d", syms, client, now_ms, _log)
    r4h = refresh_candles("4h", syms, client, now_ms, _log)
    bars_1d = load_candles("1d")
    spreads: Dict[str, Optional[float]] = {}
    for m in todo:
        if (m.get("vol24h_usd") or 0) >= U.VOL_TRADEABLE:
            try:
                spreads[m["bx_symbol"]] = client.spread_bp(client.depth(m["bx_symbol"], 5))
            except Exception:  # noqa: BLE001
                spreads[m["bx_symbol"]] = None
    metas = [enrich_row(m, bars_1d.get(m["bx_symbol"]) or [], spreads.get(m["bx_symbol"]), now_ms) for m in todo]
    gtf = gc_tf_by_symbol(metas, now_ms)
    for m in metas:
        m["gc_tf"] = gtf.get(m["symbol"])
    radar_1d = build_radar("1d", metas, bars_1d, now_ms, gc_fn)
    radar_4h = build_radar("4h", metas, load_candles("4h"), now_ms, gc_fn)
    tradfi = build_radar("1d", metas, bars_1d, now_ms, gc_fn, tradfi=True)
    tradfi_4h = build_radar("4h", metas, load_candles("4h"), now_ms, gc_fn, tradfi=True)
    have_1d = {r["symbol"] for r in tradfi["rows"]}
    tradfi["rows"] += [r for r in tradfi_4h["rows"] if r["symbol"] not in have_1d]  # young TradFi contracts
    tradfi["n"] = len(tradfi["rows"])
    _write_json(radar_path("1d"), radar_1d)
    _write_json(radar_path("4h"), radar_4h)
    _write_json(OUT_DIR / "bx_tradfi_radar.json", tradfi)
    counts: Dict[str, int] = {}
    for r in cat["rows"]:
        counts[f"ex_{r['ex']}"] = counts.get(f"ex_{r['ex']}", 0) + 1
        counts[f"class_{r['asset_class']}"] = counts.get(f"class_{r['asset_class']}", 0) + 1
    for m in metas:
        counts[f"tier_{m['liq_tier']}"] = counts.get(f"tier_{m['liq_tier']}", 0) + 1
    meta = {"schema": SCHEMA, "ts": datetime.now(timezone.utc).isoformat(), "counts": counts,
            "n_contracts": len(cat["rows"]), "n_scanned": len(metas), "cg_error": mk.get("error"),
            "cg_coins": len(mk.get("coins") or []), "review": cat["review"],
            "catalog": cat["rows"], "scanned": metas,
            "candles": {"1d": {k: v for k, v in r1d.items() if k != "failed"} | {"failed": r1d["failed"][:20]},
                        "4h": {k: v for k, v in r4h.items() if k != "failed"} | {"failed": r4h["failed"][:20]}},
            "api": dict(getattr(client, "STATS", {}) or {}), "elapsed_s": round(time.time() - t0, 1)}
    _write_json(OUT_DIR / "bx_meta.json", meta)
    ok = r1d["ok"] and r4h["ok"]
    return {"status": "success" if ok else "partial", "n_contracts": len(cat["rows"]), "n_scanned": len(metas),
            "n_1d": radar_1d["n"], "n_4h": radar_4h["n"], "n_tradfi": tradfi["n"], "review": len(cat["review"]),
            "counts": counts, "elapsed_s": meta["elapsed_s"], "api": meta["api"],
            "message": None if ok else f"candle failures 1d={len(r1d['failed'])} 4h={len(r4h['failed'])}"}


def run_tf(tf: str, symbols: Optional[List[str]] = None, now_ms: Optional[int] = None, client=None) -> Dict[str, Any]:
    """4H (:25) or 1H (:27) refresh for the scan set from the last daily run (or given BX symbols)."""
    t0 = time.time()
    now_ms = now_ms or int(time.time() * 1000)
    import bx_client
    client = client or bx_client
    meta = load_meta()
    metas = meta.get("scanned") or []
    if not metas:
        return {"status": "skipped", "message": "no daily BX catalog yet"}
    if symbols is not None:
        want = set(symbols)
        metas_tf = [m for m in metas if m["bx_symbol"] in want]
    else:
        metas_tf = metas
    if not metas_tf:
        return {"status": "success", "message": "nothing to refresh", "n": 0}
    res = refresh_candles(tf, [m["bx_symbol"] for m in metas_tf], client, now_ms, _log)
    import scan_gc_radar as sgr
    gtf = gc_tf_by_symbol(metas, now_ms)
    for m in metas:
        m["gc_tf"] = gtf.get(m["symbol"])
    radar = build_radar(tf, metas_tf if tf == "1h" else metas, load_candles(tf), now_ms, sgr.compute_gc)
    _write_json(radar_path(tf), radar)
    return {"status": "success" if res["ok"] else "partial", "tf": tf, "n": radar["n"],
            "failed": res["failed"][:20], "elapsed_s": round(time.time() - t0, 1)}


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Bitunix shadow radar (display/shadow only, no orders)")
    ap.add_argument("job", choices=["daily", "4h", "1h"])
    a = ap.parse_args(argv)
    if a.job == "daily":
        res = run_daily()
    elif a.job == "1h":
        import bx_shadow  # 1H only for new tokens on a 1H GC + coins held in the shadow book (1H exits)
        res = run_tf("1h", symbols=bx_shadow.hourly_symbols())
    else:
        res = run_tf(a.job)
    print(json.dumps(res, default=str))
    return 0 if res.get("status") in ("success", "partial", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
