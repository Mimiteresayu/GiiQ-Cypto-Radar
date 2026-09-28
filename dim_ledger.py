#!/usr/bin/env python3
"""GIIQ measurement ledger — does each 維度 (dimension) actually help?

SQLite on the Railway volume (out/giiq_ledger.db, override DIM_LEDGER_PATH).

  signals      one row per candidate per HKT day (approved OR vetoed = shadow tracking)
  dim_scores   every dimension score: source 'railway' (quant) or 'claude' (qualitative)
  decisions    Claude / fallback APPROVE|VETO per signal
  outcomes     forward result from the signal bar's close, closed 1D bars only:
               ret_1d/3d/7d, 7-day MFE/MAE, Hard-SL hit, and ret_7d_sl = the 7-day result of a
               simple "hold 7 days unless the Hard SL is hit" trade (the primary metric)
  market_ctx   daily HL funding / OI / volume per coin (for day-over-day OI change)

report() answers, per dimension: rank correlation (IC) between score and outcome, results by
score bucket, and "veto lift" (would vetoing score<0 have avoided worse trades?). It also compares
Claude-approved vs vetoed vs all signals. Built for TaaS: rows carry sot/strategy ids and nothing
account-specific, so the same ledger can back a public track record later.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
DAY_MS = 86_400_000
MIN_N = int(os.environ.get("DIM_MIN_N", "30"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_date TEXT NOT NULL,            -- HKT date YYYY-MM-DD of the 08:05 candidate run
    symbol TEXT NOT NULL,
    type TEXT,                            -- Base | Chase
    tier TEXT,
    ref_close REAL,                       -- signal bar close (closed 1D)
    ref_bar_time INTEGER,                 -- open time (ms) of that 1D bar
    hard_sl REAL,
    entry_ref REAL,
    already_held INTEGER DEFAULT 0,
    sot TEXT,
    strategy TEXT DEFAULT 'giiq-gc',
    dim_version TEXT,
    created_at TEXT,
    UNIQUE(signal_date, symbol)
);
CREATE TABLE IF NOT EXISTS dim_scores (
    signal_id INTEGER NOT NULL,
    source TEXT NOT NULL,                 -- railway | claude
    dim TEXT NOT NULL,
    score REAL,
    raw TEXT,
    PRIMARY KEY (signal_id, source, dim)
);
CREATE TABLE IF NOT EXISTS decisions (
    signal_id INTEGER NOT NULL,
    source TEXT NOT NULL,                 -- claude | fallback
    action TEXT,                          -- approve | veto
    kind TEXT,                            -- BASE | CONTINUATION | ADD_ON (as sent)
    rule TEXT,                            -- veto rule id (V1_WEAK_4H_BREAKOUT ...) or NULL
    size_pct REAL,
    leverage REAL,
    reason TEXT,
    decided_at TEXT,
    PRIMARY KEY (signal_id, source)
);
CREATE TABLE IF NOT EXISTS outcomes (
    signal_id INTEGER PRIMARY KEY,
    ret_1d REAL, ret_3d REAL, ret_7d REAL,
    mfe_7d REAL, mae_7d REAL,
    sl_hit INTEGER, sl_hit_day INTEGER,
    ret_7d_sl REAL,
    bars_after INTEGER,
    complete INTEGER DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS market_ctx (
    snap_date TEXT NOT NULL,
    coin TEXT NOT NULL,
    funding REAL, oi REAL, day_ntl_vlm REAL, mark REAL,
    PRIMARY KEY (snap_date, coin)
);
CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(signal_date);
"""


def db_path() -> str:
    return os.environ.get("DIM_LEDGER_PATH") or str(ROOT / "out" / "giiq_ledger.db")


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    p = path or db_path()
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(decisions)")}
    if "rule" not in cols:  # ledgers created before the veto-rule column
        conn.execute("ALTER TABLE decisions ADD COLUMN rule TEXT")
        conn.commit()
    return conn


def backfill_db_path() -> str:
    """Separate DB for the historical replay so it never mixes with (or overwrites) live signals."""
    return os.environ.get("DIM_BACKFILL_PATH") or str(Path(db_path()).with_name("giiq_ledger_backfill.db"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def _signal_id(conn: sqlite3.Connection, signal_date: str, symbol: str, create: bool = True) -> Optional[int]:
    row = conn.execute("SELECT id FROM signals WHERE signal_date=? AND symbol=?", (signal_date, symbol)).fetchone()
    if row:
        return int(row["id"])
    if not create:
        return None
    cur = conn.execute("INSERT INTO signals(signal_date, symbol, created_at) VALUES (?,?,?)",
                       (signal_date, symbol, _now()))
    return int(cur.lastrowid)


def record_signals(conn: sqlite3.Connection, signal_date: str, candidates: List[dict], dims_result: dict,
                   ref_bar_times: Dict[str, int], sot: str = "") -> Dict[str, int]:
    """Upsert one signal per candidate + its railway dimension scores. Returns symbol -> id."""
    by_sym = {c["symbol"]: c for c in dims_result.get("candidates") or []}
    ids: Dict[str, int] = {}
    for c in candidates or []:
        sym = str(c.get("symbol") or "").upper()
        if not sym:
            continue
        sid = _signal_id(conn, signal_date, sym)
        conn.execute(
            """UPDATE signals SET type=?, tier=?, ref_close=?, ref_bar_time=?, hard_sl=?, entry_ref=?,
               already_held=?, sot=?, dim_version=? WHERE id=?""",
            (c.get("type"), c.get("tier"), _f(c.get("close_1d")), ref_bar_times.get(sym),
             _f(c.get("hard_sl")), _f(c.get("entry_ref")), int(bool(c.get("already_held"))),
             sot, dims_result.get("dim_version"), sid))
        for name, d in ((by_sym.get(sym) or {}).get("dims") or {}).items():
            conn.execute("INSERT OR REPLACE INTO dim_scores(signal_id, source, dim, score, raw) VALUES (?,?,?,?,?)",
                         (sid, "railway", name, d.get("score"), json.dumps(d.get("raw"), default=str)))
        ids[sym] = sid
    conn.commit()
    return ids


def record_decisions(conn: sqlite3.Connection, signal_date: str, decisions: List[dict],
                     source: str = "claude") -> int:
    """Store normalised decisions (symbol/decision keys, see decisions.normalize) + Claude dims."""
    n = 0
    for d in decisions or []:
        sym = str(d.get("symbol") or "").upper()
        act = str(d.get("decision") or "").lower()
        if not sym or act not in ("approve", "veto"):
            continue
        sid = _signal_id(conn, signal_date, sym)
        conn.execute(
            "INSERT OR REPLACE INTO decisions(signal_id, source, action, kind, rule, size_pct, leverage, reason, decided_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, source, act, d.get("type"), d.get("rule"), _f(d.get("size_pct")), _f(d.get("leverage")),
             str(d.get("reason") or "")[:500], d.get("timestamp") or _now()))
        for name, score in (d.get("dims") or {}).items():
            s = _f(score)
            if s is None or not str(name).strip():
                continue
            conn.execute("INSERT OR REPLACE INTO dim_scores(signal_id, source, dim, score, raw) VALUES (?,?,?,?,?)",
                         (sid, "claude", str(name)[:32], max(-2.0, min(2.0, s)), None))
        n += 1
    conn.commit()
    return n


def record_market_ctx(conn: sqlite3.Connection, snap_date: str, ctxs: Dict[str, dict]) -> int:
    rows = [(snap_date, k, v.get("funding"), v.get("oi"), v.get("day_ntl_vlm"), v.get("mark"))
            for k, v in (ctxs or {}).items()]
    conn.executemany("INSERT OR REPLACE INTO market_ctx VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def previous_market_ctx(conn: sqlite3.Connection, before_date: str) -> Dict[str, dict]:
    row = conn.execute("SELECT MAX(snap_date) d FROM market_ctx WHERE snap_date < ?", (before_date,)).fetchone()
    if not row or not row["d"]:
        return {}
    return {r["coin"]: {"funding": r["funding"], "oi": r["oi"], "day_ntl_vlm": r["day_ntl_vlm"], "mark": r["mark"]}
            for r in conn.execute("SELECT * FROM market_ctx WHERE snap_date=?", (row["d"],))}


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------
def compute_outcome(bars: List[list], ref_bar_time: int, ref_close: float, hard_sl: Optional[float],
                    now_ms: int) -> Optional[dict]:
    """bars = [t,o,h,l,c,v] 1D. Uses only bars CLOSED at now_ms and strictly after the signal bar."""
    after = []
    for b in sorted(bars or [], key=lambda x: x[0]):
        try:
            t = int(b[0])
        except (TypeError, ValueError, IndexError):
            continue
        if t > ref_bar_time and t + DAY_MS <= now_ms:
            after.append((t, float(b[2]), float(b[3]), float(b[4])))
    if not ref_close or ref_close <= 0:
        return None
    after = after[:7]

    def ret(k: int) -> Optional[float]:
        return round((after[k - 1][3] / ref_close - 1) * 100, 3) if len(after) >= k else None

    out = {"ret_1d": ret(1), "ret_3d": ret(3), "ret_7d": ret(7), "bars_after": len(after),
           "mfe_7d": None, "mae_7d": None, "sl_hit": None, "sl_hit_day": None, "ret_7d_sl": None,
           "complete": int(len(after) >= 7)}
    if after:
        out["mfe_7d"] = round((max(b[1] for b in after) / ref_close - 1) * 100, 3)
        out["mae_7d"] = round((min(b[2] for b in after) / ref_close - 1) * 100, 3)
    if hard_sl and hard_sl > 0:
        hit_day = next((i + 1 for i, b in enumerate(after) if b[2] <= hard_sl), None)
        out["sl_hit"] = int(hit_day is not None) if (hit_day or out["complete"]) else None
        out["sl_hit_day"] = hit_day
        if hit_day is not None:
            out["ret_7d_sl"] = round((hard_sl / ref_close - 1) * 100, 3)
        elif out["complete"]:
            out["ret_7d_sl"] = out["ret_7d"]
    elif out["complete"]:
        out["ret_7d_sl"] = out["ret_7d"]
    return out


def fill_outcomes(conn: sqlite3.Connection, bars_1d: Dict[str, List[list]], now_ms: int,
                  fetch_bars: Optional[Callable[[str, int], List[list]]] = None) -> Dict[str, int]:
    """Update outcomes for every signal not yet complete. fetch_bars(symbol, start_ms) is the
    fallback when a coin dropped out of the radar's candle cache."""
    stats = {"checked": 0, "updated": 0, "completed": 0, "no_bars": 0}
    rows = conn.execute(
        """SELECT s.id, s.symbol, s.ref_close, s.ref_bar_time, s.hard_sl FROM signals s
           LEFT JOIN outcomes o ON o.signal_id = s.id
           WHERE s.ref_close IS NOT NULL AND s.ref_bar_time IS NOT NULL AND COALESCE(o.complete,0)=0""").fetchall()
    for r in rows:
        stats["checked"] += 1
        bars = bars_1d.get(r["symbol"])
        if (not bars or max(int(b[0]) for b in bars) <= r["ref_bar_time"]) and fetch_bars:
            try:
                bars = fetch_bars(r["symbol"], int(r["ref_bar_time"]))
            except Exception:
                bars = None
        if not bars:
            stats["no_bars"] += 1
            continue
        o = compute_outcome(bars, int(r["ref_bar_time"]), float(r["ref_close"]), r["hard_sl"], now_ms)
        if o is None:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO outcomes(signal_id, ret_1d, ret_3d, ret_7d, mfe_7d, mae_7d, sl_hit, sl_hit_day,
               ret_7d_sl, bars_after, complete, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["id"], o["ret_1d"], o["ret_3d"], o["ret_7d"], o["mfe_7d"], o["mae_7d"], o["sl_hit"], o["sl_hit_day"],
             o["ret_7d_sl"], o["bars_after"], o["complete"], _now()))
        stats["updated"] += 1
        stats["completed"] += o["complete"]
    conn.commit()
    return stats


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _ranks(xs: List[float]) -> List[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if vx == 0 or vy == 0:
        return None
    return round(cov / (vx * vy), 3)


def _stats(rows: List[dict], metric: str) -> Dict[str, Any]:
    vals = [r[metric] for r in rows if r.get(metric) is not None]
    sl = [r["sl_hit"] for r in rows if r.get("sl_hit") is not None]
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "mean": round(sum(vals) / len(vals), 3),
            "win_rate": round(sum(1 for v in vals if v > 0) / len(vals), 3),
            "sl_hit_rate": round(sum(sl) / len(sl), 3) if sl else None}


def _load_rows(conn: sqlite3.Connection, since: Optional[str], sig_type: Optional[str]) -> List[dict]:
    q = """SELECT s.id, s.signal_date, s.symbol, s.type, s.dim_version, o.ret_3d, o.ret_7d, o.ret_7d_sl, o.sl_hit,
                  o.mfe_7d, o.complete, d.action AS claude_action, d.rule AS veto_rule
           FROM signals s LEFT JOIN outcomes o ON o.signal_id=s.id
           LEFT JOIN decisions d ON d.signal_id=s.id AND d.source='claude' WHERE 1=1"""
    args: List[Any] = []
    if since:
        q += " AND s.signal_date >= ?"
        args.append(since)
    if sig_type:
        q += " AND s.type = ?"
        args.append(sig_type)
    rows = [dict(r) for r in conn.execute(q, args)]
    scores: Dict[int, Dict[str, float]] = {}
    for r in conn.execute("SELECT signal_id, source, dim, score FROM dim_scores WHERE score IS NOT NULL"):
        scores.setdefault(r["signal_id"], {})[f"{r['source']}:{r['dim']}"] = r["score"]
    for r in rows:
        r["scores"] = scores.get(r["id"], {})
        rs = [v for k, v in r["scores"].items() if k.startswith("railway:")]
        r["scores"]["railway:TOTAL"] = sum(rs) if rs else None
    return rows


def _verdict(n: int, ic: Optional[float], lift: Optional[float], min_n: int) -> str:
    if n < min_n or ic is None:
        return f"insufficient (n={n} < {min_n})" if n < min_n else "insufficient"
    if ic >= 0.10 and (lift is None or lift > 0):
        return "helpful"
    if ic <= -0.10:
        return "harmful (inverse)"
    return "no edge yet"


def _veto_rule_stats(done: List[dict], approved: Dict[str, Any], metric: str) -> List[dict]:
    """Per veto rule: how many signals it blocked and how those blocked signals would have done.
    A rule earns its place when its blocked signals did WORSE than the approved ones (avoided_loss > 0)."""
    rules: Dict[str, List[dict]] = {}
    for r in done:
        if r.get("claude_action") == "veto":
            rules.setdefault(r.get("veto_rule") or "(no rule id)", []).append(r)
    out = []
    for rule, rows in sorted(rules.items()):
        st = _stats(rows, metric)
        avoided = (round(approved["mean"] - st["mean"], 3)
                   if st.get("n") and approved.get("n") else None)
        out.append({"rule": rule, "blocked": len(rows), "blocked_outcome": st,
                    "avoided_vs_approved": avoided,
                    "symbols": sorted({f"{x['signal_date']} {x['symbol']}" for x in rows})[-10:]})
    return out


def report(conn: sqlite3.Connection, metric: str = "ret_7d_sl", since: Optional[str] = None,
           sig_type: Optional[str] = None, min_n: int = MIN_N) -> Dict[str, Any]:
    rows = _load_rows(conn, since, sig_type)
    done = [r for r in rows if r.get(metric) is not None]
    dims = sorted({k for r in rows for k in r["scores"]})
    per_dim = []
    for k in dims:
        pairs = [(r["scores"][k], r[metric]) for r in done if r["scores"].get(k) is not None]
        n = len(pairs)
        ic = spearman([p[0] for p in pairs], [p[1] for p in pairs]) if n >= 3 else None
        ic3_pairs = [(r["scores"][k], r["ret_3d"]) for r in rows
                     if r["scores"].get(k) is not None and r.get("ret_3d") is not None]
        ic3 = spearman([p[0] for p in ic3_pairs], [p[1] for p in ic3_pairs]) if len(ic3_pairs) >= 3 else None
        with_s = [r for r in done if r["scores"].get(k) is not None]
        neg = [r for r in with_s if r["scores"][k] < 0]
        zero = [r for r in with_s if r["scores"][k] == 0]
        pos = [r for r in with_s if r["scores"][k] > 0]
        keep = [r for r in with_s if r["scores"][k] >= 0]
        sk, sn = _stats(keep, metric), _stats(neg, metric)
        lift = round(sk["mean"] - sn["mean"], 3) if sk.get("n") and sn.get("n") else None
        per_dim.append({"dim": k, "n": n, "ic": ic, "ic_ret3d": ic3, "veto_lift": lift,
                        "buckets": {"neg": _stats(neg, metric), "zero": _stats(zero, metric), "pos": _stats(pos, metric)},
                        "verdict": _verdict(n, ic, lift, min_n)})
    per_dim.sort(key=lambda d: (d["ic"] is None, -(d["ic"] or 0)))
    decided = [r for r in done if r.get("claude_action")]
    appr = [r for r in decided if r["claude_action"] == "approve"]
    veto = [r for r in decided if r["claude_action"] == "veto"]
    all_s, appr_s, veto_s = _stats(done, metric), _stats(appr, metric), _stats(veto, metric)
    edge = round(appr_s["mean"] - all_s["mean"], 3) if appr_s.get("n") and all_s.get("n") else None
    if not decided:
        claude_verdict = "no decisions recorded"
    elif len(decided) < min_n or edge is None:
        claude_verdict = f"insufficient (n={len(decided)} < {min_n})"
    else:
        claude_verdict = "helpful" if edge > 0.5 else "harmful" if edge < -0.5 else "no edge yet"
    return {
        "generated_at": _now(), "metric": metric, "since": since, "type": sig_type, "min_n": min_n,
        "signals_total": len(rows), "signals_with_outcome": len(done),
        "dimensions": per_dim,
        "claude": {"all_signals": all_s, "approved": appr_s, "vetoed": veto_s,
                   "approve_edge_vs_all": edge, "verdict": claude_verdict},
        "by_type": {t: _stats([r for r in done if r.get("type") == t], metric) for t in ("Base", "Chase")},
        "veto_rules": _veto_rule_stats(done, appr_s, metric),
        "how_to_read": ("ic = rank correlation of score vs outcome (>0.10 useful, <-0.10 inverse). "
                        "veto_lift = mean outcome of score>=0 minus score<0 (positive = vetoing score<0 would "
                        "have avoided worse signals). Outcome = 7d return, or Hard-SL loss if hit first."),
    }


def signal_rows(conn: sqlite3.Connection, signal_date: str) -> List[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT s.*, o.ret_7d_sl, o.complete, d.action AS claude_action FROM signals s
           LEFT JOIN outcomes o ON o.signal_id=s.id
           LEFT JOIN decisions d ON d.signal_id=s.id AND d.source='claude'
           WHERE s.signal_date=? ORDER BY s.symbol""", (signal_date,))]
