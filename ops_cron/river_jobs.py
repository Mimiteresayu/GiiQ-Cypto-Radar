"""River's three read-only jobs. Insert-only into giiq-brain. Alert only on failure or a Hard SL hit.

1. c48-scoreboard — HL testnet public info, every 3h until 2026-10-07 21:00 HKT
2. desk-veto — daily 09:05 HKT
3. trade-journal — daily 09:15 HKT, one row per fill or decision, with a decider column
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import decider, gc_levels, hlparse, report, rules, stops

C48_UNTIL = datetime(2026, 10, 7, 21, 0, tzinfo=rules.HKT)
# C48-3 locks: allocation <= 2% NAV, position <= 8% NAV, leverage <= 3, drawdown <= 4%.
# The 6% ROE stop at 2x is about a 3% adverse price move.
C48_PRICE_SL_PCT = 3.0


def _src_hl(src, body):
    return src.hl(body)


def fetch_scoreboard(src, now: datetime, env: Dict[str, str]) -> Dict[str, Any]:
    end_ms = int(now.timestamp() * 1000)
    start_ms = end_ms - 14 * 86_400_000
    return {
        "state": src.hl({"type": "clearinghouseState", "user": src.hl_address}),
        "spot": src.hl({"type": "spotClearinghouseState", "user": src.hl_address}),
        "orders": src.hl({"type": "frontendOpenOrders", "user": src.hl_address}),
        "fills": src.hl({"type": "userFillsByTime", "user": src.hl_address, "startTime": start_ms, "endTime": end_ms}),
        "nav_start": env.get("C48_NAV_START") or "",
    }


def build_scoreboard(inputs: Dict[str, Any], now: datetime, env: Dict[str, str]) -> Dict[str, Any]:
    if now.astimezone(rules.HKT) >= C48_UNTIL:
        md = "C48-3 window closed after 2026-10-07 21:00 HKT. No score row.\n"
        return report.shell("river_c48_ft_score", now, "ok", "C48-3 window closed", [], None, md, closed=True)
    problems: List[dict] = []
    state_res, fill_res = inputs.get("state") or {}, inputs.get("fills") or {}
    state_ok = bool(state_res.get("ok")) and isinstance(state_res.get("data"), dict)
    if not state_ok:
        problems.append(report.problem("DATA_UNAVAILABLE", f"testnet account unreadable: {state_res.get('error') or 'unreadable'}"))
    if not fill_res.get("ok"):
        problems.append(report.problem("DATA_UNAVAILABLE", f"testnet fills unreadable: {fill_res.get('error') or 'missing'}"))
    spot_res = inputs.get("spot") if "spot" in inputs else {"ok": False}
    if "spot" in inputs and not spot_res.get("ok"):
        report.add_problem(problems, "DATA_UNAVAILABLE",
                           f"hl_spot unavailable: {spot_res.get('error') or 'not fetched'}", "hl_spot")
    nav_info = hlparse.nav_from_envelopes(state_res, spot_res)
    if nav_info.get("warning"):
        report.warn_nav_unverified(nav_info["warning"])
    nav = nav_info["nav"]
    nav_start = stops.num((inputs.get("nav_start") or env.get("C48_NAV_START") or "").strip() or None)
    if nav_start is None:
        problems.append(report.problem("DATA_UNAVAILABLE", "C48_NAV_START is unset; drawdown is 未知"))
    pos = hlparse.positions(state_res.get("data") if state_ok else None)
    fills = hlparse.fills(fill_res.get("data")) if fill_res.get("ok") else []
    net = hlparse.sum_pnl(fills) if fill_res.get("ok") else None
    margin = hlparse.margin_used(state_res.get("data") if state_res.get("ok") else None)
    notional = sum(abs(p.get("position_value") or 0.0) for p in pos)
    lev = max((p.get("lev") or 0.0) for p in pos) if pos else None
    notional_pct = (notional / nav * 100.0) if nav else None
    margin_pct = (margin / nav * 100.0) if nav and margin is not None else None
    margin_txt = "未核實" if nav is None else report.pct(margin_pct)
    dd = None
    if nav is not None and nav_start:
        dd = max(0.0, (nav_start - nav) / nav_start * 100.0)
        # Also walk the realised path so a recovered dip is still a max drawdown.
        peak = nav_start
        equity = nav_start
        ordered = sorted(fills, key=lambda x: int(x["time"]))
        for x in ordered:
            equity += (hlparse.num(x.get("closedPnl")) or 0) - (hlparse.num(x.get("fee")) or 0)
            peak = max(peak, equity)
            if peak:
                dd = max(dd, (peak - equity) / peak * 100.0)
    if dd is not None and dd > 4.0:
        problems.append(report.problem("C48_DRAWDOWN", f"max drawdown {dd:.2f}% > 4%"))
    if notional_pct is not None and notional_pct > 8.0:
        problems.append(report.problem("C48_NOTIONAL", f"position notional {notional_pct:.2f}% of NAV > 8%"))
    if margin_pct is not None and margin_pct > 2.0:
        problems.append(report.problem("C48_ALLOC", f"margin {margin_pct:.2f}% of NAV > 2%"))
    if lev is not None and lev > 3.0:
        problems.append(report.problem("C48_LEVERAGE", f"leverage {lev:g}x > 3x"))
    for p in pos:
        if p["coin"] != "BTC":
            problems.append(report.problem("C48_SYMBOL", f"non-BTC position {p['coin']}", p["coin"]))
    hard_hit = False
    opens: Dict[str, List[float]] = {}
    for x in sorted(fills, key=lambda z: int(z["time"])):
        coin = str(x.get("coin") or "")
        px = hlparse.num(x.get("px"))
        if str(x.get("dir") or "").startswith("Open") and px:
            opens.setdefault(coin, []).append(px)
        if "Liquidat" in str(x.get("dir") or ""):
            hard_hit = True
        if str(x.get("dir") or "").startswith("Close") and px and opens.get(coin):
            entry = sum(opens[coin]) / len(opens[coin])
            if entry and (entry - px) / entry * 100.0 >= C48_PRICE_SL_PCT:
                hard_hit = True
    if hard_hit:
        problems.append(report.problem("HARD_SL_HIT", "a testnet close went through the ~3% price stop"))
    orders_res = inputs.get("orders")
    orders = None
    if isinstance(orders_res, dict):
        if orders_res.get("ok") and isinstance(orders_res.get("data"), list):
            orders = orders_res.get("data")
        else:
            problems.append(report.problem("DATA_UNAVAILABLE", "testnet open orders unreadable"))
    for p in pos:
        mark = abs(p["position_value"] / p["szi"]) if p.get("position_value") and p.get("szi") else None
        if isinstance(orders, list) and not hlparse.hard_sl_status(
                orders, p["coin"], p["side"], p["szi"], mark)["ok"]:
            problems.append(report.problem("NO_SL", f"no Hard SL on {p['coin']}", p["coin"]))
    status = "problem" if problems else "ok"
    summary = (f"C48 {nav_info['label']} net {report.usd(net)} dd {report.pct(dd)} "
               f"lev {lev if lev is not None else '未知'} notional {report.pct(notional_pct)} margin% {margin_txt}")
    md = "\n".join([
        f"## C48-3 scoreboard {now.astimezone(rules.HKT).strftime('%Y-%m-%d %H:%M')} HKT",
        "",
        summary,
        "",
        f"fills {len(fills)} · hard_sl_hit {hard_hit}",
        *([f"- {p['code']}: {p['msg']}" for p in problems] or ["- checks inside 2% / 8% / 3x / 4% dd"]),
        "",
    ])
    row = (now.isoformat(), env.get("TESTNET_WALLET_ADDRESS") or None, nav, nav_start, net, dd, lev,
           notional_pct, margin_pct, hard_hit, status, {"summary": summary, "problems": problems})
    brain = [{
        "table": "raw.river_c48_ft_score",
        "columns": ["run_at", "wallet", "nav", "nav_start", "net_pnl", "max_drawdown_pct", "leverage",
                    "notional_pct", "margin_pct", "hard_sl_hit", "status", "report"],
        "values": [row],
    }]
    notify = "problem" if problems else None
    return report.shell("river_c48_ft_score", now, status, summary, problems, notify, md, brain_rows=brain,
                        hard_sl_hit=hard_hit)


def _radar_rows(radar: Any, tf: str) -> List[dict]:
    if not isinstance(radar, dict):
        return []
    block = radar.get(f"gc_radar_{tf}") or {}
    return [r for r in (block.get("rows") or []) if isinstance(r, dict)]


def signal_day(now: datetime) -> str:
    """The desk batch from two HKT dates ago. The 09:05 report scores that batch, not today's crosses."""
    return (now.astimezone(rules.HKT) - timedelta(days=2)).strftime("%Y-%m-%d")


def signal_anchor(day: str) -> datetime:
    """08:55 HKT on the signal day, when the desk POST is in."""
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, 8, 55, tzinfo=rules.HKT)


def _close_asof(bars: List[dict], ms: int) -> Optional[float]:
    closed = [b for b in bars if b.get("t") is not None and int(b["t"]) + 3_600_000 <= ms and b.get("c")]
    if not closed:
        return None
    return float(closed[-1]["c"])


def _ret_forward(bars: List[dict], start_ms: int, end_ms: int) -> Optional[float]:
    """Price change from the signal time forward to `end_ms` (the 48h after the signal, capped at now)."""
    if not bars or end_ms <= start_ms:
        return None
    base = _close_asof(bars, start_ms)
    last = _close_asof(bars, end_ms)
    if not base or not last:
        return None
    return (last - base) / base


def _sl_aware(bars_1h: List[dict], bars_4h: List[dict], start_ms: int, end_ms: int,
              raw_ret: Optional[float]) -> tuple:
    """Return (ret_sl_aware, hard_sl_hit) over the 48h AFTER the signal.

    Hit when a 1h low in that forward window trades through the 4H Filter as of the signal.
    """
    if raw_ret is None:
        return None, None
    ch = gc_levels.channel_at(
        [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]} for b in bars_4h], "4h", start_ms)
    hard = ch.get("filter")
    if hard is None:
        return raw_ret, None
    base = _close_asof(bars_1h, start_ms)
    if not base:
        return raw_ret, None
    later = [b for b in bars_1h if int(b["t"]) >= start_ms and int(b["t"]) + 3_600_000 <= end_ms]
    hit = any(float(b["l"]) <= hard for b in later)
    if not hit:
        return raw_ret, False
    return (hard - base) / base, True


def _bars_from_hl(raw) -> List[dict]:
    return gc_levels.parse_candles(raw)


def _bars_from_bx(raw) -> List[dict]:
    data = raw
    if isinstance(raw, dict):
        data = raw.get("data") or raw.get("list") or []
    out = []
    for k in data or []:
        if not isinstance(k, dict):
            continue
        try:
            out.append({"t": int(k.get("time") if k.get("time") is not None else k["t"]),
                        "o": float(k["open"]), "h": float(k["high"]), "l": float(k["low"]), "c": float(k["close"])})
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda b: b["t"])
    return out


def _batch_symbols(dec: Any) -> List[str]:
    symbols = []
    if not isinstance(dec, dict):
        return symbols
    for rec in dec.get("records") or []:
        if isinstance(rec, dict) and rec.get("symbol"):
            symbols.append(str(rec["symbol"]))
    return list(dict.fromkeys(symbols))[:25]


def fetch_veto(src, now: datetime, env: Dict[str, str]) -> Dict[str, Any]:
    day = now.astimezone(rules.HKT).strftime("%Y-%m-%d")
    sig_day = signal_day(now)
    anchor = signal_anchor(sig_day)
    end_ms = int(now.timestamp() * 1000)
    # Candles cover the signal and the 48h after it, plus the 48h before so a wrong window is visible in tests.
    start_ms = int((anchor - timedelta(hours=60)).timestamp() * 1000)
    decisions = src.cockpit(f"/api/exec/run-report?date={sig_day}")
    dec = ((decisions.get("data") or {}).get("decisions") if decisions.get("ok") else None) or {}
    symbols = _batch_symbols(dec)
    if "BTC" not in symbols:
        symbols.append("BTC")
    candles = {}
    bx = {}
    for coin in symbols:
        candles[coin] = {
            "1h": src.hl({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h",
                                                             "startTime": start_ms, "endTime": end_ms}}),
            "4h": src.hl({"type": "candleSnapshot", "req": {"coin": coin, "interval": "4h",
                                                             "startTime": end_ms - 120 * 14_400_000, "endTime": end_ms}}),
        }
        sym = coin if coin.endswith("USDT") else coin + "USDT"
        bx[coin] = src.bx_klines(sym, "1h", 80)
    return {"day": day, "signal_day": sig_day, "decisions": decisions, "symbols": symbols,
            "candles": candles, "bx": bx}


def build_veto(inputs: Dict[str, Any], now: datetime, env: Dict[str, str]) -> Dict[str, Any]:
    day = inputs.get("day") or now.astimezone(rules.HKT).strftime("%Y-%m-%d")
    sig_day = inputs.get("signal_day") or signal_day(now)
    anchor = signal_anchor(sig_day)
    problems: List[dict] = []
    dec_res = inputs.get("decisions") or {}
    dec = ((dec_res.get("data") or {}).get("decisions") if dec_res.get("ok") else None)
    if not isinstance(dec, dict):
        problems.append(report.problem("DATA_UNAVAILABLE", "D-2 signal batch unreadable"))
        dec = {"ok": False, "records": [], "history": []}
    now_ms = int(now.timestamp() * 1000)
    anchor_ms = int(anchor.timestamp() * 1000)
    rows = []
    btc_bars = _bars_from_hl(((inputs.get("candles") or {}).get("BTC") or {}).get("1h", {}).get("data"))
    if not btc_bars:
        btc_bars = _bars_from_bx((inputs.get("bx") or {}).get("BTC", {}).get("data"))
    btc_end = min(now_ms, anchor_ms + 48 * 3_600_000)
    btc_ret = _ret_forward(btc_bars, anchor_ms, btc_end)
    symbols = [s for s in (inputs.get("symbols") or _batch_symbols(dec)) if s != "BTC"]
    for coin in symbols:
        rec = None
        for r in dec.get("records") or []:
            if isinstance(r, dict) and str(r.get("symbol")) == coin:
                rec = r
        kind = str((rec or {}).get("type") or "").upper()
        if kind == "BASE":
            strategy = "Base"
        elif kind in {"CHASE", "CONTINUATION"}:
            strategy = "Chase"
        else:
            strategy = "unknown"
        decision = (rec or {}).get("decision") or "none"
        who = decider.decider_of(rec) if rec else decider.UNKNOWN
        sig_ts = decider._ts((rec or {}).get("timestamp")) if rec else None
        if sig_ts is not None and sig_ts.astimezone(rules.HKT).strftime("%Y-%m-%d") != sig_day:
            sig_ts = None
        start_at = sig_ts or anchor
        start_ms = int(start_at.timestamp() * 1000)
        end_ms = min(now_ms, start_ms + 48 * 3_600_000)
        pack = (inputs.get("candles") or {}).get(coin) or {}
        bars_1h = _bars_from_hl((pack.get("1h") or {}).get("data") if (pack.get("1h") or {}).get("ok") else None)
        bars_4h = _bars_from_hl((pack.get("4h") or {}).get("data") if (pack.get("4h") or {}).get("ok") else None)
        source = "HL"
        if not bars_1h:
            bx = (inputs.get("bx") or {}).get(coin) or {}
            if bx.get("ok"):
                bars_1h = _bars_from_bx(bx.get("data"))
                source = "BX"
        raw = _ret_forward(bars_1h, start_ms, end_ms)
        aware, hit = _sl_aware(bars_1h, bars_4h, start_ms, end_ms, raw)
        excess = (aware - btc_ret) if aware is not None and btc_ret is not None else None
        if hit is True:
            outcome = "hard_sl"
        elif aware is None:
            outcome = "未知"
        elif aware > 0:
            outcome = "up"
        elif aware < 0:
            outcome = "down"
        else:
            outcome = "flat"
        if hit:
            problems.append(report.problem("HARD_SL_HIT", f"{coin} 48h low went through 4H Filter", coin))
        window_complete = (now_ms - start_ms) >= 48 * 3_600_000
        rows.append({
            "coin": coin, "strategy": strategy, "desk_decision": decision, "decider": who,
            "ret_48h": raw, "ret_48h_sl_aware": aware, "excess_vs_btc": excess,
            "hard_sl_hit": hit, "outcome": outcome, "candle_source": source,
            "window_complete": window_complete,
        })
    status = "problem" if problems else "ok"
    complete = [r for r in rows if r.get("window_complete")]
    ups = [r for r in complete if r.get("outcome") == "up"]
    cont = (len(ups) / len(complete)) if complete else None
    lines = ["## Desk veto + CONT", "",
             f"continuation rate (complete 48h windows only): {_n(cont)}",
             "",
             "coin | strategy | desk_decision | decider | ret_48h | ret_48h_sl_aware | excess_vs_btc | hard_sl_hit | outcome | window",
             ""]
    for r in rows:
        flag = "48h" if r.get("window_complete") else "窗口未夠 48h"
        lines.append(
            f"{r['coin']} | {r['strategy']} | {r['desk_decision']} | {r['decider']} | "
            f"{_n(r['ret_48h'])} | {_n(r['ret_48h_sl_aware'])} | {_n(r['excess_vs_btc'])} | "
            f"{r['hard_sl_hit']} | {r['outcome']} | {flag}")
    if not rows:
        lines.append("(no signals)")
    md = "\n".join(lines) + "\n"
    brain_values = []
    for r in rows:
        brain_values.append((now.isoformat(), sig_day, r["coin"], r["strategy"], r["desk_decision"], r["decider"],
                             r["ret_48h"], r["ret_48h_sl_aware"], r["excess_vs_btc"], r["hard_sl_hit"],
                             r["outcome"], bool(r["window_complete"]), r))
    brain = []
    if brain_values:
        brain.append({
            "table": "raw.river_desk_veto",
            "columns": ["run_at", "as_of", "coin", "strategy", "desk_decision", "decider", "ret_48h",
                        "ret_48h_sl_aware", "excess_vs_btc", "hard_sl_hit", "outcome", "window_complete", "report"],
            "values": brain_values,
        })
    notify = "problem" if problems else None
    summary = f"desk veto {day} on {sig_day} signals: {len(rows)}" + (f", {len(problems)} problem(s)" if problems else "")
    return report.shell("river_desk_veto", now, status, summary, problems, notify, md, brain_rows=brain, rows=rows)


def _n(v) -> str:
    if v is None:
        return "未知"
    try:
        return f"{float(v):.4f}"
    except (TypeError, ValueError):
        return "未知"


def signed_fill_size(fill: dict) -> Optional[float]:
    """Long exposure positive, short exposure negative. Open Short was previously stored positive."""
    sz = hlparse.num(fill.get("sz"))
    if sz is None:
        return None
    d = str(fill.get("dir") or "")
    side = str(fill.get("side") or "").upper()
    if d.startswith("Open Short") or d.startswith("Close Long"):
        return -abs(sz)
    if d.startswith("Open Long") or d.startswith("Close Short"):
        return abs(sz)
    if side == "A":
        return -abs(sz)
    if side == "B":
        return abs(sz)
    return sz


def journal_window_start(now: datetime, last_fill: Optional[datetime]) -> datetime:
    """From the newest fill time already stored. First run (empty table) looks back 26h."""
    if last_fill is None:
        return now - timedelta(hours=26)
    if last_fill.tzinfo is None:
        last_fill = last_fill.replace(tzinfo=timezone.utc)
    return last_fill


def fetch_journal(src, now: datetime, env: Dict[str, str]) -> Dict[str, Any]:
    from . import persist
    day = now.astimezone(rules.HKT).strftime("%Y-%m-%d")
    yday = (now.astimezone(rules.HKT) - timedelta(days=1)).strftime("%Y-%m-%d")
    from .sources import normalize_hl_address
    account = normalize_hl_address(env.get("HL_ADDRESS") or getattr(src, "hl_address", "") or "")
    dsn = persist.brain_dsn(env)
    last = persist.last_journal_fill_time(dsn, account) if dsn else None
    known = persist.existing_journal_fill_ids(dsn, account) if dsn else set()
    start = journal_window_start(now, last)
    end_ms = int(now.timestamp() * 1000)
    inp = {
        "day": day,
        "account": account,
        "fills": src.hl({"type": "userFillsByTime", "user": src.hl_address,
                         "startTime": int(start.timestamp() * 1000), "endTime": end_ms}),
        "state": src.hl({"type": "clearinghouseState", "user": src.hl_address}),
        "orders": src.hl({"type": "frontendOpenOrders", "user": src.hl_address}),
        "mids": src.hl({"type": "allMids"}),
        "run_report": src.cockpit(f"/api/exec/run-report?date={day}"),
        "run_report_yesterday": src.cockpit(f"/api/exec/run-report?date={yday}"),
        "existing_fill_ids": known,
        "fill_dedupe_ok": known is not None,
    }
    coins = set()
    if inp["fills"].get("ok"):
        coins.update(str(x.get("coin")) for x in hlparse.fills(inp["fills"].get("data")) if x.get("coin"))
    dec = ((inp["run_report"].get("data") or {}).get("decisions") if inp["run_report"].get("ok") else None) or {}
    for rec in (dec.get("records") or []) if isinstance(dec, dict) else []:
        if isinstance(rec, dict) and rec.get("symbol"):
            coins.add(str(rec["symbol"]))
    for p in hlparse.positions(inp["state"].get("data") if inp["state"].get("ok") else None):
        coins.add(p["coin"])
    candles = {}
    for coin in sorted(coins):
        candles[coin] = {
            "1h": src.hl({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h",
                                                             "startTime": end_ms - 400 * 3_600_000, "endTime": end_ms}}),
            "4h": src.hl({"type": "candleSnapshot", "req": {"coin": coin, "interval": "4h",
                                                             "startTime": end_ms - 450 * 14_400_000, "endTime": end_ms}}),
        }
    inp["candles"] = candles
    inp["window_start"] = start.isoformat()
    return inp


def _decision_for(dec: Any, coin: str) -> Optional[dict]:
    if not isinstance(dec, dict):
        return None
    for bucket in (dec.get("records"), dec.get("history")):
        for rec in bucket or []:
            if isinstance(rec, dict) and str(rec.get("symbol") or "").upper() == str(coin or "").upper():
                return rec
    return None


def build_journal(inputs: Dict[str, Any], now: datetime, env: Dict[str, str]) -> Dict[str, Any]:
    day = inputs.get("day") or now.astimezone(rules.HKT).strftime("%Y-%m-%d")
    problems: List[dict] = []
    fill_res = inputs.get("fills") or {}
    if not fill_res.get("ok"):
        problems.append(report.problem("DATA_UNAVAILABLE", f"fills unreadable: {fill_res.get('error') or 'missing'}"))
    dec_res = inputs.get("run_report") or {}
    dec = ((dec_res.get("data") or {}).get("decisions") if dec_res.get("ok") else None)
    if not isinstance(dec, dict):
        problems.append(report.problem("DATA_UNAVAILABLE", "decisions unreadable"))
        dec = {"records": [], "history": []}
    mids = hlparse.mids((inputs.get("mids") or {}).get("data")) if (inputs.get("mids") or {}).get("ok") else {}
    now_ms = int(now.timestamp() * 1000)
    dedupe_ok = bool(inputs.get("fill_dedupe_ok", True))
    known_ids = inputs.get("existing_fill_ids")
    if not dedupe_ok or ("existing_fill_ids" in inputs and known_ids is None):
        problems.append(report.problem("DATA_UNAVAILABLE", "trade log fill ids unreadable; fills not inserted"))
        dedupe_ok = False
        known_ids = set()
    elif known_ids is None:
        known_ids = set()
    executed = hlparse.executed_records(inputs.get("run_report"), inputs.get("run_report_yesterday"))
    orders = (inputs.get("orders") or {}).get("data") if (inputs.get("orders") or {}).get("ok") else []
    historical = inputs.get("historical_orders")
    rr = (inputs.get("run_report") or {}).get("data") if (inputs.get("run_report") or {}).get("ok") else None
    exit_actions = (rr.get("exit_actions") if isinstance(rr, dict) else None) or []
    from .sources import normalize_hl_address
    account = normalize_hl_address(env.get("HL_ADDRESS") or inputs.get("account") or "")
    rows = []
    seen = set()
    seen_ids = set(known_ids or [])
    for x in hlparse.fills(fill_res.get("data")) if fill_res.get("ok") else []:
        coin = str(x.get("coin") or "")
        fid = hlparse.fill_key(x)
        if fid and fid in seen_ids:
            seen.add(coin)
            continue
        if fid:
            seen_ids.add(fid)
        seen.add(coin)
        px = hlparse.num(x.get("px"))
        sz = signed_fill_size(x)
        side = str(x.get("side") or x.get("dir") or "")
        who = decider.decider_of(_decision_for(dec, coin))
        d = str(x.get("dir") or "")
        if d.startswith("Open"):
            order_actor = hlparse.order_actor(x, executed)
            close_actor = ""
        elif d.startswith("Close") or "liquidat" in d.lower():
            order_actor = ""
            close_actor = hlparse.close_actor(x, orders, exit_actions, historical)
        else:
            order_actor = "unknown"
            close_actor = "unknown"
        pack = (inputs.get("candles") or {}).get(coin) or {}
        c1 = (pack.get("1h") or {}).get("data") if (pack.get("1h") or {}).get("ok") else None
        c4 = (pack.get("4h") or {}).get("data") if (pack.get("4h") or {}).get("ok") else None
        both = stops.both_stops(px, px, sz, c1, c4, now_ms)
        hard_px = (both["hard"] or {}).get("price")
        hit = False
        if hard_px and px and d.startswith("Close") and px <= hard_px:
            hit = True
            problems.append(report.problem("HARD_SL_HIT", f"{coin} close px {px} at or through 4H Filter {hard_px:.6g}", coin))
        ts = datetime.fromtimestamp(int(x["time"]) / 1000, tz=rules.HKT).isoformat()
        rows.append({
            "time": ts, "coin": coin, "side": side, "size": sz, "price": px,
            "fee": hlparse.num(x.get("fee")), "pnl": hlparse.num(x.get("closedPnl")),
            "decider": who, "soft": both["soft"], "hard": both["hard"], "hard_sl_hit": hit,
            "kind": "fill", "fill_id": fid, "account": account,
            "order_actor": order_actor, "close_actor": close_actor,
        })
    insert_fills = dedupe_ok and known_ids is not None
    for rec in dec.get("records") or []:
        if not isinstance(rec, dict):
            continue
        coin = str(rec.get("symbol") or "")
        if coin in seen:
            continue
        who = decider.decider_of(rec)
        rows.append({
            "time": rec.get("timestamp"), "coin": coin, "side": rec.get("decision"), "size": rec.get("size_pct"),
            "price": None, "fee": None, "pnl": None, "decider": who,
            "soft": None, "hard": None, "hard_sl_hit": False, "kind": "decision",
            "fill_id": None, "account": account, "order_actor": "", "close_actor": "",
        })
    status = "problem" if problems else "ok"
    lines = ["## Trade journal", "",
             "time | coin | side | size | price | fee | pnl | decider | order | close | soft | hard | hard_sl_hit", ""]
    for r in rows:
        actor = ""
        if r.get("order_actor"):
            actor = f"order: {r['order_actor']}"
        elif r.get("close_actor"):
            actor = f"close: {r['close_actor']}"
        lines.append(
            f"{r['time']} | {r['coin']} | {r['side']} | {r['size']} | {r['price']} | {r['fee']} | {r['pnl']} | "
            f"{r['decider']} | {actor} | "
            f"{stops.fmt_level(r['soft']) if r.get('soft') else '未知'} | "
            f"{stops.fmt_level(r['hard']) if r.get('hard') else '未知'} | {r['hard_sl_hit']}")
    if not rows:
        lines.append("(no fills or decisions today)")
    md = "\n".join(lines) + "\n"
    values = []
    stored = rows if insert_fills else [r for r in rows if r.get("kind") != "fill"]
    for r in stored:
        values.append((now.isoformat(), r["time"], r["coin"], r["side"], r["size"], r["price"], r["fee"], r["pnl"],
                       r["decider"], r.get("soft"), r.get("hard"), bool(r["hard_sl_hit"]),
                       {"kind": r["kind"], "fill_id": r.get("fill_id"), "account": r.get("account") or None,
                        "order_actor": r.get("order_actor") or None, "close_actor": r.get("close_actor") or None}))
    brain = []
    if values:
        brain.append({
            "table": "raw.river_trade_log",
            "columns": ["run_at", "trade_time", "coin", "side", "size", "price", "fee", "pnl", "decider",
                        "soft_sl", "hard_sl", "hard_sl_hit", "report"],
            "values": values,
        })
    notify = "problem" if problems else None
    summary = f"trade journal {day}: {len(rows)} rows" + (" · Hard SL hit" if any(r["hard_sl_hit"] for r in rows) else "")
    return report.shell("river_trade_log", now, status, summary, problems, notify, md, brain_rows=brain, rows=rows)
