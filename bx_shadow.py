#!/usr/bin/env python3
"""Bitunix shadow book — simulated entries / exits for BX-only signals. NO ORDERS, NO KEYS.

Harbor-approved rules (2026-09-29, updated 2026-10-08):
  Signals      same locked GC rules as HL. Base = 1D dual cross up above 1D Upper (NO Green requirement).
               Chase = 1D Green + 4H Green + 4H cross above 4H Upper -> simulated pending pullback
               (CONTINUATION / ADD_ON, N / N+1 confirmation via pending_entries.evaluate, 7-day TTL).
               New tokens (history too short for a 1D GC): 4H cross + 4H Green; 1H cross + 1H Green when even
               4H is too short. gc_tf=1h signals are WATCH-ONLY and never count in the 30-signal test.
  Counted      only tradeable-tier signals on a 1D or 4H GC (bx_universe.counts_in_test).
  Size         normal: 2% NAV margin at 3x (same as the HL fallback); new token: 1% NAV at 3x.
               Downsized to <= 0.5% of 24h volume. Shadow caps: total margin <= 30% NAV, coin notional
               <= 20% NAV, <= 3 new fills per HKT day.
  Fill price   BX last price x (1 +/- slippage), slippage = max(5 bp, spread / 2); fees 0.06% taker each side.
  Hard SL      tier by CoinGecko mcap (unknown -> Tiny): Mega/Large 4H Lower, Small/Tiny 4H Filter;
               new tokens 4H Filter, or 1H Lower when gc_tf=1h. No SL level -> no entry (fail-closed).
  Exits        Small/Tiny 1H close < 1H Lower; Mega/Large 4H close < 4H Filter; Hard SL on any closed bar low;
               new tokens: 4H close < 4H Filter (1H Lower for gc_tf=1h), 5-day time stop, liquidity exit
               when 24h vol < $1M or spread > 30 bp.
Ledger: out/bx_shadow_ledger.db (same schema as giiq_ledger.db via dim_ledger.connect + bx tables). The live
HL ledger is only ever opened READ-ONLY here (for the comparison baseline).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import bx_universe as U  # noqa: E402

OUT_DIR = Path(os.environ.get("BX_OUT_DIR") or (ROOT / "out"))
HKT = timezone(timedelta(hours=8))
STRATEGY = "giiq-gc-bx-shadow"
SIZE_PCT = 2.0
SIZE_PCT_NEW = 1.0
LEVERAGE = 3.0
TOTAL_MARGIN_CAP_PCT = 30.0
COIN_NOTIONAL_CAP_PCT = 20.0
MAX_FILLS_PER_DAY = 3
FEE_BP = 6.0
NEW_TOKEN_TIME_STOP_D = 5
LIQ_EXIT_VOL = 200_000.0
LIQ_EXIT_SPREAD_BP = 30.0
ADDON_MIN_GAIN_PCT = 10.0
MIN_N = 30
BAR_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

SCHEMA = """
CREATE TABLE IF NOT EXISTS bx_signal_meta (
    signal_id INTEGER PRIMARY KEY,
    ex TEXT DEFAULT 'BX', bx_symbol TEXT, gc_tf TEXT, liq_tier TEXT, counted INTEGER,
    asset_class TEXT, asset_age TEXT, spread_bp REAL, slip_bp REAL
);
CREATE TABLE IF NOT EXISTS shadow_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER, symbol TEXT, bx_symbol TEXT, kind TEXT, gc_tf TEXT, tier TEXT, counted INTEGER,
    entry_time TEXT, entry_px REAL, size_pct_nav REAL, leverage REAL, hard_sl REAL, exit_rule TEXT,
    time_stop_at TEXT, exit_time TEXT, exit_px REAL, exit_reason TEXT, fees_bp REAL, slip_bp REAL,
    ret_pct REAL, pnl_nav_pct REAL, status TEXT DEFAULT 'open', note TEXT
);
CREATE INDEX IF NOT EXISTS idx_shadow_status ON shadow_trades(status);
"""


# ---------------------------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------------------------
def db_path() -> str:
    return os.environ.get("BX_LEDGER_PATH") or str(OUT_DIR / "bx_shadow_ledger.db")


LIVE_COLS = {"mode": "TEXT DEFAULT 'shadow'", "order_id": "TEXT", "client_id": "TEXT", "position_id": "TEXT",
             "qty": "REAL", "pnl_usd": "REAL", "fees_usd": "REAL", "funding_usd": "REAL", "sl_order_id": "TEXT"}
SHADOW = "COALESCE(mode,'shadow')='shadow'"


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    import dim_ledger
    conn = dim_ledger.connect(path or db_path())   # signals / outcomes / ... in the BX file only
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(shadow_trades)")}
    for col, typ in LIVE_COLS.items():             # live pilot rows share this table, marked mode='live'
        if col not in have:
            conn.execute(f"ALTER TABLE shadow_trades ADD COLUMN {col} {typ}")
    conn.commit()
    return conn


def pending_path() -> Path:
    return OUT_DIR / "bx_shadow_pending.json"   # never the HL out/pending_entries.json


def load_pending() -> List[dict]:
    try:
        d = json.loads(pending_path().read_text(encoding="utf-8"))
        return d.get("entries", []) if isinstance(d, dict) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_pending(entries: List[dict]) -> None:
    p = pending_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"updated_at": datetime.now(timezone.utc).isoformat(), "entries": entries},
                              indent=1, default=str), encoding="utf-8")
    os.replace(tmp, p)


def _f(v: Any) -> Optional[float]:
    return U._f(v)


def hkt_date(now: datetime) -> str:
    return now.astimezone(HKT).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------------------------
# pure rules
# ---------------------------------------------------------------------------------------------
def classify_signal(meta: dict, r1d: Optional[dict], r4h: Optional[dict], r1h: Optional[dict]) -> Optional[dict]:
    """-> {type: Base|Chase|NewToken, gc_tf, row} or None. Crypto BX-only rows only; exclude tier never."""
    if meta.get("ex") != "BX" or meta.get("asset_class") != "crypto" or meta.get("liq_tier") == "exclude":
        return None
    gtf = meta.get("gc_tf")
    if gtf == "1d" and r1d:
        if r1d.get("dual_cross_up") and r1d.get("trend") == "Green":
            return {"type": "Base", "gc_tf": "1d", "row": r1d}
        if (r4h and r1d.get("trend") == "Green" and r4h.get("trend") == "Green" and r4h.get("dual_cross_up")):
            return {"type": "Chase", "gc_tf": "1d", "row": r4h}
        return None
    if gtf == "4h" and r4h and r4h.get("dual_cross_up") and r4h.get("trend") == "Green":
        return {"type": "NewToken", "gc_tf": "4h", "row": r4h}
    if gtf == "1h" and r1h and r1h.get("dual_cross_up") and r1h.get("trend") == "Green":
        return {"type": "NewToken", "gc_tf": "1h", "row": r1h}
    return None


def hard_sl(tier: str, kind: str, gc_tf: str, r4h: Optional[dict], r1h: Optional[dict]) -> Tuple[Optional[float], str]:
    if kind == "NewToken":
        if gc_tf == "1h":
            return _f((r1h or {}).get("lower")), "1h_lower"
        return _f((r4h or {}).get("filter")), "4h_filter"
    if tier in ("mega", "large"):
        return _f((r4h or {}).get("lower")), "4h_lower"
    return _f((r4h or {}).get("filter")), "4h_filter"


def exit_rule_for(tier: str, kind: str, gc_tf: str) -> str:
    if kind == "NewToken":
        return "1h_close_below_lower" if gc_tf == "1h" else "4h_close_below_filter"
    return "4h_close_below_filter" if tier in ("mega", "large") else "1h_close_below_lower"


def fill_px(price: float, slip_bp: float, side: str) -> float:
    return price * (1 + slip_bp / 10_000) if side == "buy" else price * (1 - slip_bp / 10_000)


def trade_return(entry_px: float, exit_px: float, fee_bp: float = FEE_BP) -> float:
    """% return on the position after taker fees on both sides."""
    return round(((exit_px / entry_px) - 1) * 100 - 2 * fee_bp / 100, 4)


def size_for(kind: str, meta: dict, nav_usd: Optional[float]) -> Tuple[float, str]:
    """Margin % of NAV, downsized so notional <= 0.5% of 24h volume. -> (size_pct, note)."""
    base = SIZE_PCT_NEW if kind == "NewToken" else SIZE_PCT
    cap = meta.get("max_notional_usd")
    if nav_usd and cap:
        max_pct = cap / (nav_usd * LEVERAGE) * 100
        if max_pct < base:
            return round(max_pct, 4), f"downsized to 0.5% of 24h vol (${cap:,.0f})"
    return base, ""


def caps_ok(open_trades: List[dict], symbol: str, size_pct: float, fills_today: int) -> Tuple[bool, str]:
    used = sum(float(t["size_pct_nav"] or 0) for t in open_trades)
    if used + size_pct > TOTAL_MARGIN_CAP_PCT:
        return False, f"shadow margin {used + size_pct:.1f}% > {TOTAL_MARGIN_CAP_PCT:g}% NAV"
    coin = sum(float(t["size_pct_nav"] or 0) * float(t["leverage"] or 0) for t in open_trades if t["bx_symbol"] == symbol)
    if coin + size_pct * LEVERAGE > COIN_NOTIONAL_CAP_PCT:
        return False, f"coin notional {coin + size_pct * LEVERAGE:.1f}% > {COIN_NOTIONAL_CAP_PCT:g}% NAV"
    if fills_today >= MAX_FILLS_PER_DAY:
        return False, f"{MAX_FILLS_PER_DAY} new fills already today"
    return True, ""


def exit_check(trade: dict, now: datetime, rows: Dict[str, Optional[dict]], meta: Optional[dict]) -> Optional[Tuple[str, float]]:
    """-> (reason, exit reference price) or None. rows = latest closed {'1h','4h'} radar rows for the coin.
    Hard SL is checked on the closed bar lows first (it can hit any time)."""
    sl = _f(trade.get("hard_sl"))
    for tf in ("1h", "4h"):
        r = rows.get(tf)
        if r and sl and _f(r.get("low")) is not None and r["low"] <= sl \
                and int(r.get("bar_time") or 0) * 1 >= _iso_ms(trade.get("entry_time")):
            return "hard_sl", sl
    rule = trade.get("exit_rule")
    if rule == "1h_close_below_lower":
        r = rows.get("1h")
        if r and _f(r.get("close")) is not None and _f(r.get("lower")) is not None and r["close"] < r["lower"] \
                and int(r["bar_time"]) >= _iso_ms(trade.get("entry_time")):
            return "tier_exit_1h_lower", r["close"]
    if rule == "cont_staircase_4h_filter":
        r = rows.get("4h")
        if r and _f(r.get("close")) is not None and _f(r.get("filter")) is not None and r["close"] < r["filter"]:
            bar_close = int(r.get("bar_time") or 0) + 4 * 3600 * 1000
            if bar_close > _iso_ms(trade.get("entry_time")):
                return "CONT_STAIRCASE_4h_filter", r["close"]
    if rule == "4h_close_below_filter":
        r = rows.get("4h")
        if r and _f(r.get("close")) is not None and _f(r.get("filter")) is not None and r["close"] < r["filter"] \
                and int(r["bar_time"]) >= _iso_ms(trade.get("entry_time")):
            return "tier_exit_4h_filter", r["close"]
    if trade.get("kind") == "NewToken":
        ts = _iso_ms(trade.get("time_stop_at"))
        last = _f((meta or {}).get("price")) or _f((rows.get("1h") or rows.get("4h") or {}).get("close"))
        if ts and int(now.timestamp() * 1000) >= ts and last:
            return "time_stop_5d", last
        vol, spr = _f((meta or {}).get("vol24h_usd")), _f((meta or {}).get("spread_bp"))
        if last and ((vol is not None and vol < LIQ_EXIT_VOL) or (spr is not None and spr > LIQ_EXIT_SPREAD_BP)):
            return "liquidity_exit", last
    return None


def _iso_ms(v: Any) -> int:
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return int(d.timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------------------------
def _rows_by_symbol(radar: dict) -> Dict[str, dict]:
    return {r["bx_symbol"]: r for r in (radar or {}).get("rows") or [] if r.get("bx_symbol")}


def hourly_symbols(conn: Optional[sqlite3.Connection] = None) -> List[str]:
    """BX symbols the hourly job must refresh: new tokens on a 1H GC + every open shadow position."""
    import bx_radar
    syms = [m["bx_symbol"] for m in (bx_radar.load_meta().get("scanned") or [])
            if m.get("gc_tf") == "1h" and m.get("liq_tier") != "exclude"]
    own = conn is None
    try:
        conn = conn or connect()
        syms += [r[0] for r in conn.execute("SELECT DISTINCT bx_symbol FROM shadow_trades WHERE status='open'")]
    except sqlite3.Error:
        pass
    finally:
        if own and conn is not None:
            conn.close()
    return sorted(set(syms))


def run(job: str, now: Optional[datetime] = None, nav_usd: Optional[float] = None,
        conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """job = daily | 4h | 1h. Reads the bx_radar files written just before; writes only the BX ledger and
    out/bx_shadow_pending.json. Returns a report dict."""
    import bx_radar
    import dim_ledger
    from pending_entries import ADD_ON, CONTINUATION, band, create_pending, evaluate
    now = now or datetime.now(timezone.utc)
    own = conn is None
    conn = conn or connect()
    rep: Dict[str, Any] = {"job": job, "ts": now.isoformat(), "signals": [], "opened": [], "closed": [],
                           "pending": [], "skipped": []}
    meta_all = {m["bx_symbol"]: m for m in (bx_radar.load_meta().get("scanned") or [])}
    r1d, r4h, r1h = (_rows_by_symbol(bx_radar.load_radar(tf)) for tf in ("1d", "4h", "1h"))
    open_trades = [dict(r) for r in conn.execute(f"SELECT * FROM shadow_trades WHERE status='open' AND {SHADOW}")]
    today = hkt_date(now)
    fills_today = conn.execute(f"SELECT COUNT(*) FROM shadow_trades WHERE {SHADOW} AND substr(entry_time,1,10)>=? AND note LIKE ?",
                               ((now - timedelta(days=1)).strftime("%Y-%m-%d"), f"%hkt={today}%")).fetchone()[0]

    # 1) exits first (same order as HL: exits -> re-read book -> entries)
    for t in open_trades:
        sym = t["bx_symbol"]
        hit = exit_check(t, now, {"1h": r1h.get(sym), "4h": r4h.get(sym)}, meta_all.get(sym))
        if not hit:
            continue
        reason, ref = hit
        slip = U.shadow_slippage_bp((meta_all.get(sym) or {}).get("spread_bp"))
        px = fill_px(ref, slip, "sell")
        ret = trade_return(float(t["entry_px"]), px)
        conn.execute("UPDATE shadow_trades SET status='closed', exit_time=?, exit_px=?, exit_reason=?, ret_pct=?, "
                     "pnl_nav_pct=? WHERE id=?",
                     (now.isoformat(), px, reason, ret, round(ret * float(t["size_pct_nav"]) * float(t["leverage"]) / 100, 5),
                      t["id"]))
        rep["closed"].append({"symbol": t["symbol"], "reason": reason, "ret_pct": ret})
    conn.commit()
    open_trades = [dict(r) for r in conn.execute(f"SELECT * FROM shadow_trades WHERE status='open' AND {SHADOW}")]
    held = {t["bx_symbol"] for t in open_trades}

    def open_trade(meta: dict, kind: str, gc_tf: str, sid: Optional[int], counted: bool, note: str = "") -> None:
        nonlocal fills_today
        sym = meta["bx_symbol"]
        size, dn = size_for(kind, meta, nav_usd)
        ok, why = caps_ok(open_trades, sym, size, fills_today)
        price = _f(meta.get("price")) or _f((r1h.get(sym) or r4h.get(sym) or r1d.get(sym) or {}).get("close"))
        sl, sl_rule = hard_sl(meta.get("tier") or "tiny", kind, gc_tf, r4h.get(sym), r1h.get(sym))
        if not ok:
            rep["skipped"].append({"symbol": meta["symbol"], "kind": kind, "reason": why})
            return
        if not price or not sl or sl >= price * (1 - 0.015):
            rep["skipped"].append({"symbol": meta["symbol"], "kind": kind,
                                   "reason": f"no valid Hard SL ({sl_rule}={sl}) vs price {price} (>= 1.5% below needed)"})
            return
        slip = U.shadow_slippage_bp(meta.get("spread_bp"))
        px = fill_px(price, slip, "buy")
        stop_at = (now + timedelta(days=NEW_TOKEN_TIME_STOP_D)).isoformat() if kind == "NewToken" else None
        conn.execute(
            """INSERT INTO shadow_trades(signal_id, symbol, bx_symbol, kind, gc_tf, tier, counted, entry_time, entry_px,
               size_pct_nav, leverage, hard_sl, exit_rule, time_stop_at, fees_bp, slip_bp, status, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?)""",
            (sid, meta["symbol"], sym, kind, gc_tf, meta.get("tier"), int(counted), now.isoformat(), px, size, LEVERAGE,
             sl, exit_rule_for(meta.get("tier") or "tiny", kind, gc_tf), stop_at, FEE_BP, slip,
             f"hkt={today}; {sl_rule}; {dn}; {note}".strip("; ")))
        open_trades.append({"bx_symbol": sym, "size_pct_nav": size, "leverage": LEVERAGE})
        held.add(sym)
        fills_today += 1
        rep["opened"].append({"symbol": meta["symbol"], "kind": kind, "px": round(px, 10), "size_pct": size,
                              "hard_sl": sl, "counted": counted})

    # 2) pending pullback fills (Chase) — every job that has fresh band bars
    pend = load_pending()
    for rec in [e for e in pend if e.get("status") == "pending"]:
        sym = rec["symbol"]
        tf = "4h" if rec["kind"] == ADD_ON else "1d"
        row_1d = r1d.get(sym)
        row_4h = r4h.get(sym)
        # New 3-step rule: need both 1D and 4H data
        bnd = band(rec.get("kind") or "CONTINUATION", row_1d, row_4h)
        meta = meta_all.get(sym) or {}
        action, reason, upd = evaluate(rec, bnd, _f(meta.get("price")), now, held)
        rec.update(upd)
        rec["last_check"] = now.isoformat()
        rec.setdefault("history", []).append({"t": now.isoformat(), "action": action, "reason": reason[:200]})
        rec["history"] = rec["history"][-20:]
        if action in ("expire", "cancel"):
            rec["status"] = "expired" if action == "expire" else "cancelled"
        elif action == "trigger":
            if rec["kind"] == ADD_ON:
                base = next((t for t in open_trades if t["bx_symbol"] == sym and t.get("entry_px")), None)
                gain = (float(meta.get("price") or 0) / float(base["entry_px"]) - 1) * 100 if base else None
                if gain is None or gain < ADDON_MIN_GAIN_PCT:
                    rec["history"][-1]["reason"] += f" | held (price gain {gain} < +{ADDON_MIN_GAIN_PCT:g}%)"
                    rep["pending"].append({"symbol": sym, "kind": rec["kind"], "action": "wait", "reason": "add-on gain gate"})
                    continue
            rec["status"] = "filled"
            open_trade(meta, "Chase", "1d", rec.get("signal_id"), bool(rec.get("counted")), note=rec["kind"])
        rep["pending"].append({"symbol": sym, "kind": rec["kind"], "action": action, "reason": reason[:160]})

    # 3) new signals (daily: 1D rows + 4H Chase; 4h: new tokens on 4H; 1h: new tokens on 1H, watch-only)
    for sym, meta in meta_all.items():
        s = classify_signal(meta, r1d.get(sym), r4h.get(sym), r1h.get(sym))
        if not s:
            continue
        if job == "daily" and s["gc_tf"] not in ("1d", "4h"):
            continue
        if job == "4h" and s["gc_tf"] != "4h":
            continue
        if job == "1h" and s["gc_tf"] != "1h":
            continue
        row = s["row"]
        counted = U.counts_in_test(s["gc_tf"], meta.get("liq_tier"))
        sid_row = conn.execute("SELECT id FROM signals WHERE signal_date=? AND symbol=?", (today, sym)).fetchone()
        if sid_row:
            continue  # one signal per coin per HKT day
        sl, _ = hard_sl(meta.get("tier") or "tiny", s["type"], s["gc_tf"], r4h.get(sym), r1h.get(sym))
        ids = dim_ledger.record_signals(conn, today, [{
            "symbol": sym, "type": s["type"], "tier": meta.get("tier"), "close_1d": row.get("close"),
            "hard_sl": sl, "entry_ref": row.get("upper"), "already_held": sym in held}],
            {"dim_version": STRATEGY}, {sym: row.get("bar_time")}, sot=STRATEGY)
        sid = ids.get(sym)
        conn.execute("UPDATE signals SET strategy=? WHERE id=?", (STRATEGY, sid))
        conn.execute("INSERT OR REPLACE INTO bx_signal_meta VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (sid, "BX", sym, s["gc_tf"], meta.get("liq_tier"), int(counted), meta.get("asset_class"),
                      meta.get("asset_age"), meta.get("spread_bp"), U.shadow_slippage_bp(meta.get("spread_bp"))))
        rep["signals"].append({"symbol": meta["symbol"], "type": s["type"], "gc_tf": s["gc_tf"],
                               "tier": meta.get("liq_tier"), "counted": counted})
        if s["type"] == "Chase":
            kind = ADD_ON if sym in held else CONTINUATION
            rec, created = create_pending(pend, sym, kind, {"size_pct": SIZE_PCT, "leverage": LEVERAGE,
                                                            "reason": "BX shadow Chase"},
                                          {"tier": meta.get("tier"), "type": "Chase"},
                                          {"tf": "4h" if kind == ADD_ON else "1d"}, now)
            if created:
                rec["signal_id"], rec["counted"] = sid, counted
        elif sym not in held:
            open_trade(meta, s["type"], s["gc_tf"], sid, counted)
    save_pending(pend)
    conn.commit()
    if job == "daily":
        rep["outcomes"] = dim_ledger.fill_outcomes(conn, bx_radar.load_candles("1d"), int(now.timestamp() * 1000))
    rep["open"] = conn.execute(f"SELECT COUNT(*) FROM shadow_trades WHERE status='open' AND {SHADOW}").fetchone()[0]
    if own:
        conn.close()
    return rep


# ---------------------------------------------------------------------------------------------
# comparison report (BX-only vs HL rule-only; secondary column vs Claude-approved)
# ---------------------------------------------------------------------------------------------
def _hl_rows_readonly(path: Optional[str] = None) -> Optional[List[dict]]:
    import dim_ledger
    p = path or dim_ledger.db_path()
    if not os.path.isfile(p):
        return None
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        return dim_ledger._load_rows(c, None, None)
    finally:
        c.close()


def compare(conn: Optional[sqlite3.Connection] = None, hl_path: Optional[str] = None,
            metric: str = "ret_7d_sl") -> Dict[str, Any]:
    import dim_ledger
    own = conn is None
    conn = conn or connect()
    rows = [dict(r) for r in conn.execute(
        f"""SELECT s.id, s.symbol, s.type, s.signal_date, m.gc_tf, m.liq_tier, m.counted, o.{metric} AS v, o.sl_hit
            FROM signals s JOIN bx_signal_meta m ON m.signal_id = s.id LEFT JOIN outcomes o ON o.signal_id = s.id""")]
    for r in rows:
        r[metric] = r.pop("v")
    counted = [r for r in rows if r["counted"]]
    watch = [r for r in rows if not r["counted"]]
    trades = [dict(r) for r in conn.execute("SELECT counted, ret_pct, pnl_nav_pct, exit_reason, "
                                            "COALESCE(mode,'shadow') AS mode, pnl_usd FROM shadow_trades "
                                            "WHERE status='closed'")]
    if own:
        conn.close()

    def tstats(ts: List[dict]) -> Dict[str, Any]:
        if not ts:
            return {"n": 0}
        rs = [t["ret_pct"] for t in ts if t["ret_pct"] is not None]
        return {"n": len(ts), "mean_ret_pct": round(sum(rs) / len(rs), 3) if rs else None,
                "win_rate": round(sum(1 for x in rs if x > 0) / len(rs), 3) if rs else None,
                "pnl_nav_pct": round(sum(t["pnl_nav_pct"] or 0 for t in ts), 4)}

    hl = _hl_rows_readonly(hl_path)
    hl_done = [r for r in (hl or []) if r.get(metric) is not None]
    hl_rule = dim_ledger._stats(hl_done, metric)
    hl_appr = dim_ledger._stats([r for r in hl_done if r.get("claude_action") == "approve"], metric)
    bx_c = dim_ledger._stats([r for r in counted if r.get(metric) is not None], metric)
    n_bx, n_hl = bx_c.get("n", 0), hl_rule.get("n", 0)
    if n_bx < MIN_N or n_hl < MIN_N:
        verdict = f"insufficient (BX counted n={n_bx}, HL n={n_hl}; need {MIN_N} each)"
    else:
        edge = bx_c["mean"] - hl_rule["mean"]
        verdict = "BX-only better" if edge > 0.5 else "BX-only worse" if edge < -0.5 else "no clear difference"
    return {
        "metric": metric, "min_n": MIN_N, "verdict": verdict,
        "bx_counted": bx_c, "bx_counted_by_type": {t: dim_ledger._stats(
            [r for r in counted if r["type"] == t and r.get(metric) is not None], metric) for t in ("Base", "Chase", "NewToken")},
        "bx_watch": dim_ledger._stats([r for r in watch if r.get(metric) is not None], metric),
        "bx_watch_1h": dim_ledger._stats([r for r in watch if r["gc_tf"] == "1h" and r.get(metric) is not None], metric),
        "hl_rule_only": hl_rule, "hl_claude_approved": hl_appr,
        "hl_ledger": "missing" if hl is None else "read-only",
        "shadow_trades": {"counted": tstats([t for t in trades if t["counted"] and t["mode"] == "shadow"]),
                          "watch": tstats([t for t in trades if not t["counted"] and t["mode"] == "shadow"])},
        "live_trades": dict(tstats([t for t in trades if t["mode"] == "live"]),
                            pnl_usd=round(sum(t["pnl_usd"] or 0 for t in trades if t["mode"] == "live"), 4)),
        "signals": {"total": len(rows), "counted": len(counted), "watch": len(watch)},
        "how_to_read": ("Primary: bx_counted vs hl_rule_only on the 7-day return (Hard-SL loss if hit first). "
                        "hl_claude_approved is a reference column. Watch-tier and 1H-GC signals are shown apart "
                        "and never count toward the 30-signal test."),
    }


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Bitunix shadow book (no orders)")
    ap.add_argument("job", choices=["daily", "4h", "1h", "compare"])
    ap.add_argument("--nav", type=float, default=None)
    a = ap.parse_args(argv)
    res = compare() if a.job == "compare" else run(a.job, nav_usd=a.nav)
    print(json.dumps(res, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
