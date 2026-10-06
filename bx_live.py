#!/usr/bin/env python3
"""Bitunix LIVE pilot — rules, gates, entries, exits, circuit breaker. MMT-approved 2026-09-30.

Separate from HL GIIQ-SoT-3; nothing here is imported by the HL order path (test_bx_isolation.py).
Runs only in the Singapore bx-exec service (bx_service.py).

FAIL CLOSED — a live ORDER is sent only when every one of these holds (live_gate):
  1. BX_ENABLED=1 and BX_LIVE=1 (default 0; MMT flips it), and the circuit breaker is not tripped
  2. egress verified non-US by two geo sources, Railway region not us-*, IP = BX_EXPECTED_EGRESS_IP if set
  3. BX_API_KEY / BX_API_SECRET present (Railway variables only; never logged)
  4. the account answers a signed read (bad key / IP not whitelisted -> refuse)
  5. an ENTRY_DESK approval (source=claude, today HKT, this symbol) exists — no approval, no order
Per trade (check_entry):
  * pilot tier only: BX-only crypto, 24h vol >= $2M, spread < 10 bp (re-measured live), gc_tf 1d or 4h
    (no 1H signals, no watch tier, no stock / commodity / index contracts)
  * isolated margin 1% NAV, 3x; notional <= 0.5% of 24h volume (downsized, skipped below the minimum qty)
  * max 2 open BX positions, max 1 new BX entry per HKT day (pending Chase fills count)
  * Hard SL (tier rule, 4H radar) >= 1.5% below the price; estimated isolated liquidation price below the
    Hard SL (maintenance rate from the public position tiers); else skip
  * order = IOC limit buy at ask + 0.5% with the Hard SL attached (MARK_PRICE trigger, market); then the
    position, leverage, margin mode, liq price and the SL order are verified on the exchange; if the SL is
    missing it is placed again, and if that fails the position is closed at once
Exits (every 4h and hourly): 4H close < 4H Filter; time cap 7 days (5 days for new tokens); liquidity exit
when 24h vol < $1M or spread > 30 bp; the exchange Hard SL. Exits and SL repair are close-only actions and
keep running while BX_LIVE=0 or the breaker is tripped, as long as the key is present and BX_ENABLED=1.
Circuit breaker: cumulative live BX P&L (closed, after fees and funding, plus open unrealized) <= -3% of the
pilot NAV baseline -> tripped (sticky file), no new entries, [BX_ALERT] logged, shown as a problem on the
cockpit exit health (EXIT_DESK emails MMT), BX_LIVE set to 0 through the Railway API when a token is set.
Only BX_ADMIN_KEY can reset it.
NAV = HL NAV (same definition as the HL executor, read from the public HL API) + Bitunix futures equity.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import bx_universe as U  # noqa: E402

OUT_DIR = Path(os.environ.get("BX_OUT_DIR") or (ROOT / "out"))
HKT = timezone(timedelta(hours=8))

# --- pilot rules (MMT 2026-09-30 / GIIQ-SoT-5 2026-10-06) ----------------------------------------
# GIIQ-SoT-5: size/leverage from desk decision (not fixed); daily cap and max-open removed; 
# $2M volume floor removed (kept spread limit, volume-based sizing, min qty)
BREAKER_PCT_NAV = 3.0
SPREAD_MAX_BP = 10.0            # strictly below
MAX_SIZE_OF_VOL = 0.005
MIN_SL_DIST_PCT = 0.6           # GIIQ-SoT-5: lowered to match HL (IOC 0.5% + fees 0.09%)
IOC_SLIP = 0.005                # IOC limit = ask x 1.005
PRICE_SANITY = 0.5              # live price within 50% of the radar close
BX_TOTAL_MARGIN_CAP_PCT = 80.0  # GIIQ-SoT-5 ADD-2: BX total margin cap (HL+BX combined if visible)
# BX radar freshness thresholds (GIIQ-SoT-5 ADD-1)
BX_RADAR_MAX_AGE_H = {"1d": 36.0, "4h": 4.5}
BX_RADAR_MIN_ROWS_PCT = 0.70    # 70% of normal row count
TIME_CAP_DAYS = 7
TIME_CAP_DAYS_NEW_TOKEN = 5
LIQ_EXIT_VOL = 1_000_000.0
LIQ_EXIT_SPREAD_BP = 30.0
LIVE_GC_TFS = ("1d", "4h")
DECISION_LATE_HKT = (8, 50)
DEFAULT_MMR = 0.02              # used only if the tier lookup fails (conservative)


def _f(v: Any) -> Optional[float]:
    return U._f(v)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hkt_date(now: datetime) -> str:
    return now.astimezone(HKT).strftime("%Y-%m-%d")


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _log(msg: str) -> None:
    try:
        from bx_trade import redact
        msg = redact(msg)
    except Exception:  # pragma: no cover
        pass
    sys.stderr.write(msg + "\n")


# ---------------------------------------------------------------------------------------------
# switches + breaker
# ---------------------------------------------------------------------------------------------
def env_on(name: str, default: str = "0") -> bool:
    return (os.environ.get(name, default) or default).strip().lower() in ("1", "true", "yes", "on")


def breaker_path() -> Path:
    return OUT_DIR / "bx_breaker.json"


def breaker_state() -> dict:
    return _read(breaker_path()) or {"tripped": False}


def live_gate(egress: Optional[dict], keys_ok: bool, breaker: Optional[dict] = None,
              env: Optional[Dict[str, str]] = None) -> Tuple[bool, List[str]]:
    """Global prerequisites for sending ANY new live order. Pure; every missing item is listed."""
    env = env if env is not None else dict(os.environ)
    on = lambda k, d="0": (env.get(k, d) or d).strip().lower() in ("1", "true", "yes", "on")  # noqa: E731
    why = []
    if not on("BX_ENABLED", "1"):
        why.append("BX_ENABLED=0")
    if not on("BX_LIVE", "0"):
        why.append("BX_LIVE=0 (shadow only)")
    if (breaker or {}).get("tripped"):
        why.append(f"circuit breaker tripped at {(breaker or {}).get('at')}")
    if not egress or not egress.get("ok"):
        why.append(f"egress not verified non-US: {(egress or {}).get('reason', 'not checked')}")
    if not keys_ok:
        why.append("Bitunix API key/secret missing")
    return (not why), why


def close_gate(keys_ok: bool, env: Optional[Dict[str, str]] = None) -> Tuple[bool, List[str]]:
    """Close-only actions (exits, SL repair) need only BX_ENABLED=1 and the key."""
    env = env if env is not None else dict(os.environ)
    why = []
    if (env.get("BX_ENABLED", "1") or "1").strip().lower() not in ("1", "true", "yes", "on"):
        why.append("BX_ENABLED=0")
    if not keys_ok:
        why.append("Bitunix API key/secret missing")
    return (not why), why


def breaker_check(realized_usd: float, unrealized_usd: float, baseline_nav: Optional[float]) -> Dict[str, Any]:
    pnl = round(float(realized_usd or 0) + float(unrealized_usd or 0), 4)
    if not baseline_nav or baseline_nav <= 0:
        return {"trip": False, "pnl_usd": pnl, "limit_usd": None, "pct_nav": None}
    limit = -BREAKER_PCT_NAV / 100.0 * baseline_nav
    return {"trip": pnl <= limit, "pnl_usd": pnl, "limit_usd": round(limit, 4),
            "pct_nav": round(pnl / baseline_nav * 100, 3)}


def trip_breaker(info: dict, now: Optional[datetime] = None, set_var: Optional[Callable[[], str]] = None) -> dict:
    state = {"tripped": True, "at": (now or _now()).isoformat(), **info}
    try:
        state["railway_var"] = (set_var or railway_set_live_off)()
    except Exception as e:  # noqa: BLE001
        state["railway_var"] = f"failed: {str(e)[:120]}"
    _write(breaker_path(), state)
    _log(f"[BX_ALERT] CIRCUIT BREAKER TRIPPED: live BX P&L {info.get('pnl_usd')} USD "
         f"({info.get('pct_nav')}% of NAV) <= -{BREAKER_PCT_NAV}% -> no new BX entries; BX_LIVE={state['railway_var']}")
    hook = (os.environ.get("BX_ALERT_WEBHOOK") or "").strip()
    if hook:
        try:
            req = urllib.request.Request(hook, data=json.dumps({"text": f"BX circuit breaker tripped: {info}"}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:  # noqa: BLE001
            _log(f"[BX_ALERT] webhook failed: {str(e)[:120]}")
    return state


def railway_set_live_off() -> str:
    """Set BX_LIVE=0 on this service via the Railway API (skipDeploys) when RAILWAY_API_TOKEN is set.
    The sticky breaker file blocks entries either way."""
    token = (os.environ.get("RAILWAY_API_TOKEN") or "").strip()
    ids = {k: os.environ.get(k) for k in ("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID")}
    if not token or not all(ids.values()):
        return "not changed (no RAILWAY_API_TOKEN); breaker file blocks entries"
    q = ("mutation($i: VariableUpsertInput!) { variableUpsert(input: $i) }")
    body = {"query": q, "variables": {"i": {"projectId": ids["RAILWAY_PROJECT_ID"],
                                            "environmentId": ids["RAILWAY_ENVIRONMENT_ID"],
                                            "serviceId": ids["RAILWAY_SERVICE_ID"], "name": "BX_LIVE", "value": "0",
                                            "skipDeploys": True}}}
    req = urllib.request.Request("https://backboard.railway.com/graphql/v2", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode())
    return "set to 0" if (d.get("data") or {}).get("variableUpsert") else f"failed: {str(d.get('errors'))[:120]}"


def reset_breaker(admin_key: str) -> Tuple[bool, str]:
    want = (os.environ.get("BX_ADMIN_KEY") or "").strip()
    if not want:
        return False, "BX_ADMIN_KEY not set: breaker cannot be reset over the API"
    import hmac
    if not admin_key or not hmac.compare_digest(admin_key, want):
        return False, "forbidden"
    st = breaker_state()
    _write(breaker_path(), {"tripped": False, "reset_at": _now().isoformat(), "previous": st})
    return True, "breaker reset; BX_LIVE must still be set to 1 by MMT"


def baseline_nav(nav_now: Optional[float]) -> Optional[float]:
    """Pilot NAV baseline for the breaker: frozen at the first live check with a valid NAV."""
    p = OUT_DIR / "bx_live_state.json"
    st = _read(p) or {}
    if st.get("baseline_nav"):
        return float(st["baseline_nav"])
    if nav_now and nav_now > 0:
        _write(p, {**st, "baseline_nav": round(nav_now, 4), "baseline_at": _now().isoformat()})
        return nav_now
    return None


# ---------------------------------------------------------------------------------------------
# NAV
# ---------------------------------------------------------------------------------------------
HL_INFO = "https://api.hyperliquid.xyz/info"


def _hl_info(body: dict) -> Any:
    req = urllib.request.Request(HL_INFO, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def hl_nav(address: Optional[str] = None, info: Callable[[dict], Any] = _hl_info) -> Optional[float]:
    """HL NAV from the PUBLIC info API (no key): same definition as the HL executor (exec_common.nav_snapshot)."""
    from exec_common import nav_snapshot
    addr = (address or os.environ.get("HL_ADDRESS") or "").strip()
    if not addr:
        return None
    try:
        perp = info({"type": "clearinghouseState", "user": addr})
        spot = info({"type": "spotClearinghouseState", "user": addr})
        try:
            ab = info({"type": "userAbstraction", "user": addr})
        except Exception:  # noqa: BLE001
            ab = "unknown"
        nav = nav_snapshot(spot, perp, ab if isinstance(ab, str) else "unknown")["nav"]
        return float(nav) if nav and nav > 0 else None
    except Exception as e:  # noqa: BLE001
        _log(f"[BX_LIVE] HL NAV unavailable: {str(e)[:120]}")
        return None


def bx_equity(account: dict) -> float:
    return round(sum(_f(account.get(k)) or 0.0 for k in
                     ("available", "frozen", "margin", "crossUnrealizedPNL", "isolationUnrealizedPNL")), 6)


# ---------------------------------------------------------------------------------------------
# candidates for ENTRY_DESK + decisions
# ---------------------------------------------------------------------------------------------
def candidates_path() -> Path:
    return OUT_DIR / "bx_candidates_latest.json"


def decisions_path(day: str) -> Path:
    return OUT_DIR / "bx_decisions" / f"bx_decisions_{day.replace('-', '')}.json"


def pilot_eligible(meta: dict) -> Tuple[bool, str]:
    """GIIQ-SoT-5: removed $2M volume floor (B8), keep spread limit."""
    if meta.get("ex") != "BX":
        return False, "listed on HL (HL path only)"
    if meta.get("asset_class") != "crypto":
        return False, f"asset class {meta.get('asset_class')} (no stocks / commodities / indices)"
    if meta.get("liq_tier") != "tradeable":
        return False, f"tier {meta.get('liq_tier')} (entry tier only)"
    # GIIQ-SoT-5: removed VOL_MIN check, keep spread and volume-based sizing
    sp = _f(meta.get("spread_bp"))
    if sp is None or sp >= SPREAD_MAX_BP:
        return False, f"spread {sp} bp not < {SPREAD_MAX_BP:g}"
    if meta.get("gc_tf") not in LIVE_GC_TFS:
        return False, f"GC timeframe {meta.get('gc_tf')} (no 1H signals)"
    if meta.get("api_supported") is False:
        return False, "API trading not supported on this contract"
    # Leverage check removed: will come from desk decision
    return True, ""


def build_candidates(now: Optional[datetime] = None) -> dict:
    """08:02 HKT (after the BX daily radar): today's pilot-eligible BX signals for the 08:10 ENTRY_DESK."""
    import bx_radar
    import bx_shadow as S
    now = now or _now()
    meta_all = {m["bx_symbol"]: m for m in (bx_radar.load_meta().get("scanned") or [])}
    r1d, r4h = (S._rows_by_symbol(bx_radar.load_radar(tf)) for tf in ("1d", "4h"))
    out, skipped = [], []
    for sym, m in meta_all.items():
        s = S.classify_signal(m, r1d.get(sym), r4h.get(sym), None)
        if not s or s["gc_tf"] not in LIVE_GC_TFS:
            continue
        ok, why = pilot_eligible(m)
        if not ok:
            skipped.append({"symbol": sym, "type": s["type"], "reason": why})
            continue
        row1, row4 = r1d.get(sym) or {}, r4h.get(sym) or {}
        sl, sl_rule = S.hard_sl(m.get("tier") or "tiny", s["type"], s["gc_tf"], row4, None)
        close = _f(m.get("price")) or _f(s["row"].get("close"))
        last_cross = row1.get("last_cross_up_at")
        out.append({
            "symbol": sym, "coin": m.get("symbol"), "ex": "BX", "type": s["type"], "gc_tf": s["gc_tf"],
            "kind": ("CONTINUATION" if s["type"] == "Chase" else "BASE"), "tier": m.get("tier"),
            "close": close, "upper_1d": row1.get("upper"), "filter_1d": row1.get("filter"), "lower_1d": row1.get("lower"),
            "trend_1d": row1.get("trend"), "upper_4h": row4.get("upper"), "filter_4h": row4.get("filter"),
            "lower_4h": row4.get("lower"), "trend_4h": row4.get("trend"),
            "breakout_4h_pct": (round((_f(row4.get("close")) / _f(row4.get("upper")) - 1) * 100, 3)
                                if _f(row4.get("close")) and _f(row4.get("upper")) else None),
            "last_1d_cross_days": (round((now.timestamp() * 1000 - int(last_cross)) / 86_400_000, 1)
                                   if last_cross else None),
            "hard_sl": sl, "hard_sl_rule": sl_rule,
            "sl_dist_pct": round((close - sl) / close * 100, 3) if close and sl else None,
            "vol24h_usd": m.get("vol24h_usd"), "spread_bp": m.get("spread_bp"), "ign_x": m.get("ign_x"),
            "narrative": bool(m.get("narrative")), "cat_tags": m.get("cat_tags"), "mcap_usd": m.get("mcap_usd"),
            "max_leverage": m.get("max_leverage"), "contract_age_days": m.get("contract_age_days"),
            "asset_age": m.get("asset_age"),
            "size_rule": f"{MARGIN_PCT_NAV:g}% NAV margin, {LEVERAGE}x, <= 0.5% of 24h vol",
        })
    doc = {"date": hkt_date(now), "generated_at": now.isoformat(), "ex": "BX", "candidates": out,
           "not_eligible": skipped[:100],
           "rules": {"veto": ["V1", "V2", "V3", "V5"], "approve_max": MAX_NEW_PER_DAY,
                     "no_fallback": "no approval -> no order"}}
    _write(candidates_path(), doc)
    return doc


def store_decisions(decisions: List[dict], source: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """ENTRY_DESK BX decisions. Only source=claude counts (there is no fallback for BX). A symbol must be in
    today's BX candidate list. Decisions after 08:50 HKT are stored but flagged late."""
    from decisions import normalize
    now = now or _now()
    day = hkt_date(now)
    if str(source or "claude").lower() != "claude":
        return {"ok": False, "error": "BX decisions accept source=claude only (no fallback for BX)", "stored": 0}
    cand = _read(candidates_path()) or {}
    valid = {c["symbol"]: c for c in cand.get("candidates") or []} if cand.get("date") == day else {}
    by_coin = {str(c.get("coin") or "").upper(): c["symbol"] for c in valid.values()}
    hm = now.astimezone(HKT)
    late = (hm.hour, hm.minute) > DECISION_LATE_HKT
    p = decisions_path(day)
    doc = _read(p) or {"date": day, "decisions": {}, "history": []}
    stored, rejected = [], []
    for d in decisions or []:
        n = normalize(d)
        if not n:
            rejected.append({"input": d, "why": "need symbol + approve|veto"})
            continue
        sym = n["symbol"] if n["symbol"] in valid else by_coin.get(n["symbol"])
        if not sym:
            rejected.append({"symbol": n["symbol"], "why": "not in today's BX candidate list"})
            continue
        rec = {**n, "symbol": sym, "source": "claude", "ts": now.isoformat(), "late": late}
        doc["decisions"][sym] = rec
        doc["history"].append(rec)
        stored.append(rec)
    _write(p, doc)
    return {"ok": bool(stored), "stored": len(stored), "rejected": rejected, "late": late, "date": day}


def approval_for(symbol: str, now: Optional[datetime] = None) -> Optional[dict]:
    day = hkt_date(now or _now())
    rec = ((_read(decisions_path(day)) or {}).get("decisions") or {}).get(symbol)
    if rec and rec.get("decision") == "approve" and rec.get("source") == "claude":
        return rec
    return None


# ---------------------------------------------------------------------------------------------
# sizing + entry checks (pure)
# ---------------------------------------------------------------------------------------------
def floor_to(x: float, decimals: int) -> float:
    q = 10 ** int(decimals or 0)
    return math.floor(x * q + 1e-9) / q


def fmt(x: float, decimals: int) -> str:
    return f"{x:.{int(decimals or 0)}f}"


def mmr_for(tiers: List[dict], notional: float) -> float:
    for t in sorted(tiers or [], key=lambda t: _f(t.get("startValue")) or 0):
        lo, hi = _f(t.get("startValue")) or 0, _f(t.get("endValue")) or float("inf")
        if lo <= notional < hi:
            return _f(t.get("maintenanceMarginRate")) or DEFAULT_MMR
    return DEFAULT_MMR


def liq_price_long(entry: float, leverage: float, mmr: float) -> float:
    """Isolated long, no extra margin: liq ~ entry x (1 - 1/lev + mmr)."""
    return entry * (1 - 1.0 / leverage + mmr)


def bx_radar_fresh(radar_1d: dict, radar_4h: dict, now: datetime) -> Tuple[bool, str]:
    """GIIQ-SoT-5 ADD-1: BX radar freshness and row-count check (same as HL guardrail)."""
    def age_h(rd: dict) -> Optional[float]:
        ts = rd.get("generated_at")
        if not ts:
            return None
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return (now.timestamp() - dt.timestamp()) / 3600.0
        except (ValueError, TypeError):
            return None
    
    def row_count_ok(rd: dict, tf: str, normal_count: int) -> Tuple[bool, str]:
        rows = rd.get("rows", [])
        n = len(rows) if isinstance(rows, list) else 0
        min_n = int(normal_count * BX_RADAR_MIN_ROWS_PCT)
        if n < min_n:
            return False, f"BX {tf.upper()} radar has {n} rows < {BX_RADAR_MIN_ROWS_PCT:.0%} of normal ({normal_count})"
        return True, f"BX {tf.upper()} radar {n} rows OK"
    
    # Age checks
    age_1d = age_h(radar_1d)
    age_4h = age_h(radar_4h)
    if age_1d is None or age_1d > BX_RADAR_MAX_AGE_H["1d"]:
        return False, f"BX 1D radar stale (age={age_1d}h > {BX_RADAR_MAX_AGE_H['1d']}h)"
    if age_4h is None or age_4h > BX_RADAR_MAX_AGE_H["4h"]:
        return False, f"BX 4H radar stale (age={age_4h}h > {BX_RADAR_MAX_AGE_H['4h']}h)"
    
    # Row count checks (normal ~120-180, use 120 as baseline)
    ok_1d, why_1d = row_count_ok(radar_1d, "1d", 120)
    if not ok_1d:
        return False, why_1d
    ok_4h, why_4h = row_count_ok(radar_4h, "4h", 120)
    if not ok_4h:
        return False, why_4h
    
    return True, "BX radar fresh"


def check_entry(cand: dict, meta: dict, live: dict, nav: Optional[float], available: Optional[float],
                open_live: List[dict], bx_margin_used: float, approval: Optional[dict],
                tiers: List[dict]) -> Dict[str, Any]:
    """GIIQ-SoT-5: size/leverage from desk decision, daily cap and max-open removed, BX total margin cap added.
    Pure pilot checks for one entry. live = {price, ask, bid, spread_bp, vol24h}. -> {ok, reason, plan}."""
    sym = cand["symbol"]

    def no(reason: str) -> Dict[str, Any]:
        return {"ok": False, "symbol": sym, "reason": reason}
    if not approval:
        return no("no ENTRY_DESK approval today (no approval -> no order)")
    ok, why = pilot_eligible(meta)
    if not ok:
        return no(why)
    # GIIQ-SoT-5: max-open check removed (B13)
    if any(t.get("bx_symbol") == sym for t in open_live):
        return no("already holding this contract")
    # GIIQ-SoT-5: daily cap removed (B14)
    price, ask = _f(live.get("price")), _f(live.get("ask")) or _f(live.get("price"))
    if not price or not ask:
        return no("no live price")
    # GIIQ-SoT-5: volume floor removed, but spread check stays
    sp = _f(live.get("spread_bp"))
    if sp is None or sp >= SPREAD_MAX_BP:
        return no(f"live spread {sp} bp not < {SPREAD_MAX_BP:g}")
    ref = _f(cand.get("close"))
    if ref and abs(price / ref - 1) > PRICE_SANITY:
        return no(f"price {price} vs radar {ref}: > 50% apart")
    sl = _f(cand.get("hard_sl"))
    if not sl or sl >= price:
        return no(f"no valid Hard SL ({cand.get('hard_sl_rule')}={sl})")
    dist = (price - sl) / price * 100
    if dist < MIN_SL_DIST_PCT:
        return no(f"Hard SL only {dist:.2f}% below price (< {MIN_SL_DIST_PCT:g}%)")
    if not nav or nav <= 0:
        return no("NAV unavailable (HL NAV + BX equity)")
    
    # GIIQ-SoT-5: size/leverage from desk decision (B19), within 2-5x bounds
    size_pct = _f(approval.get("size_pct")) or 2.0  # floor is 2%
    leverage = int(_f(approval.get("leverage")) or 2)  # floor is 2x
    leverage = max(2, min(5, leverage))  # clamp to 2-5x
    # Check coin max leverage
    coin_max_lev = _f(meta.get("max_leverage"))
    if coin_max_lev and leverage > coin_max_lev:
        leverage = int(math.floor(coin_max_lev))
    if leverage < 2:
        return no(f"leverage {leverage}x < 2x minimum after coin max leverage")
    
    margin = size_pct / 100.0 * nav
    notional = margin * leverage
    note = ""
    
    # Downsize to 0.5% of 24h volume if needed
    cap = MAX_SIZE_OF_VOL * float(live.get("vol24h") or 0)
    if cap > 0 and notional > cap:
        notional = cap
        margin = cap / leverage
        note = f"downsized to 0.5% of 24h vol (${cap:,.0f})"
    
    # GIIQ-SoT-5 ADD-2: BX total margin cap
    if nav > 0:
        new_total_pct = (bx_margin_used + margin) / nav * 100.0
        if new_total_pct > BX_TOTAL_MARGIN_CAP_PCT:
            return no(f"BX total margin {new_total_pct:.1f}% > {BX_TOTAL_MARGIN_CAP_PCT:g}% cap")
    
    if available is not None and margin > available:
        return no(f"BX available {available:.2f} USDT < margin {margin:.2f}")
    bp = int(meta.get("base_precision") or 0)
    qp = int(meta.get("quote_precision") or 8)
    qty = floor_to(notional / ask, bp)
    min_qty = _f(meta.get("min_qty")) or 0.0
    if qty <= 0 or qty < min_qty:
        return no(f"qty {qty} below the minimum {min_qty}")
    mmr = mmr_for(tiers, qty * ask)
    limit_px = floor_to(ask * (1 + IOC_SLIP), qp)
    liq = liq_price_long(limit_px, leverage, mmr)
    if liq >= sl:
        return no(f"estimated liq {liq:.6g} not below Hard SL {sl:.6g} at {leverage}x")
    sl_px = floor_to(sl, qp)
    return {"ok": True, "symbol": sym, "reason": note or "ok",
            "plan": {"qty": fmt(qty, bp), "limit_price": fmt(limit_px, qp), "sl_price": fmt(sl_px, qp),
                     "margin_usd": round(margin, 4), "notional_usd": round(qty * ask, 4), "leverage": leverage,
                     "mmr": mmr, "liq_est": round(liq, 10), "sl_dist_pct": round(dist, 3), "nav": round(nav, 2),
                     "note": note, "desk_size_pct": size_pct, "desk_leverage": approval.get("leverage")}}


# ---------------------------------------------------------------------------------------------
# exits (pure)
# ---------------------------------------------------------------------------------------------
def exit_reason(trade: dict, row4h: Optional[dict], meta: Optional[dict], live: Optional[dict],
                now: datetime) -> Optional[str]:
    entry_ms = _iso_ms(trade.get("entry_time"))
    if row4h and _f(row4h.get("close")) is not None and _f(row4h.get("filter")) is not None \
            and int(row4h.get("bar_time") or 0) >= entry_ms and row4h["close"] < row4h["filter"]:
        return "exit_4h_close_below_filter"
    days = TIME_CAP_DAYS_NEW_TOKEN if trade.get("kind") == "NewToken" else TIME_CAP_DAYS
    if entry_ms and now.timestamp() * 1000 - entry_ms >= days * 86_400_000:
        return f"time_cap_{days}d"
    vol = _f((live or {}).get("vol24h"))
    if vol is None:
        vol = _f((meta or {}).get("vol24h_usd"))
    sp = _f((live or {}).get("spread_bp"))
    if sp is None:
        sp = _f((meta or {}).get("spread_bp"))
    if (vol is not None and vol < LIQ_EXIT_VOL) or (sp is not None and sp > LIQ_EXIT_SPREAD_BP):
        return "liquidity_exit"
    return None


def _iso_ms(v: Any) -> int:
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return int(d.timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------------------------
# market data (public)
# ---------------------------------------------------------------------------------------------
def live_market(symbol: str) -> dict:
    import bx_client
    t = next((x for x in bx_client.tickers(symbols=symbol) if x.get("symbol") == symbol), {})
    book = bx_client.depth(symbol, 5)
    try:
        bid, ask = float(book["bids"][0][0]), float(book["asks"][0][0])
    except (KeyError, IndexError, TypeError, ValueError):
        bid = ask = None
    price = _f(t.get("lastPrice")) or _f(t.get("markPrice"))
    return {"price": price, "bid": bid, "ask": ask, "spread_bp": bx_client.spread_bp(book),
            "vol24h": bx_client.usd_volume(t.get("quoteVol"), t.get("baseVol"), price)}


# ---------------------------------------------------------------------------------------------
# ledger (bx_shadow_ledger.db, live trades marked mode='live')
# ---------------------------------------------------------------------------------------------
def connect(path: Optional[str] = None):
    """Same ledger as the shadow book (bx_shadow_ledger.db); live trades are rows with mode='live'."""
    import bx_shadow
    return bx_shadow.connect(path)


def open_live_trades(conn) -> List[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM shadow_trades WHERE mode='live' AND status='open'")]


def live_entries_today(conn, now: datetime) -> int:
    day = hkt_date(now)
    return sum(1 for r in conn.execute("SELECT entry_time FROM shadow_trades WHERE mode='live'")
               if r[0] and hkt_date(datetime.fromisoformat(r[0])) == day)


def realized_live_pnl(conn) -> float:
    return float(conn.execute("SELECT COALESCE(SUM(pnl_usd),0) FROM shadow_trades "
                              "WHERE mode='live' AND status='closed'").fetchone()[0] or 0.0)


# ---------------------------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------------------------
def execute_entry(trade_api, cand: dict, meta: dict, plan: dict, now: datetime, conn,
                  counted: bool = True, kind: Optional[str] = None) -> Dict[str, Any]:
    """Send the order with the Hard SL attached, then verify everything on the exchange. On any
    protection failure the position is closed at once (fail closed).
    GIIQ-SoT-5 ADD-4: deterministic client order ID."""
    sym = cand["symbol"]
    # GIIQ-SoT-5 ADD-4: deterministic client ID from hash(symbol + HKT date + kind)
    import hashlib
    day = hkt_date(now)
    s = f"{sym}|{day}|entry"
    h = hashlib.sha256(s.encode()).hexdigest()[:12]
    cid = f"giiqbx{h}"
    res: Dict[str, Any] = {"symbol": sym, "client_id": cid, "plan": plan}
    leverage = plan.get("leverage", 3)
    trade_api.set_isolated(sym)
    trade_api.set_leverage(sym, leverage)
    order = trade_api.open_long(sym, plan["qty"], plan["limit_price"], plan["sl_price"], cid)
    res["order_id"] = order.get("orderId")
    if trade_api.dry_run:
        res["status"] = "dry_run"
        return res
    det = trade_api.order_detail(order_id=order.get("orderId"), client_id=cid)
    filled = _f(det.get("tradeQty")) or 0.0
    if filled <= 0:
        res["status"] = "not_filled"
        res["detail_status"] = det.get("status")
        return res
    pos = next((p for p in trade_api.pending_positions(sym) if str(p.get("side")).upper() == "LONG"), None)
    if not pos:
        res["status"] = "filled_but_no_position"
        return res
    pid = str(pos.get("positionId"))
    problems = []
    if str(pos.get("marginMode") or "").upper() not in ("", "ISOLATION"):
        problems.append(f"margin mode {pos.get('marginMode')}")
    if pos.get("leverage") is not None and int(pos.get("leverage")) != leverage:
        problems.append(f"leverage {pos.get('leverage')} vs expected {leverage}")
    liq = _f(pos.get("liqPrice"))
    sl = float(plan["sl_price"])
    if liq and liq > 0 and liq >= sl:
        problems.append(f"exchange liq {liq} not below Hard SL {sl}")
    sl_orders = [o for o in trade_api.tpsl_pending(sym, pid) if _f(o.get("slPrice"))]
    sl_id = sl_orders[0].get("id") if sl_orders else None
    if not sl_orders and not problems:
        try:
            sl_id = trade_api.place_position_sl(sym, pid, plan["sl_price"]).get("orderId")
        except Exception as e:  # noqa: BLE001
            problems.append(f"Hard SL could not be placed: {str(e)[:120]}")
    if problems:
        trade_api.flash_close(pid)
        res.update(status="closed_protection_failed", problems=problems, position_id=pid)
        _log(f"[BX_ALERT] {sym} entry closed at once: {problems}")
        return res
    entry_px = _f(pos.get("avgOpenPrice")) or float(plan["limit_price"])
    conn.execute(
        """INSERT INTO shadow_trades(signal_id, symbol, bx_symbol, kind, gc_tf, tier, counted, entry_time, entry_px,
           size_pct_nav, leverage, hard_sl, exit_rule, fees_bp, slip_bp, status, note, mode, order_id, client_id,
           position_id, qty, sl_order_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?, 'live', ?,?,?,?,?)""",
        (cand.get("signal_id"), cand.get("coin") or sym, sym, kind or cand.get("type"), cand.get("gc_tf"),
         cand.get("tier"), int(counted), now.isoformat(), entry_px,
         round(plan["margin_usd"] / plan["nav"] * 100, 4), leverage, sl, "4h_close_below_filter", 6.0, None,
         f"hkt={hkt_date(now)}; live pilot; desk {plan.get('desk_size_pct')}%/{plan.get('desk_leverage')}x; {plan.get('note') or ''}".strip("; "), 
         res["order_id"], cid, pid, filled, sl_id))
    conn.commit()
    res.update(status="filled", position_id=pid, filled_qty=filled, entry_px=entry_px, sl_order_id=sl_id)
    _log(f"[BX_LIVE] ENTRY {sym} qty={filled} px={entry_px} {leverage}x SL={sl} pos={pid} cloid={cid}")
    return res


def _close_record(trade_api, conn, t: dict, reason: str, now: datetime) -> dict:
    hist = []
    try:
        hist = trade_api.history_positions(t["bx_symbol"], t.get("position_id"))
    except Exception as e:  # noqa: BLE001
        _log(f"[BX_LIVE] history lookup {t['bx_symbol']} failed: {str(e)[:120]}")
    h = next((x for x in hist if str(x.get("positionId")) == str(t.get("position_id"))), None)
    close_px = _f((h or {}).get("closePrice"))
    fee, funding = _f((h or {}).get("fee")) or 0.0, _f((h or {}).get("funding")) or 0.0
    realized = _f((h or {}).get("realizedPNL"))
    if realized is None and close_px:
        realized = (close_px - float(t["entry_px"])) * float(t.get("qty") or 0)
    pnl = (realized or 0.0) - abs(fee) + funding if h else None
    ret = round((close_px / float(t["entry_px"]) - 1) * 100 - 0.12, 4) if close_px else None
    conn.execute("UPDATE shadow_trades SET status='closed', exit_time=?, exit_px=?, exit_reason=?, ret_pct=?, "
                 "pnl_usd=?, fees_usd=?, funding_usd=? WHERE id=?",
                 (now.isoformat(), close_px, reason, ret, pnl, fee, funding, t["id"]))
    conn.commit()
    return {"symbol": t["bx_symbol"], "reason": reason, "pnl_usd": pnl, "exit_px": close_px}


def manage_open(trade_api, conn, now: datetime, radar4h: Dict[str, dict], meta_all: Dict[str, dict],
                market: Callable[[str], dict] = live_market) -> Dict[str, Any]:
    """Close-only: detect exchange closes (SL / liquidation), repair a missing SL, apply the pilot exits.
    Returns {closed, repaired, unrealized_usd, problems}."""
    out: Dict[str, Any] = {"closed": [], "repaired": [], "unrealized_usd": 0.0, "problems": []}
    for t in open_live_trades(conn):
        sym = t["bx_symbol"]
        try:
            pos = next((p for p in trade_api.pending_positions(sym)
                        if str(p.get("positionId")) == str(t.get("position_id"))), None)
            if not pos:
                out["closed"].append(_close_record(trade_api, conn, t, "exchange_close (Hard SL / liquidation)", now))
                continue
            out["unrealized_usd"] += (_f(pos.get("unrealizedPNL")) or 0.0) - abs(_f(pos.get("fee")) or 0.0) \
                + (_f(pos.get("funding")) or 0.0)
            if not [o for o in trade_api.tpsl_pending(sym, t.get("position_id")) if _f(o.get("slPrice"))]:
                try:
                    qp = int((meta_all.get(sym) or {}).get("quote_precision") or 8)
                    trade_api.place_position_sl(sym, t["position_id"], fmt(float(t["hard_sl"]), qp))
                    out["repaired"].append(sym)
                except Exception as e:  # noqa: BLE001
                    out["problems"].append({"code": "BX_NO_SL", "coin": sym, "msg": str(e)[:160]})
                    trade_api.flash_close(t["position_id"])
                    out["closed"].append(_close_record(trade_api, conn, t, "no_sl_closed", now))
                    continue
            try:
                live = market(sym)
            except Exception:  # noqa: BLE001
                live = None
            why = exit_reason(t, radar4h.get(sym), meta_all.get(sym), live, now)
            if why:
                trade_api.flash_close(t["position_id"])
                out["closed"].append(_close_record(trade_api, conn, t, why, now))
        except Exception as e:  # noqa: BLE001
            out["problems"].append({"code": "BX_MANAGE_ERROR", "coin": sym, "msg": str(e)[:160]})
    return out


# ---------------------------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------------------------
def _trade_api(dry: bool = False):
    from bx_trade import BXTrade
    return BXTrade(dry_run=dry)


def run_entries(now: Optional[datetime] = None, trade_api=None, egress: Optional[dict] = None,
                nav_fn: Callable[[], Optional[float]] = hl_nav, market: Callable[[str], dict] = live_market,
                tiers_fn=None, conn=None) -> Dict[str, Any]:
    """08:56 HKT: approved BX candidates -> live entries (Base / 4H NewToken) or live pending (Chase).
    GIIQ-SoT-5 ADD-1: BX radar freshness check before any orders."""
    import bx_egress
    import bx_radar
    import bx_trade
    now = now or _now()
    egress = egress if egress is not None else bx_egress.check()
    ok, why = live_gate(egress, bx_trade.keys_present(), breaker_state())
    rep: Dict[str, Any] = {"job": "entries", "ts": now.isoformat(), "live": ok, "gate": why,
                           "entered": [], "pending": [], "skipped": []}
    cand_doc = _read(candidates_path()) or {}
    cands = cand_doc.get("candidates") or [] if cand_doc.get("date") == hkt_date(now) else []
    
    # GIIQ-SoT-5 ADD-1: check BX radar freshness before any orders
    if ok:
        radar_1d = bx_radar.load_radar("1d")
        radar_4h = bx_radar.load_radar("4h")
        radar_ok, radar_why = bx_radar_fresh(radar_1d, radar_4h, now)
        if not radar_ok:
            rep["live"] = False
            rep["gate"].append(f"[BX_ALERT] {radar_why}")
            rep["skipped"] = [{"symbol": c["symbol"], "reason": radar_why} for c in cands]
            _log(f"[BX_ALERT] entries not sent: {radar_why}")
            return rep
    if not ok:
        rep["skipped"] = [{"symbol": c["symbol"], "reason": "; ".join(why)} for c in cands]
        _log(f"[BX_LIVE] entries not sent: {'; '.join(why)}")
        return rep
    own = conn is None
    conn = conn or connect()
    try:
        api = trade_api or _trade_api()
        try:
            acct = api.account()
        except Exception as e:  # noqa: BLE001  bad key / IP not whitelisted / down -> refuse
            rep["live"] = False
            rep["gate"] = [f"signed account read failed: {str(e)[:160]}"]
            return rep
        hl = nav_fn()
        nav = (hl + bx_equity(acct)) if hl else None
        baseline_nav(nav)
        meta_all = {m["bx_symbol"]: m for m in (bx_radar.load_meta().get("scanned") or [])}
        tiers_fn = tiers_fn or bx_trade.position_tiers
        
        # GIIQ-SoT-5 ADD-2: calculate BX margin used for the total margin cap
        open_trades = open_live_trades(conn)
        bx_margin_used = sum(_f(t.get("margin_used")) or 0.0 for t in open_trades)
        
        pend = load_live_pending()
        for c in cands:
            appr = approval_for(c["symbol"], now)
            if not appr:
                rep["skipped"].append({"symbol": c["symbol"], "reason": "no ENTRY_DESK approval"})
                continue
            if c["type"] == "Chase":
                rep["pending"].append(create_live_pending(pend, c, appr, now))
                continue
            _try_enter(api, conn, c, meta_all, acct, nav, bx_margin_used, now, market, tiers_fn, rep)
        save_live_pending(pend)
    finally:
        if own:
            conn.close()
    return rep


def _try_enter(api, conn, c, meta_all, acct, nav, bx_margin_used, now, market, tiers_fn, rep, kind=None) -> bool:
    meta = meta_all.get(c["symbol"]) or {}
    try:
        live = market(c["symbol"])
        tiers = tiers_fn(c["symbol"])
    except Exception as e:  # noqa: BLE001
        rep["skipped"].append({"symbol": c["symbol"], "reason": f"market data failed: {str(e)[:120]}"})
        return False
    chk = check_entry(c, meta, live, nav, _f(acct.get("available")), open_live_trades(conn),
                      bx_margin_used, approval_for(c["symbol"], now), tiers)
    if not chk["ok"]:
        rep["skipped"].append({"symbol": c["symbol"], "reason": chk["reason"]})
        return False
    try:
        res = execute_entry(api, c, meta, chk["plan"], now, conn, counted=True, kind=kind)
    except Exception as e:  # noqa: BLE001
        rep["skipped"].append({"symbol": c["symbol"], "reason": f"order error: {str(e)[:160]}"})
        _log(f"[BX_ALERT] {c['symbol']} order error: {str(e)[:160]}")
        return False
    rep["entered"].append(res)
    return res.get("status") in ("filled", "dry_run")


def live_pending_path() -> Path:
    return OUT_DIR / "bx_live_pending.json"


def load_live_pending() -> List[dict]:
    return list((_read(live_pending_path()) or {}).get("entries") or [])


def save_live_pending(entries: List[dict]) -> None:
    _write(live_pending_path(), {"updated_at": _now().isoformat(), "entries": entries})


def create_live_pending(entries: List[dict], cand: dict, approval: dict, now: datetime) -> dict:
    """GIIQ-SoT-5: size/leverage from approval instead of fixed 1%/3x."""
    from pending_entries import CONTINUATION, create_pending
    size_pct = _f(approval.get("size_pct")) or 2.0
    leverage = int(_f(approval.get("leverage")) or 2)
    rec, created = create_pending(entries, cand["symbol"], CONTINUATION,
                                  {"size_pct": size_pct, "leverage": leverage, "reason": approval.get("reason")},
                                  {"tier": cand.get("tier"), "type": "Chase"}, {"tf": "1d"}, now)
    if created:
        rec["cand"] = cand
        rec["approved_at"] = approval.get("ts")
    return {"symbol": cand["symbol"], "created": created, "id": rec.get("id")}


def run_manage(job: str, now: Optional[datetime] = None, trade_api=None, egress: Optional[dict] = None,
               nav_fn: Callable[[], Optional[float]] = hl_nav, market: Callable[[str], dict] = live_market,
               tiers_fn=None, conn=None) -> Dict[str, Any]:
    """4h / hourly: exits + SL repair (close-only), live pending Chase fills (4h, full gate), breaker."""
    import bx_egress
    import bx_radar
    import bx_shadow as S
    import bx_trade
    now = now or _now()
    rep: Dict[str, Any] = {"job": job, "ts": now.isoformat()}
    cok, cwhy = close_gate(bx_trade.keys_present())
    own = conn is None
    conn = conn or connect()
    try:
        if not open_live_trades(conn) and not load_live_pending():
            rep["note"] = "no live BX positions or pendings"
            if not cok:
                return rep
        if not cok:
            rep["gate"] = cwhy
            return rep
        api = trade_api or _trade_api()
        meta_all = {m["bx_symbol"]: m for m in (bx_radar.load_meta().get("scanned") or [])}
        r4h = S._rows_by_symbol(bx_radar.load_radar("4h"))
        man = manage_open(api, conn, now, r4h, meta_all, market)
        rep.update(closed=man["closed"], repaired=man["repaired"], problems=man["problems"])
        # breaker (live P&L = closed + open unrealized)
        try:
            acct = api.account()
            hl = nav_fn()
            nav = (hl + bx_equity(acct)) if hl else None
        except Exception as e:  # noqa: BLE001
            acct, nav = {}, None
            rep.setdefault("problems", []).append({"code": "BX_ACCOUNT", "coin": None, "msg": str(e)[:160]})
        base = baseline_nav(nav)
        br = breaker_check(realized_live_pnl(conn), man["unrealized_usd"], base)
        rep["breaker"] = br
        if br["trip"] and not breaker_state().get("tripped"):
            rep["breaker_state"] = trip_breaker(br, now)
        # pending Chase fills (4h job only; the full live gate applies, incl. the breaker)
        if job == "4h":
            egress = egress if egress is not None else bx_egress.check()
            ok, why = live_gate(egress, bx_trade.keys_present(), breaker_state())
            rep["pending"] = run_live_pending(api, conn, now, ok, why, meta_all, acct, nav, market,
                                              tiers_fn or bx_trade.position_tiers)
    finally:
        if own:
            conn.close()
    return rep


def run_live_pending(api, conn, now, gate_ok, gate_why, meta_all, acct, nav, market, tiers_fn) -> List[dict]:
    import bx_radar
    import bx_shadow as S
    from pending_entries import evaluate
    pend = load_live_pending()
    r1d = S._rows_by_symbol(bx_radar.load_radar("1d"))
    held = {t["bx_symbol"] for t in open_live_trades(conn)}
    out = []
    for rec in [e for e in pend if e.get("status") == "pending"]:
        sym = rec["symbol"]
        row = r1d.get(sym) or {}
        bnd = {"tf": "1d", "lower": _f(row.get("lower")), "filter": _f(row.get("filter")), "close": _f(row.get("close")),
               "trend": row.get("trend"), "bar_time": row.get("bar_time")}
        bar = {"t": row.get("bar_time"), "l": row.get("low"), "c": row.get("close")} if row else None
        mid = _f((meta_all.get(sym) or {}).get("price"))
        action, reason, upd = evaluate(rec, bnd, mid, now, held, bar)
        rec.update(upd)
        rec["last_check"] = now.isoformat()
        if action in ("expire", "cancel"):
            rec["status"] = "expired" if action == "expire" else "cancelled"
        elif action == "trigger":
            if not gate_ok:
                reason += f" | not sent: {'; '.join(gate_why)}"
            else:
                c = dict(rec.get("cand") or {"symbol": sym})
                sub: Dict[str, Any] = {"skipped": [], "entered": []}
                # the approval is the one given when the pending was created
                if _try_enter_pending(api, conn, c, meta_all, acct, nav, now, market, tiers_fn, sub, rec):
                    rec["status"] = "filled"
                reason += f" | {sub}"
        out.append({"symbol": sym, "action": action, "reason": reason[:300]})
    save_live_pending(pend)
    return out


def _try_enter_pending(api, conn, c, meta_all, acct, nav, now, market, tiers_fn, rep, rec) -> bool:
    meta = meta_all.get(c["symbol"]) or {}
    try:
        live = market(c["symbol"])
        tiers = tiers_fn(c["symbol"])
    except Exception as e:  # noqa: BLE001
        rep["skipped"].append({"symbol": c["symbol"], "reason": f"market data failed: {str(e)[:120]}"})
        return False
    
    # Get size/leverage from the pending record's approval
    approval = {"decision": "approve", "source": "claude", "ts": rec.get("approved_at"),
                "size_pct": rec.get("size_pct", 2.0), "leverage": rec.get("leverage", 2)} if rec.get("approved_at") else None
    
    # Calculate BX margin used for the total margin cap
    open_trades = open_live_trades(conn)
    bx_margin_used = sum(_f(t.get("margin_used")) or 0.0 for t in open_trades)
    
    chk = check_entry(c, meta, live, nav, _f(acct.get("available")), open_trades,
                      bx_margin_used, approval, tiers)
    if not chk["ok"]:
        rep["skipped"].append({"symbol": c["symbol"], "reason": chk["reason"]})
        return False
    res = execute_entry(api, c, meta, chk["plan"], now, conn, counted=True, kind="Chase")
    rep["entered"].append(res)
    return res.get("status") == "filled"


def status(conn=None) -> Dict[str, Any]:
    """For /health and the cockpit exit health: switches, breaker, open live positions (no secrets)."""
    import bx_trade
    own = conn is None
    conn = conn or connect()
    try:
        opens = [{k: t.get(k) for k in ("bx_symbol", "kind", "entry_time", "entry_px", "hard_sl", "qty", "position_id")}
                 for t in open_live_trades(conn)]
        closed = [dict(r) for r in conn.execute(
            "SELECT bx_symbol, exit_time, exit_reason, pnl_usd FROM shadow_trades WHERE mode='live' AND status='closed' "
            "ORDER BY exit_time DESC LIMIT 20")]
        realized = realized_live_pnl(conn)
    finally:
        if own:
            conn.close()
    st = _read(OUT_DIR / "bx_live_state.json") or {}
    return {"bx_enabled": env_on("BX_ENABLED", "1"), "bx_live": env_on("BX_LIVE", "0"),
            "keys_present": bx_trade.keys_present(), "breaker": breaker_state(), "baseline_nav": st.get("baseline_nav"),
            "realized_pnl_usd": round(realized, 4), "open": opens, "closed_recent": closed,
            "pending": [e for e in load_live_pending() if e.get("status") == "pending"],
            "rules": {"margin_pct_nav": MARGIN_PCT_NAV, "leverage": LEVERAGE, "max_open": MAX_OPEN,
                      "max_new_per_day": MAX_NEW_PER_DAY, "breaker_pct_nav": BREAKER_PCT_NAV}}


# ---------------------------------------------------------------------------------------------
# dry run: the exact order payloads, nothing sent
# ---------------------------------------------------------------------------------------------
EXAMPLE_CAND = {"symbol": "FOOUSDT", "coin": "FOO", "type": "Base", "gc_tf": "1d", "tier": "small",
                "close": 2.0, "hard_sl": 1.86, "hard_sl_rule": "4h_filter"}
EXAMPLE_META = {"bx_symbol": "FOOUSDT", "ex": "BX", "asset_class": "crypto", "liq_tier": "tradeable",
                "vol24h_usd": 8_000_000, "spread_bp": 4.0, "gc_tf": "1d", "base_precision": 1, "quote_precision": 4,
                "min_qty": "1", "max_leverage": 50, "api_supported": True}
EXAMPLE_LIVE = {"price": 2.0, "bid": 1.9996, "ask": 2.0004, "spread_bp": 4.0, "vol24h": 8_000_000}
EXAMPLE_TIERS = [{"startValue": "0", "endValue": "50000", "leverage": 50, "maintenanceMarginRate": "0.01"}]


def dry_run(cand: Optional[dict] = None, meta: Optional[dict] = None, live: Optional[dict] = None,
            nav: float = 10_000.0, tiers: Optional[List[dict]] = None) -> Dict[str, Any]:
    from bx_trade import BXTrade
    cand, meta, live = cand or EXAMPLE_CAND, meta or EXAMPLE_META, live or EXAMPLE_LIVE
    chk = check_entry(cand, meta, live, nav, 1_000.0, [], 0, {"decision": "approve", "source": "claude"},
                      tiers or EXAMPLE_TIERS)
    if not chk["ok"]:
        return {"ok": False, "reason": chk["reason"]}
    api = BXTrade(dry_run=True, api_key="DRYRUNKEY", secret="DRYRUNSECRET", clock=lambda: 1_790_000_000.0)
    api.set_isolated(cand["symbol"])
    api.set_leverage(cand["symbol"], LEVERAGE)
    api.open_long(cand["symbol"], chk["plan"]["qty"], chk["plan"]["limit_price"], chk["plan"]["sl_price"],
                  "giiqbx2609300856abc123")
    api.place_position_sl(cand["symbol"], "<positionId>", chk["plan"]["sl_price"])
    api.flash_close("<positionId>")
    return {"ok": True, "plan": chk["plan"], "requests": api.recorded,
            "note": "requests 1-3 are the entry; 4 is sent only if the attached SL is missing after the fill; "
                    "5 is the close used by exits (and immediately if protection cannot be verified)"}


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Bitunix live pilot")
    ap.add_argument("job", choices=["candidates", "entries", "4h", "1h", "status", "dry-run"])
    a = ap.parse_args(argv)
    if a.job == "candidates":
        res = build_candidates()
    elif a.job == "entries":
        res = run_entries()
    elif a.job in ("4h", "1h"):
        res = run_manage(a.job)
    elif a.job == "status":
        res = status()
    else:
        res = dry_run()
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
