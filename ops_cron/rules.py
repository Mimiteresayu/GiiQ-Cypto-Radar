"""Pure ok/problem rules for the two ops checks. No I/O: every input is an already-fetched endpoint result
`{"ok": bool, "data": <parsed JSON>, "error": str}`; the same inputs always give the same report.

Rule codes are listed in ops_cron/README.md ("Alert rules"). A report is `problem` iff `problems` is non-empty;
`info` items never alert.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

HKT = timezone(timedelta(hours=8))

# cockpit APScheduler jobs (serve.py, HKT) -> max minutes since last run = interval + grace
COCKPIT_JOB_MAX_AGE_MIN = {
    "1h_scan_exits": 75,                 # hourly :07
    "4h_scan_exits": 4 * 60 + 25,        # 00/04/08/12/16/20 :10
    "pending_entries": 4 * 60 + 25,      # right after each 4h job
    "1d_scan_candidates": 24 * 60 + 30,  # 08:05
    "executor": 24 * 60 + 30,            # 08:55
}
# bx-exec APScheduler jobs (bx_service.py, HKT). bx-exec keeps them in memory: empty after a restart.
BX_JOB_MAX_AGE_MIN = {"1h": 75, "4h": 4 * 60 + 25, "daily": 24 * 60 + 30, "entries": 24 * 60 + 30}

HL_MAX_NEW_ENTRIES_PER_DAY = 3           # exec_common.MAX_NEW_ENTRIES_PER_DAY
HL_LEV_MIN, HL_LEV_MAX = 1.0, 5.0        # exec_common.MIN_LEVERAGE / MAX_LEVERAGE
MIN_SL_DIST_PCT = 1.5                    # exec_common.MIN_SL_DIST_PCT == bx_live.MIN_SL_DIST_PCT
BX_DEFAULT_RULES = {"max_open": 2, "max_new_per_day": 1, "breaker_pct_nav": 3.0}   # bx_live pilot rules

# exit_health codes that ops_cron re-checks itself with its own thresholds (avoid double alerts)
SUPERSEDED_EXIT_HEALTH_CODES = {"JOB_MISSED"}

SL_DIST_MISS_RE = re.compile(r"SL distance -?[\d.]+%.*<\s*1\.5\s*%|Hard SL only .*<\s*1\.5\s*%")


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(v: Any) -> Optional[datetime]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _hkt(d: Optional[datetime]) -> str:
    return d.astimezone(HKT).strftime("%m-%d %H:%M HKT") if d else "never"


def hkt_day_start(now: datetime) -> datetime:
    h = now.astimezone(HKT)
    return h.replace(hour=0, minute=0, second=0, microsecond=0)


class _Report:
    def __init__(self) -> None:
        self.problems: List[dict] = []
        self.info: List[dict] = []
        self._seen: set = set()

    def problem(self, code: str, coin: Optional[str], msg: str, source: str) -> None:
        key = (code, coin)
        if key in self._seen:
            return
        self._seen.add(key)
        self.problems.append({"code": code, "coin": coin, "msg": msg[:300], "source": source})

    def note(self, code: str, coin: Optional[str], msg: str, source: str) -> None:
        self.info.append({"code": code, "coin": coin, "msg": msg[:300], "source": source})


def _data(res: Optional[dict]) -> Any:
    return (res or {}).get("data") if (res or {}).get("ok") else None


def _source_status(inp: Dict[str, Any], names: Iterable[str]) -> Dict[str, str]:
    return {n: ("ok" if (inp.get(n) or {}).get("ok") else f"error: {(inp.get(n) or {}).get('error') or 'not fetched'}")
            for n in names}


def _unavailable(rep: _Report, inp: Dict[str, Any], names: Iterable[str]) -> None:
    for n in names:
        r = inp.get(n) or {}
        if not r.get("ok"):
            rep.problem("DATA_UNAVAILABLE", n, f"{n} unavailable: {r.get('error') or 'not fetched'}", "ops_cron")


# ---------------------------------------------------------------------------------------------
# Hyperliquid (public info API payloads)
# ---------------------------------------------------------------------------------------------
def hl_positions(state: Any) -> List[dict]:
    out = []
    for g in (state or {}).get("assetPositions") or [] if isinstance(state, dict) else []:
        p = (g or {}).get("position") or {}
        szi = _f(p.get("szi")) or 0.0
        if not szi or not p.get("coin"):
            continue
        lev = p.get("leverage") or {}
        out.append({"coin": str(p["coin"]), "side": "LONG" if szi > 0 else "SHORT", "szi": szi,
                    "entry_px": _f(p.get("entryPx")), "lev": _f(lev.get("value")), "lev_type": lev.get("type"),
                    "unrealized_pnl": _f(p.get("unrealizedPnl")),
                    "position_value": _f(p.get("positionValue"))})
    return out


def _fills(fills: Any) -> List[dict]:
    return sorted([x for x in fills or [] if isinstance(x, dict) and x.get("time") is not None],
                  key=lambda x: int(x["time"]))


def hl_orders_in_window(fills: Any, since: datetime) -> List[dict]:
    """Fills since `since`, grouped per order id: coin, dir, size, VWAP, fees, realised P&L."""
    by_oid: Dict[Any, dict] = {}
    since_ms = since.timestamp() * 1000
    for x in _fills(fills):
        if int(x["time"]) < since_ms:
            continue
        o = by_oid.setdefault(x.get("oid") or x.get("hash") or x["time"], {
            "coin": x.get("coin"), "dir": x.get("dir"), "time": int(x["time"]), "sz": 0.0, "notional": 0.0,
            "fee": 0.0, "closed_pnl": 0.0})
        sz, px = _f(x.get("sz")) or 0.0, _f(x.get("px")) or 0.0
        o["sz"] += sz
        o["notional"] += sz * px
        o["fee"] += _f(x.get("fee")) or 0.0
        o["closed_pnl"] += _f(x.get("closedPnl")) or 0.0
    out = []
    for o in by_oid.values():
        o["px"] = round(o["notional"] / o["sz"], 10) if o["sz"] else None
        o["at"] = datetime.fromtimestamp(o.pop("time") / 1000, timezone.utc).isoformat()
        o["notional"] = round(o["notional"], 4)
        out.append(o)
    return sorted(out, key=lambda o: o["at"])


def _is_open(o: dict) -> bool:
    return str(o.get("dir") or "").startswith("Open")


def _is_close(o: dict) -> bool:
    return str(o.get("dir") or "").startswith("Close") or "Liquidat" in str(o.get("dir") or "")


def hl_round_trips(fills: Any, since: datetime) -> List[dict]:
    """Long round trips (flat -> open -> flat, walked chronologically per coin) whose last close fill is after
    `since`: entry / exit VWAP, times, realised P&L (closedPnl - fees of the close fills). A position that is only
    partly closed is listed with `closed: false`. Shorts are ignored (the strategy is long-only)."""
    trips: Dict[str, dict] = {}
    done: List[dict] = []
    for x in _fills(fills):
        coin, d = x.get("coin"), x.get("dir")
        if not coin or d not in ("Open Long", "Close Long"):
            continue
        start, sz = _f(x.get("startPosition")) or 0.0, _f(x.get("sz")) or 0.0
        if d == "Open Long":
            if coin not in trips or abs(start) < 1e-12:
                trips[coin] = {"opens": [], "closes": []}
            trips[coin]["opens"].append(x)
            continue
        trips.setdefault(coin, {"opens": [], "closes": []})["closes"].append(x)
        if abs(start - sz) <= 1e-9 * max(1.0, abs(start)):
            done.append(_trip(coin, trips.pop(coin), closed=True))
    done += [_trip(coin, t, closed=False) for coin, t in trips.items() if t["closes"]]
    since_ms = since.timestamp() * 1000
    return sorted([t for t in done if t["exit_ms"] >= since_ms], key=lambda t: t["exit_ms"])


def _trip(coin: str, t: dict, closed: bool) -> dict:
    opens, closes = t["opens"], t["closes"]
    pnl = sum((_f(x.get("closedPnl")) or 0.0) - (_f(x.get("fee")) or 0.0) for x in closes)
    note = [] if opens else ["entry fill not found in the fills window"]
    if not closed:
        note.append("partial close; position still open")
    return {"coin": coin, "closed": closed, "entry_at": _iso(opens[0]["time"]) if opens else None,
            "entry_px": _vwap(opens), "entry_ms": int(opens[0]["time"]) if opens else None,
            "exit_at": _iso(closes[-1]["time"]), "exit_px": _vwap(closes), "exit_ms": int(closes[-1]["time"]),
            "realized_pnl_usd": round(pnl, 4), **({"note": "; ".join(note)} if note else {})}


def _vwap(fills: List[dict]) -> Optional[float]:
    sz = sum(_f(x.get("sz")) or 0.0 for x in fills)
    return round(sum((_f(x.get("sz")) or 0.0) * (_f(x.get("px")) or 0.0) for x in fills) / sz, 10) if sz else None


def _iso(ms: Any) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat()


def exit_efficiency(trip: dict, candles: Any) -> dict:
    """MFE = highest 1h high between entry and exit. efficiency = (exit - entry) / (MFE high - entry):
    1.0 = sold the top, 0 = gave back the whole move, < 0 = exited below entry."""
    entry, exit_px = trip.get("entry_px"), trip.get("exit_px")
    lo_ms, hi_ms = trip.get("entry_ms"), trip.get("exit_ms")
    highs = [_f(c.get("h")) for c in candles or [] if isinstance(c, dict)
             and lo_ms is not None and hi_ms is not None and lo_ms - 3_600_000 < int(c.get("t") or 0) <= hi_ms]
    highs = [h for h in highs if h]
    if not entry or not exit_px or not highs:
        return {"mfe_pct": None, "exit_pct": None, "efficiency": None, "note": "no candles / entry"}
    top = max(max(highs), exit_px)
    eff = round((exit_px - entry) / (top - entry), 3) if top > entry else None
    return {"mfe_pct": round((top / entry - 1) * 100, 3), "exit_pct": round((exit_px / entry - 1) * 100, 3),
            "efficiency": eff}


# ---------------------------------------------------------------------------------------------
# shared checks
# ---------------------------------------------------------------------------------------------
def hlparse_mids(inp: Dict[str, Any]) -> Dict[str, float]:
    from . import hlparse
    if not (inp.get("all_mids") or {}).get("ok"):
        return {}
    return hlparse.mids(_data(inp.get("all_mids")))


def check_hl_positions(rep: _Report, state: Any, orders: Any, mids: Optional[Dict[str, float]] = None) -> List[dict]:
    """Hard SL present uses hlparse.hard_sl_status (same helper as Harbor and the BO report)."""
    from . import hlparse
    pos = hl_positions(state)
    if orders is not None:
        for p in pos:
            mark = (mids or {}).get(p["coin"])
            if mark is None and p.get("position_value") and p.get("szi"):
                mark = abs(p["position_value"] / p["szi"])
            st = hlparse.hard_sl_status(orders, p["coin"], p["side"], p["szi"], mark)
            if not st["ok"]:
                rep.problem("NO_SL", p["coin"],
                            f"HL {p['side']} {p['coin']} has no Hard SL "
                            f"(trigger/stop, reduce-only or position TP/SL, opposite side, "
                            f"trigger on the losing side of the mark, size covering the position)",
                            "hl_public")
    return pos


def check_cockpit_jobs(rep: _Report, sched: Any, now: datetime) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not isinstance(sched, dict):
        return out
    if sched.get("enabled") is False:
        rep.problem("SCHEDULER_DISABLED", None, "cockpit scheduler is disabled (SCHEDULER_ENABLED=0)", "cockpit_scheduler")
        return out
    jobs = sched.get("jobs") or {}
    for name, max_min in COCKPIT_JOB_MAX_AGE_MIN.items():
        st = jobs.get(name) or {}
        t = _ts(st.get("last_run"))
        age = round((now - t).total_seconds() / 60, 1) if t else None
        out[name] = {"last_run": st.get("last_run"), "status": st.get("status"), "age_min": age, "max_age_min": max_min}
        if age is None or age > max_min:
            rep.problem("JOB_STALE", name, f"cockpit job {name} last ran {_hkt(t)} "
                        f"({'never' if age is None else f'{age:.0f} min ago'}; limit {max_min} min)", "cockpit_scheduler")
    return out


def _bx_shape_ok(bx: Any) -> bool:
    return isinstance(bx, dict) and bx.get("ok") is True and "breaker" in bx and "open" in bx


def check_bx_core(rep: _Report, bx: dict, now: datetime) -> dict:
    """Breaker, BX_LIVE vs live gate, egress (when live), position count / SL / daily entry cap, bx-exec problems."""
    rules = {**BX_DEFAULT_RULES, **{k: v for k, v in (bx.get("rules") or {}).items() if v is not None}}
    br = bx.get("breaker") or {}
    if br.get("tripped"):
        rep.problem("BX_BREAKER", None, f"Bitunix circuit breaker tripped at {br.get('at')}: live P&L "
                    f"{br.get('pnl_usd')} USD ({br.get('pct_nav')}% NAV); new BX entries stopped", "bx_status")
    realized, base = _f(bx.get("realized_pnl_usd")), _f(bx.get("baseline_nav"))
    if not br.get("tripped") and realized is not None and base and realized <= -rules["breaker_pct_nav"] / 100 * base:
        rep.problem("BX_BREAKER_NOT_TRIPPED", None, f"realised BX P&L {realized:.2f} USD <= -{rules['breaker_pct_nav']:g}% "
                    f"of baseline NAV {base:.2f} but the breaker is not tripped", "bx_status")
    live = bool(bx.get("bx_live"))
    eg = bx.get("egress") or {}
    if live and not eg.get("ok"):
        rep.problem("BX_EGRESS", None, f"BX_LIVE=1 but egress not verified non-US: {eg.get('reason')}", "bx_status")
    blockers = [b for b in bx.get("live_blockers") or [] if not str(b).startswith("BX_LIVE=0")]
    if live and bx.get("live_ready") is False:
        rep.problem("BX_LIVE_BLOCKED", None, "BX_LIVE=1 but live orders are blocked: " + "; ".join(map(str, blockers)),
                    "bx_status")
    opens = bx.get("open") or []
    if len(opens) > int(rules["max_open"]):
        rep.problem("BX_MAX_OPEN", None, f"{len(opens)} live BX positions open (pilot max {rules['max_open']})", "bx_status")
    for o in opens:
        if _f(o.get("hard_sl")) is None:
            rep.problem("BX_NO_SL", o.get("bx_symbol"), "live BX position without a Hard SL in the ledger", "bx_status")
    day0 = hkt_day_start(now)
    today = [o for o in opens if (_ts(o.get("entry_time")) or datetime.min.replace(tzinfo=timezone.utc)) >= day0]
    if len(today) > int(rules["max_new_per_day"]):
        rep.problem("BX_ENTRY_CAP", None, f"{len(today)} new BX entries today HKT (pilot max {rules['max_new_per_day']})",
                    "bx_status")
    for p in bx.get("problems") or []:
        rep.problem(str(p.get("code") or "BX_PROBLEM"), p.get("coin"), str(p.get("msg") or ""), "bx_status")
    return {"enabled": bx.get("bx_enabled"), "bx_live": live, "live_ready": bx.get("live_ready"),
            "live_blockers": bx.get("live_blockers") or [], "breaker_tripped": bool(br.get("tripped")),
            "realized_pnl_usd": realized, "baseline_nav": base, "n_open": len(opens),
            "open": [{k: o.get(k) for k in ("bx_symbol", "kind", "entry_time", "entry_px", "hard_sl")} for o in opens],
            "entries_today": len(today), "rules": rules,
            "egress": {k: eg.get(k) for k in ("ok", "countries", "region", "reason")}}


def check_bx_jobs(rep: _Report, bx: dict, now: datetime) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if bx.get("bx_enabled") is False:
        rep.note("BX_DISABLED", None, "BX_ENABLED=0 on bx-exec: BX job checks skipped", "bx_status")
        return out
    jobs = bx.get("jobs") or {}
    for name, max_min in BX_JOB_MAX_AGE_MIN.items():
        st = jobs.get(name)
        if not st:
            out[name] = {"last_run": None, "status": None, "age_min": None, "max_age_min": max_min}
            rep.note("BX_JOB_UNKNOWN", name, f"bx-exec job {name} has not run since the service started "
                     "(job status is in memory only)", "bx_status")
            continue
        t = _ts(st.get("at"))
        age = round((now - t).total_seconds() / 60, 1) if t else None
        out[name] = {"last_run": st.get("at"), "status": st.get("status"), "age_min": age, "max_age_min": max_min}
        if st.get("status") == "error":
            rep.problem("BX_JOB_ERROR", name, f"bx-exec job {name} failed at {_hkt(t)}: "
                        f"{st.get('message') or st.get('error_steps')}", "bx_status")
        if age is None or age > max_min:
            rep.problem("BX_JOB_STALE", name, f"bx-exec job {name} last ran {_hkt(t)} (limit {max_min} min)", "bx_status")
    return out


def _bx_closes_since(bx: dict, since: datetime) -> List[dict]:
    return [c for c in bx.get("closed_recent") or []
            if (_ts(c.get("exit_time")) or datetime.min.replace(tzinfo=timezone.utc)) >= since]


def _result(check: str, now: datetime, rep: _Report, summary_ok: str, body: Dict[str, Any]) -> Dict[str, Any]:
    status = "problem" if rep.problems else "ok"
    hk = now.astimezone(HKT)
    summary = summary_ok if status == "ok" else f"{len(rep.problems)} problem(s): " + "; ".join(
        f"{p['code']}{'/' + str(p['coin']) if p.get('coin') else ''}" for p in rep.problems[:8])
    return {"check": check, "run_at": now.isoformat(), "run_at_hkt": hk.strftime("%Y-%m-%d %H:%M"),
            "status": status, "summary": summary, "problems": rep.problems, "info": rep.info, **body}


# ---------------------------------------------------------------------------------------------
# 1. exit monitor (hourly)
# ---------------------------------------------------------------------------------------------
EXIT_SOURCES = ("exit_health", "scheduler", "hl_state", "hl_orders", "hl_fills", "all_mids", "bx_status")


def exit_monitor(inp: Dict[str, Any], now: datetime, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """inp: exit_health, scheduler, hl_state, hl_orders, hl_fills, bx_status (each {ok, data, error}).
    cfg: bx_enabled (bool), lookback_min (int), daily_summary_hour_hkt (int)."""
    rep = _Report()
    bx_on = bool(cfg.get("bx_enabled", True))
    names = [n for n in EXIT_SOURCES if bx_on or n != "bx_status"]
    _unavailable(rep, inp, names)

    eh = _data(inp.get("exit_health"))
    if isinstance(eh, dict):
        for p in eh.get("problems") or []:
            if p.get("code") not in SUPERSEDED_EXIT_HEALTH_CODES:
                rep.problem(str(p.get("code")), p.get("coin"), str(p.get("msg") or ""), "cockpit_exit_health")

    state, orders, fills = _data(inp.get("hl_state")), _data(inp.get("hl_orders")), _data(inp.get("hl_fills"))
    mids = hlparse_mids(inp)
    pos = check_hl_positions(rep, state, orders if isinstance(orders, list) else None, mids) if isinstance(state, dict) else []
    since = now - timedelta(minutes=int(cfg.get("lookback_min", 65)))
    hl_today = [o for o in hl_orders_in_window(fills, hkt_day_start(now)) if _is_open(o)] if fills is not None else []
    if len(hl_today) > HL_MAX_NEW_ENTRIES_PER_DAY:
        rep.problem("HL_ENTRY_CAP", None, f"{len(hl_today)} HL opening orders today HKT "
                    f"(max {HL_MAX_NEW_ENTRIES_PER_DAY})", "hl_public")
    hl_closes = [o for o in hl_orders_in_window(fills, since) if _is_close(o)] if fills is not None else []
    for c in hl_closes:
        rep.note("HL_CLOSE", c["coin"], f"{c['dir']} {c['sz']:g} @ {c['px']} (P&L {c['closed_pnl'] - c['fee']:.2f} USD)",
                 "hl_public")

    jobs = check_cockpit_jobs(rep, _data(inp.get("scheduler")), now)

    bx_body: Dict[str, Any] = {"checked": False}
    bx = _data(inp.get("bx_status")) if bx_on else None
    if bx_on and bx is not None and not _bx_shape_ok(bx):
        rep.problem("DATA_UNAVAILABLE", "bx_status", f"bx status has an unexpected shape: {str(bx)[:160]}", "ops_cron")
    elif bx_on and bx is not None:
        bx_body = {"checked": True, **check_bx_core(rep, bx, now), "jobs": check_bx_jobs(rep, bx, now),
                   "closes_since_last_run": _bx_closes_since(bx, since)}
        for c in bx_body["closes_since_last_run"]:
            rep.note("BX_CLOSE", c.get("bx_symbol"), f"{c.get('exit_reason')} P&L {c.get('pnl_usd')} USD", "bx_status")

    hk = now.astimezone(HKT)
    pend_body: Dict[str, Any] = {"checked": False}
    if "pending" in inp:
        names = list(names) + ["pending"]
        if not (inp.get("pending") or {}).get("ok"):
            rep.problem("DATA_UNAVAILABLE", "pending",
                        f"pending unavailable: {(inp.get('pending') or {}).get('error') or 'not fetched'}", "ops_cron")
        else:
            pdata = _data(inp.get("pending"))
            active = (pdata.get("active") or []) if isinstance(pdata, dict) else []
            pend_body = {"checked": True, "disabled": pdata.get("disabled") if isinstance(pdata, dict) else None,
                         "n_active": len(active) if isinstance(active, list) else None}
    ok_line = (f"OK · HL {len(pos)} 倉 · BX {bx_body.get('n_open', '-')} 倉 · BX_LIVE="
               f"{'-' if not bx_body.get('checked') else int(bool(bx_body.get('bx_live')))} · "
               f"closes {len(hl_closes) + len(bx_body.get('closes_since_last_run') or [])} · {hk.strftime('%H:%M HKT %d-%b')}")
    res = _result("exit_monitor", now, rep, ok_line, {
        "hl": {"n_positions": len(pos), "positions": pos, "entries_today": len(hl_today),
               "closes_since_last_run": hl_closes},
        "bx": bx_body, "pending": pend_body, "cockpit_jobs": jobs,
        "exit_health_summary": eh.get("summary") if isinstance(eh, dict) else None,
        "sources": _source_status(inp, names), "lookback_min": int(cfg.get("lookback_min", 65))})
    # Silent when nothing is wrong. The old 20:xx daily OK line is not sent.
    res["notify"] = "problem" if res["status"] == "problem" else None
    return _attach_nav(res, inp)


# ---------------------------------------------------------------------------------------------
# 2. daily live audit (09:22 HKT)
# ---------------------------------------------------------------------------------------------
AUDIT_SOURCES = ("bx_status", "bx_day", "run_report_today", "run_report_yesterday", "pending", "hl_fills")


def candle_requests(fills_res: Optional[dict], now: datetime, window_h: int = 24) -> List[dict]:
    """Which 1h candles the audit needs for exit efficiency (one request per closed HL round trip)."""
    out = []
    for t in hl_round_trips(_data(fills_res), now - timedelta(hours=window_h)):
        if t.get("entry_ms"):
            out.append({"coin": t["coin"], "start_ms": t["entry_ms"] - 3_600_000, "end_ms": t["exit_ms"]})
    return out


def _in_window(v: Any, since: datetime) -> bool:
    t = _ts(v)
    return bool(t and t >= since)


def _hl_fill_review(x: dict, slip_max_bp: float) -> Tuple[dict, List[str], Optional[float]]:
    px, mid, sl, lev = _f(x.get("px")), _f(x.get("mid")), _f(x.get("hard_sl")), _f(x.get("leverage"))
    kind = str(x.get("kind") or "")
    why: List[str] = []
    sl_dist = round((px - sl) / px * 100, 3) if px and sl else None
    if sl is None:
        why.append("no Hard SL recorded")
    elif sl_dist is not None and sl_dist < MIN_SL_DIST_PCT:
        why.append(f"Hard SL {sl_dist:.2f}% below fill (< {MIN_SL_DIST_PCT}%)")
    if lev is not None and not HL_LEV_MIN <= lev <= HL_LEV_MAX:
        why.append(f"leverage {lev:g}x outside {HL_LEV_MIN:g}-{HL_LEV_MAX:g}x")
    zone = x.get("zone") if isinstance(x.get("zone"), list) and len(x.get("zone")) == 2 else None
    zone_pos = None
    if zone and px:
        lo, fi = _f(zone[0]), _f(zone[1])
        if lo and px <= lo:
            zone_pos = "below_lower"
            why.append(f"fill {px:g} at/below the pending zone Lower {lo:g}")
        elif fi and px > fi:
            zone_pos = f"above_filter +{(px / fi - 1) * 100:.2f}%"
        else:
            zone_pos = "in_zone"
    upper = _f(x.get("upper_ref"))
    if kind == "Base" and upper and px and px <= upper:
        why.append(f"Base fill {px:g} not above the 1D Upper {upper:g}")
    slip = round((px - mid) / mid * 10_000, 1) if px and mid else None
    row = {"venue": "HL", "symbol": x.get("symbol"), "kind": kind, "at": x.get("run_ts"), "fill_px": px,
           "mid_at_signal": mid, "slippage_bp": slip, "hard_sl": sl, "sl_dist_pct": sl_dist, "leverage": lev,
           "zone": zone, "zone_position": zone_pos, "upper_ref": upper, "compliant": not why, "violations": why}
    return row, why, (slip if slip is not None and slip > slip_max_bp else None)


def _bx_fill_review(o: dict) -> Tuple[dict, List[str]]:
    px, sl = _f(o.get("entry_px")), _f(o.get("hard_sl"))
    why: List[str] = []
    if not o.get("sl_confirmed"):
        why.append("Hard SL order not confirmed on the exchange")
    sl_dist = round((px - sl) / px * 100, 3) if px and sl else None
    if sl_dist is not None and sl_dist < MIN_SL_DIST_PCT:
        why.append(f"Hard SL {sl_dist:.2f}% below fill (< {MIN_SL_DIST_PCT}%)")
    return ({"venue": "BX", "symbol": o.get("symbol"), "kind": None, "at": o.get("at"), "fill_px": px,
             "mid_at_signal": None, "slippage_bp": None, "hard_sl": sl, "sl_dist_pct": sl_dist,
             "sl_confirmed": bool(o.get("sl_confirmed")), "compliant": not why, "violations": why}, why)


def daily_audit(inp: Dict[str, Any], now: datetime, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """inp: bx_status, bx_day, run_report_today, run_report_yesterday, pending, hl_fills, candles {coin: res}.
    cfg: bx_enabled, expect_bx_live (None|bool), slippage_max_bp, bx_country, bx_region_prefix, window_h."""
    rep = _Report()
    bx_on = bool(cfg.get("bx_enabled", True))
    names = [n for n in AUDIT_SOURCES if bx_on or not n.startswith("bx_")]
    # bx_day 404 = no 08:56 run report yet today (e.g. bx-exec restarted): info, not a data outage
    bx_day_res = inp.get("bx_day") or {}
    if bx_on and not bx_day_res.get("ok") and bx_day_res.get("http") == 404:
        names.remove("bx_day")
        rep.note("BX_DAY_MISSING", None, "no BX day report for today (08:56 run not recorded)", "bx_day")
    _unavailable(rep, inp, names)
    window_h = int(cfg.get("window_h", 24))
    since = now - timedelta(hours=window_h)
    slip_max = float(cfg.get("slippage_max_bp", 60))

    # --- BX: live flag, Singapore egress, breaker, orders, realised P&L
    bx_body: Dict[str, Any] = {"checked": False}
    bx = _data(inp.get("bx_status")) if bx_on else None
    if bx_on and bx is not None and not _bx_shape_ok(bx):
        rep.problem("DATA_UNAVAILABLE", "bx_status", f"bx status has an unexpected shape: {str(bx)[:160]}", "ops_cron")
    elif bx_on and bx is not None:
        bx_body = {"checked": True, **check_bx_core(rep, bx, now)}
        eg = bx.get("egress") or {}
        countries = set((eg.get("countries") or {}).values()) if isinstance(eg.get("countries"), dict) else set()
        want_c, want_r = cfg.get("bx_country", "SG"), cfg.get("bx_region_prefix", "asia-southeast1")
        sg_ok = bool(eg.get("ok")) and countries == {want_c} and str(eg.get("region") or "").startswith(want_r)
        bx_body["singapore_egress_ok"] = sg_ok
        if not sg_ok:
            rep.problem("BX_EGRESS", None, f"egress not verified as {want_c}/{want_r}*: ok={eg.get('ok')} "
                        f"countries={sorted(c for c in countries if c)} region={eg.get('region')} {eg.get('reason') or ''}",
                        "bx_status")
        if cfg.get("expect_bx_live") is True and not bx_body["bx_live"]:
            rep.problem("BX_LIVE_OFF", None, "BX_LIVE=0 but OPS_EXPECT_BX_LIVE=1", "bx_status")
        closes = _bx_closes_since(bx, since)
        bx_body["closes_24h"] = closes
        bx_body["realized_pnl_24h_usd"] = round(sum(_f(c.get("pnl_usd")) or 0.0 for c in closes), 4)

    day = _data(inp.get("bx_day")) or {}
    bx_orders = [dict(o, at=day.get("at")) for o in day.get("orders") or []] if _in_window(day.get("at"), since) else []
    seen = {o.get("symbol") for o in bx_orders}
    for o in (bx or {}).get("open") or []:   # Chase pending fills happen in the 4h job, not in the day report
        if _in_window(o.get("entry_time"), since) and o.get("bx_symbol") not in seen:
            bx_orders.append({"symbol": o.get("bx_symbol"), "status": "filled", "entry_px": o.get("entry_px"),
                              "hard_sl": o.get("hard_sl"), "sl_confirmed": None, "at": o.get("entry_time"),
                              "source": "open_position"})

    # Open HL positions: same Hard SL helper as the exit monitor and Harbor.
    if "hl_state" in inp and isinstance(_data(inp.get("hl_state")), dict):
        check_hl_positions(rep, _data(inp.get("hl_state")),
                           _data(inp.get("hl_orders")) if isinstance(_data(inp.get("hl_orders")), list) else None,
                           hlparse_mids(inp))

    # --- HL: orders, realised P&L, round trips
    fills = _data(inp.get("hl_fills"))
    hl_orders = hl_orders_in_window(fills, since) if fills is not None else []
    hl_pnl = round(sum(o["closed_pnl"] - o["fee"] for o in hl_orders), 4)
    trips = hl_round_trips(fills, since) if fills is not None else []
    exits = []
    for t in trips:
        cres = (inp.get("candles") or {}).get(t["coin"]) or {}
        eff = exit_efficiency(t, _data(cres))
        exits.append({"venue": "HL", "coin": t["coin"], "closed": t["closed"],
                      "entry_at": t.get("entry_at"), "entry_px": t.get("entry_px"),
                      "exit_at": t.get("exit_at"), "exit_px": t.get("exit_px"),
                      "realized_pnl_usd": t.get("realized_pnl_usd"), **eff, **({"note": t["note"]} if t.get("note") else {})})

    # --- trade review: rule compliance + slippage per fill
    runs = [_data(inp.get("run_report_yesterday")) or {}, _data(inp.get("run_report_today")) or {}]
    executed = [x for r in runs for x in r.get("executed") or [] if not x.get("dry_run") and _in_window(x.get("run_ts"), since)]
    failed = [x for r in runs for x in r.get("failed") or [] if _in_window(x.get("run_ts"), since)]
    fills_review = []
    for x in executed:
        row, why, slip = _hl_fill_review(x, slip_max)
        fills_review.append(row)
        if why:
            rep.problem("RULE_VIOLATION", x.get("symbol"), "HL fill: " + "; ".join(why), "run_report")
        if slip is not None:
            rep.problem("SLIPPAGE_HIGH", x.get("symbol"), f"HL fill {slip:.0f} bp above the signal mid "
                        f"(limit {slip_max:g} bp)", "run_report")
    for o in bx_orders:
        if o.get("status") != "filled":
            continue
        row, why = _bx_fill_review(o)
        if o.get("source") == "open_position":
            row["violations"] = [w for w in why if not w.startswith("Hard SL order not confirmed")]
            row["compliant"] = not row["violations"]
            why = row["violations"]
        fills_review.append(row)
        if why:
            rep.problem("RULE_VIOLATION", o.get("symbol"), "BX fill: " + "; ".join(why), "bx_day")
    for x in failed:
        rep.note("HL_ORDER_FAILED", x.get("symbol"), str(x.get("reason") or ""), "run_report")

    # --- missed entries: pending expired / cancelled, or rejected by the 1.5% min SL distance
    missed: Dict[Tuple[str, str], dict] = {}
    pend = _data(inp.get("pending")) or {}
    for e in pend.get("all") or []:
        if e.get("status") in ("expired", "cancelled") and _in_window(e.get("closed_at"), since):
            missed[(str(e.get("symbol")), "pending_" + e["status"])] = {
                "venue": "HL", "symbol": e.get("symbol"), "kind": e.get("kind"), "why": "pending_" + e["status"],
                "reason": e.get("close_reason"), "at": e.get("closed_at")}
    for r in runs:
        for x in r.get("skipped") or []:
            if _in_window(x.get("run_ts"), since) and SL_DIST_MISS_RE.search(str(x.get("reason") or "")):
                missed[(str(x.get("symbol")), "min_sl_distance")] = {
                    "venue": "HL", "symbol": x.get("symbol"), "kind": x.get("kind") or x.get("run_job"),
                    "why": "min_sl_distance", "reason": x.get("reason"), "at": x.get("run_ts")}
    if _in_window(day.get("at"), since):
        for x in day.get("skipped") or []:
            if SL_DIST_MISS_RE.search(str(x.get("reason") or "")):
                missed[(str(x.get("symbol")), "bx_min_sl_distance")] = {
                    "venue": "BX", "symbol": x.get("symbol"), "kind": None, "why": "min_sl_distance",
                    "reason": x.get("reason"), "at": day.get("at")}

    hl_opens = [o for o in hl_orders if _is_open(o)]
    hl_closes = [o for o in hl_orders if _is_close(o)]
    bx_filled = [o for o in bx_orders if o.get("status") == "filled"]
    pnl_bx = bx_body.get("realized_pnl_24h_usd")
    ok_line = (f"Audit OK · HL orders {len(hl_opens)} open / {len(hl_closes)} close · BX fills {len(bx_filled)} · "
               f"P&L 24h HL {hl_pnl:+.2f} BX {('-' if pnl_bx is None else f'{pnl_bx:+.2f}')} USD · "
               f"BX_LIVE={'-' if not bx_body.get('checked') else int(bool(bx_body.get('bx_live')))} · "
               f"missed {len(missed)}")
    res = _result("daily_audit", now, rep, ok_line, {
        "window_h": window_h,
        "hl": {"orders_24h": hl_orders, "n_open_orders": len(hl_opens), "n_close_orders": len(hl_closes),
               "realized_pnl_24h_usd": hl_pnl, "failed_24h": failed},
        "bx": {**bx_body, "orders_24h": bx_orders},
        "trade_review": {"fills": fills_review, "missed_entries": list(missed.values()), "exits": exits},
        "sources": _source_status(inp, names)})
    # The 09:22 audit always produces one summary. Problems are inside that message.
    res["notify"] = "report"
    return _attach_nav(res, inp)


# ---------------------------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------------------------
def _attach_nav(res: Dict[str, Any], inp: Dict[str, Any]) -> Dict[str, Any]:
    """NAV from hlparse.portfolio_nav when this run fetched spot USDC. Missing key: leave the report alone."""
    if "hl_spot" not in inp:
        return res
    from . import hlparse, report
    info = hlparse.nav_from_envelopes(inp.get("hl_state"), inp.get("hl_spot"))
    if info.get("warning"):
        report.warn_nav_unverified(info["warning"])
    res["nav"] = info["nav"]
    res["nav_label"] = info["label"]
    return res


def to_markdown(r: Dict[str, Any]) -> str:
    title = "Exit monitor" if r["check"] == "exit_monitor" else "Daily live audit"
    lines = [f"## {title} — {r['run_at_hkt']} HKT — **{r['status'].upper()}**", "", r["summary"], ""]
    if r.get("nav_label"):
        lines += [r["nav_label"], ""]
    if r["problems"]:
        lines += ["### Problems", ""] + [f"- `{p['code']}`{' ' + str(p['coin']) if p.get('coin') else ''}: {p['msg']}"
                                          for p in r["problems"]] + [""]
    if r["check"] == "exit_monitor":
        hl, bx = r["hl"], r["bx"]
        lines += [f"- HL positions: {hl['n_positions']} ({', '.join(p['coin'] for p in hl['positions']) or 'none'}); "
                  f"opening orders today: {hl['entries_today']}; closes since last run: {len(hl['closes_since_last_run'])}"]
        if bx.get("checked"):
            lines += [f"- BX: BX_LIVE={int(bool(bx['bx_live']))}, live_ready={bx['live_ready']}, breaker "
                      f"{'TRIPPED' if bx['breaker_tripped'] else 'ok'}, open {bx['n_open']}, entries today "
                      f"{bx['entries_today']}, realised {bx['realized_pnl_usd']} USD"]
        lines += ["- Jobs: " + ", ".join(f"{k} {v['age_min']}m" for k, v in r["cockpit_jobs"].items())]
    else:
        hl, bx, tr = r["hl"], r["bx"], r["trade_review"]
        lines += [f"- HL: {hl['n_open_orders']} opening / {hl['n_close_orders']} closing orders in {r['window_h']}h, "
                  f"realised P&L {hl['realized_pnl_24h_usd']:+.2f} USD"]
        if bx.get("checked"):
            lines += [f"- BX: BX_LIVE={int(bool(bx['bx_live']))}, Singapore egress "
                      f"{'OK' if bx.get('singapore_egress_ok') else 'NOT OK'}, breaker "
                      f"{'TRIPPED' if bx['breaker_tripped'] else 'ok'}, fills {len([o for o in bx['orders_24h'] if o.get('status') == 'filled'])}, "
                      f"realised 24h {bx['realized_pnl_24h_usd']:+.2f} USD (cumulative {bx['realized_pnl_usd']})"]
        if tr["fills"]:
            lines += ["", "| venue | symbol | kind | fill | mid@signal | slip bp | SL dist % | zone | compliant |",
                      "|---|---|---|---|---|---|---|---|---|"]
            lines += [f"| {f['venue']} | {f['symbol']} | {f.get('kind') or ''} | {f['fill_px']} | {f.get('mid_at_signal') or ''} | "
                      f"{'' if f.get('slippage_bp') is None else f['slippage_bp']} | {f.get('sl_dist_pct')} | "
                      f"{f.get('zone_position') or ''} | {'yes' if f['compliant'] else 'NO: ' + '; '.join(f['violations'])} |"
                      for f in tr["fills"]]
        if tr["missed_entries"]:
            lines += ["", "Missed entries:"] + [f"- {m['venue']} {m['symbol']} ({m['why']}): {m.get('reason')}"
                                                 for m in tr["missed_entries"]]
        if tr["exits"]:
            lines += ["", "Exit efficiency (exit vs max favourable excursion):"] + [
                f"- {e['venue']} {e['coin']}: entry {e.get('entry_px')} exit {e.get('exit_px')} "
                f"({e.get('exit_pct')}%), MFE {e.get('mfe_pct')}%, efficiency {e.get('efficiency')}, "
                f"P&L {e.get('realized_pnl_usd')} USD" for e in tr["exits"]]
    if r["info"]:
        lines += ["", "Info:"] + [f"- `{i['code']}`{' ' + str(i['coin']) if i.get('coin') else ''}: {i['msg']}"
                                  for i in r["info"][:20]]
    return "\n".join(lines) + "\n"
