#!/usr/bin/env python3
"""GIIQ analysis dimensions (維度) — per-candidate scores, SHADOW ONLY.

Every entry candidate gets one score per dimension on a -2..+2 scale plus the raw
numbers behind it. Scores never gate an order: the executor ignores this module.
Their only job is to be recorded in the ledger (dim_ledger.py) together with the
signal's later outcome, so we can MEASURE which dimension actually helps.

Railway-computed (quantitative) dimensions:
  trend          1D/4H/1H alignment + breakout strength on the signal timeframe
  extension      how far the 1D close already sits above the 1D Upper (chase risk)
  rel_strength   7-day return vs BTC (closed 1D bars)
  crowding       HL funding (annualised) — longs paying a lot = crowded
  liquidity      HL 24h notional volume
  narrative      on the Narrative watchlist / CAT N
  btc_regime     BTC 1D and 4H close vs GC Filter (same for every coin on a day)
  concentration  open positions already in the same sector
  smart_money    net long/short of tracked HL whale wallets (whales.py)

Claude-scored (qualitative) dimensions arrive with POST /api/ai/decision as
"dims": {"macro": .., "news": .., "social": .., "conviction": ..}.

Thresholds are v1 guesses on purpose — the ledger report tells us which to keep.
Bump DIM_VERSION whenever a formula changes so old and new scores never mix.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

DIM_VERSION = "dims-v1"
DAY_MS = 86_400_000

QUANT_DIMS: Tuple[str, ...] = (
    "trend", "extension", "rel_strength", "crowding", "liquidity",
    "narrative", "btc_regime", "concentration", "smart_money",
)
CLAUDE_DIMS: Tuple[str, ...] = ("macro", "news", "social", "conviction")

WHALE_MIN_WALLETS = 3  # fewer tracked wallets than this in a coin -> no signal (score 0)


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def clamp(x: float, lo: int = -2, hi: int = 2) -> int:
    return int(max(lo, min(hi, round(x))))


def _dim(score: Optional[int], **raw: Any) -> Dict[str, Any]:
    return {"score": score, "raw": {k: v for k, v in raw.items()}}


def _r(x: Optional[float], n: int = 4) -> Optional[float]:
    return round(x, n) if x is not None else None


# ---------------------------------------------------------------------------
# Individual dimensions (pure functions — easy to unit test and to re-score later)
# ---------------------------------------------------------------------------
def score_trend(cand: dict, row_1h: Optional[dict]) -> Dict[str, Any]:
    t1d, t4h = cand.get("trend_1d"), cand.get("trend_4h")
    t1h = (row_1h or {}).get("trend")
    align = sum(1 for t in (t1d, t4h, t1h) if t == "Green")
    chase = bool(cand.get("is_chase", cand.get("type") == "Chase"))
    close = _f(cand.get("close_4h") if chase else cand.get("close_1d"))
    upper = _f(cand.get("upper_4h") if chase else cand.get("upper_1d"))
    if close is None or upper is None or upper <= 0:
        return _dim(None, align=align, why="missing close/upper")
    strength = (close - upper) / upper * 100
    s = (align - 2) + (1 if strength >= 1.0 else 0 if strength >= 0.5 else -1)
    return _dim(clamp(s), align=align, trends=[t1d, t4h, t1h], signal_tf="4h" if chase else "1d",
                breakout_pct=_r(strength, 3))


def score_extension(cand: dict) -> Dict[str, Any]:
    close, upper = _f(cand.get("close_1d")), _f(cand.get("upper_1d"))
    if close is None or upper is None or upper <= 0:
        return _dim(None, why="missing close/upper")
    ext = (close - upper) / upper * 100
    s = 1 if ext < 3 else 0 if ext < 10 else -1 if ext < 20 else -2
    return _dim(s, ext_above_upper_pct=_r(ext, 2))


def closed_closes(bars: Optional[List[list]], now_ms: int, bar_ms: int = DAY_MS) -> List[float]:
    """Closes of CLOSED bars only ([t,o,h,l,c,v] rows)."""
    out = []
    for b in bars or []:
        try:
            if int(b[0]) + bar_ms <= now_ms:
                out.append(float(b[4]))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def ret_n(closes: List[float], n: int) -> Optional[float]:
    if len(closes) <= n or closes[-1 - n] <= 0:
        return None
    return (closes[-1] / closes[-1 - n] - 1) * 100


def score_rel_strength(sym_bars: Optional[List[list]], btc_bars: Optional[List[list]], now_ms: int) -> Dict[str, Any]:
    r_c = ret_n(closed_closes(sym_bars, now_ms), 7)
    r_b = ret_n(closed_closes(btc_bars, now_ms), 7)
    if r_c is None or r_b is None:
        return _dim(None, why="need 8 closed 1D bars")
    d = r_c - r_b
    s = 2 if d >= 10 else 1 if d >= 3 else 0 if d > -3 else -1 if d > -10 else -2
    return _dim(s, ret7_pct=_r(r_c, 2), btc_ret7_pct=_r(r_b, 2), diff_pp=_r(d, 2))


def score_crowding(ctx: Optional[dict], prev_ctx: Optional[dict]) -> Dict[str, Any]:
    fund = _f((ctx or {}).get("funding"))
    if fund is None:
        return _dim(None, why="no funding")
    ann = fund * 24 * 365 * 100  # HL funding is an hourly rate
    s = -2 if ann >= 50 else -1 if ann >= 20 else 1 if ann <= -5 else 0
    oi, poi = _f((ctx or {}).get("oi")), _f((prev_ctx or {}).get("oi"))
    oi_chg = (oi / poi - 1) * 100 if oi is not None and poi else None
    return _dim(s, funding_ann_pct=_r(ann, 2), oi=oi, oi_chg_24h_pct=_r(oi_chg, 2))


def score_liquidity(ctx: Optional[dict], cand: dict) -> Dict[str, Any]:
    vol = _f((ctx or {}).get("day_ntl_vlm"))
    if vol is None:
        vol = _f(cand.get("dayNtlVlm"))
    if vol is None:
        return _dim(None, why="no volume")
    s = 2 if vol >= 50e6 else 1 if vol >= 10e6 else 0 if vol >= 2e6 else -1 if vol >= 0.5e6 else -2
    return _dim(s, day_ntl_vlm=round(vol, 0))


def score_narrative(cand: dict, narrative_syms: Dict[str, Optional[str]]) -> Dict[str, Any]:
    sym = str(cand.get("symbol") or "").upper()
    cats = str(cand.get("cat_tags") or cand.get("category_label") or "").split()
    on_list = sym in narrative_syms
    hit = on_list or "N" in cats
    return _dim(1 if hit else 0, on_watchlist=on_list, cat_tags=" ".join(cats), sector=narrative_syms.get(sym))


def btc_regime(radar_1d: dict, radar_4h: dict) -> Dict[str, Any]:
    def _pos(radar: dict) -> Optional[bool]:
        for r in (radar or {}).get("rows") or []:
            if r.get("symbol") == "BTC":
                c, f = _f(r.get("close")), _f(r.get("filter"))
                return None if c is None or f is None else c > f
        return None
    d, h = _pos(radar_1d), _pos(radar_4h)
    if d is None:
        return _dim(None, why="BTC 1D row missing")
    if h is None:
        h = d
    s = {(True, True): 1, (True, False): 0, (False, True): -1, (False, False): -2}[(d, h)]
    return _dim(s, btc_1d_above_filter=d, btc_4h_above_filter=h)


def score_concentration(sector: Optional[str], held: Iterable[str], sector_of: Dict[str, Optional[str]],
                        sym: str) -> Dict[str, Any]:
    held = [h for h in held if h and h != sym]
    if not sector:
        return _dim(0, sector=None, same_sector=0, n_positions=len(held), why="sector unknown")
    same = sorted(h for h in held if sector_of.get(h) == sector)
    return _dim(-min(2, len(same)), sector=sector, same_sector=len(same), same_sector_coins=same,
                n_positions=len(held))


def score_smart_money(agg: Optional[dict], prev_agg: Optional[dict]) -> Dict[str, Any]:
    if agg is None:
        return _dim(None, why="no whale snapshot")
    L, S = _f(agg.get("long_ntl")) or 0.0, _f(agg.get("short_ntl")) or 0.0
    nl, ns = int(agg.get("n_long") or 0), int(agg.get("n_short") or 0)
    raw = dict(long_ntl=round(L, 0), short_ntl=round(S, 0), n_long=nl, n_short=ns)
    if nl + ns < WHALE_MIN_WALLETS or L + S <= 0:
        return _dim(0, net=None, **raw, why=f"< {WHALE_MIN_WALLETS} tracked wallets")
    net = (L - S) / (L + S)
    prev_net = None
    if prev_agg:
        pl, ps = _f(prev_agg.get("long_ntl")) or 0.0, _f(prev_agg.get("short_ntl")) or 0.0
        prev_net = (pl - ps) / (pl + ps) if pl + ps > 0 else None
    s = 2 if net >= 0.5 else 1 if net >= 0.2 else -2 if net <= -0.5 else -1 if net <= -0.2 else 0
    return _dim(s, net=_r(net, 3), net_change=_r(net - prev_net, 3) if prev_net is not None else None, **raw)


# ---------------------------------------------------------------------------
# Inputs -> scores for every candidate
# ---------------------------------------------------------------------------
def parse_asset_ctxs(meta_and_ctxs: Any) -> Dict[str, dict]:
    """HL metaAndAssetCtxs -> {coin: {funding, oi, day_ntl_vlm, mark, prev_day_px}}."""
    out: Dict[str, dict] = {}
    try:
        meta, ctxs = meta_and_ctxs[0], meta_and_ctxs[1]
        for u, c in zip(meta.get("universe") or [], ctxs or []):
            name = u.get("name")
            if not name:
                continue
            out[name] = {"funding": _f(c.get("funding")), "oi": _f(c.get("openInterest")),
                         "day_ntl_vlm": _f(c.get("dayNtlVlm")), "mark": _f(c.get("markPx")),
                         "prev_day_px": _f(c.get("prevDayPx"))}
    except (TypeError, IndexError, KeyError, AttributeError):
        return {}
    return out


def narrative_index(narrative_wl: dict, radar_1d: Optional[dict] = None) -> Dict[str, Optional[str]]:
    """symbol (HL name) -> sector, from the Narrative watchlist (+ HL name map on the 1D radar)."""
    nmap = {str(k).upper(): v for k, v in ((radar_1d or {}).get("narrative_map") or {}).items()}
    out: Dict[str, Optional[str]] = {}
    for it in (narrative_wl or {}).get("items") or []:
        t = str(it.get("ticker") or "").upper()
        if not t:
            continue
        sym = str(nmap.get(t) or t).upper()
        out[sym] = it.get("sector")
        out.setdefault(t, it.get("sector"))
    return out


def sector_index(radar_1d: dict, narrative_syms: Dict[str, Optional[str]]) -> Dict[str, Optional[str]]:
    """symbol -> sector: Narrative sector first, else the radar row's primary category."""
    out: Dict[str, Optional[str]] = {}
    for r in (radar_1d or {}).get("rows") or []:
        s = r.get("symbol")
        if s:
            out[str(s).upper()] = r.get("category")
    for s, sec in narrative_syms.items():
        if sec:
            out[s] = sec
    return out


def held_coins(perp_state: Any) -> List[str]:
    out = []
    groups = perp_state.get("assetPositions") or [] if isinstance(perp_state, dict) else []
    for g in groups:
        p = (g or {}).get("position") or {}
        if (_f(p.get("szi")) or 0) != 0 and p.get("coin"):
            out.append(str(p["coin"]).upper())
    return out


def compute_all(candidates: List[dict], *, radar_1d: dict, radar_4h: dict, radar_1h: dict,
                bars_1d: Dict[str, List[list]], asset_ctxs: Dict[str, dict],
                prev_ctxs: Optional[Dict[str, dict]], narrative_wl: dict, perp_state: Any,
                whale_coins: Optional[Dict[str, dict]], prev_whale_coins: Optional[Dict[str, dict]],
                now_ms: int) -> Dict[str, Any]:
    rows_1h = {r.get("symbol"): r for r in (radar_1h or {}).get("rows") or []}
    nsyms = narrative_index(narrative_wl, radar_1d)
    sectors = sector_index(radar_1d, nsyms)
    held = held_coins(perp_state)
    regime = btc_regime(radar_1d, radar_4h)
    prev_ctxs = prev_ctxs or {}
    out: List[dict] = []
    for c in candidates or []:
        sym = str(c.get("symbol") or "").upper()
        if not sym:
            continue
        ctx = asset_ctxs.get(sym)
        dims = {
            "trend": score_trend(c, rows_1h.get(sym)),
            "extension": score_extension(c),
            "rel_strength": score_rel_strength(bars_1d.get(sym), bars_1d.get("BTC"), now_ms),
            "crowding": score_crowding(ctx, prev_ctxs.get(sym)),
            "liquidity": score_liquidity(ctx, c),
            "narrative": score_narrative(c, nsyms),
            "btc_regime": regime,
            "concentration": score_concentration(sectors.get(sym), held, sectors, sym),
            "smart_money": score_smart_money((whale_coins or {}).get(sym) if whale_coins is not None else None,
                                             (prev_whale_coins or {}).get(sym)),
        }
        scored = [d["score"] for d in dims.values() if d["score"] is not None]
        out.append({
            "symbol": sym, "type": c.get("type"), "tier": c.get("tier"),
            "already_held": bool(c.get("already_held")) or sym in held,
            "total": sum(scored), "n_scored": len(scored), "dims": dims,
        })
    out.sort(key=lambda x: (-x["total"], x["symbol"]))
    return {"dim_version": DIM_VERSION, "btc_regime": regime, "n": len(out), "candidates": out}


def compact(result: dict) -> Dict[str, Any]:
    """Small view for DESK_DATA / Claude: {symbol: {total, scores}}."""
    return {
        "dim_version": result.get("dim_version"),
        "generated_at": result.get("generated_at"),
        "signal_date": result.get("signal_date"),
        "whales": result.get("whales_meta"),
        "scores": {c["symbol"]: {"total": c["total"], **{k: v["score"] for k, v in c["dims"].items()}}
                   for c in result.get("candidates") or []},
    }
