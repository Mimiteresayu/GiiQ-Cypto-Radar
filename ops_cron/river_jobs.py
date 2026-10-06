"""River's three read-only jobs. Insert-only into giiq-brain. Alert only on failure or a Hard SL hit.

1. c48-scoreboard — HL testnet public info, every 3h until 2026-10-07 21:00 HKT
2. desk-veto — daily 09:05 HKT
3. trade-journal — daily 09:15 HKT, one row per fill or decision, with a decider column
"""
from __future__ import annotations

from datetime import datetime
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
    if not state_res.get("ok"):
        problems.append(report.problem("DATA_UNAVAILABLE", f"testnet account unreadable: {state_res.get('error') or 'missing'}"))
    if not fill_res.get("ok"):
        problems.append(report.problem("DATA_UNAVAILABLE", f"testnet fills unreadable: {fill_res.get('error') or 'missing'}"))
    nav = hlparse.account_value(state_res.get("data") if state_res.get("ok") else None)
    nav_start = stops.num((inputs.get("nav_start") or env.get("C48_NAV_START") or "").strip() or None)
    if nav_start is None:
        problems.append(report.problem("DATA_UNAVAILABLE", "C48_NAV_START is unset; drawdown is 未知"))
    pos = hlparse.positions(state_res.get("data") if state_res.get("ok") else None)
    fills = hlparse.fills(fill_res.get("data")) if fill_res.get("ok") else []
    net = hlparse.sum_pnl(fills) if fill_res.get("ok") else None
    margin = hlparse.margin_used(state_res.get("data") if state_res.get("ok") else None)
    notional = sum(abs(p.get("position_value") or 0.0) for p in pos)
    lev = max((p.get("lev") or 0.0) for p in pos) if pos else None
    notional_pct = (notional / nav * 100.0) if nav else None
    margin_pct = (margin / nav * 100.0) if nav and margin is not None else None
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
    orders = (inputs.get("orders") or {}).get("data") if (inputs.get("orders") or {}).get("ok") else None
    for p in pos:
        if isinstance(orders, list) and hlparse.hard_sl_order(orders, p["coin"]) is None:
            problems.append(report.problem("NO_SL", f"no trigger stop on {p['coin']}", p["coin"]))
    status = "problem" if problems else "ok"
    summary = (f"C48 NAV {report.usd(nav)} net {report.usd(net)} dd {report.pct(dd)} "
               f"lev {lev if lev is not None else '未知'} notional {report.pct(notional_pct)}")
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


def _ret_48h(bars: List[dict], now_ms: int) -> Optional[float]:
    closed = [b for b in bars if b["t"] + 3_600_000 <= now_ms]
    if len(closed) < 2:
        return None
    now_c = closed[-1]["c"]
    cutoff = now_ms - 48 * 3_600_000
    then = [b for b in closed if b["t"] <= cutoff]
    if not then or not then[-1]["c"]:
        return None
    return (now_c - then[-1]["c"]) / then[-1]["c"]


def _sl_aware(bars_1h: List[dict], bars_4h: List[dict], now_ms: int, raw_ret: Optional[float]) -> tuple:
    """Return (ret_sl_aware, hard_sl_hit). Hit when a 1h low after the 48h mark trades through the 4H Filter then."""
    if raw_ret is None:
        return None, None
    ch = gc_levels.channel_at(
        [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]} for b in bars_4h], "4h",
        now_ms - 48 * 3_600_000)
    hard = ch.get("filter")
    if hard is None:
        return raw_ret, None
    cutoff = now_ms - 48 * 3_600_000
    later = [b for b in bars_1h if b["t"] >= cutoff and b["t"] + 3_600_000 <= now_ms]
    base = [b for b in bars_1h if b["t"] + 3_600_000 <= now_ms and b["t"] <= cutoff]
    if not base or not base[-1]["c"]:
        return raw_ret, None
    hit = any(b["l"] <= hard for b in later)
    if not hit:
        return raw_ret, False
    return (hard - base[-1]["c"]) / base[-1]["c"], True


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


def fetch_veto(src, now: datetime, env: Dict[str, str]) -> Dict[str, Any]:
    day = now.astimezone(rules.HKT).strftime("%Y-%m-%d")
    end_ms = int(now.timestamp() * 1000)
    start_ms = end_ms - 80 * 3_600_000
    radar = src.cockpit_public("/api/public/radar")
    decisions = src.cockpit(f"/api/exec/run-report?date={day}")
    symbols = []
    if radar.get("ok"):
        for tf in ("1d", "4h"):
            for row in _radar_rows(radar.get("data"), tf):
                if row.get("dual_cross_up") and row.get("symbol"):
                    symbols.append(str(row["symbol"]))
    dec = ((decisions.get("data") or {}).get("decisions") if decisions.get("ok") else None) or {}
    for rec in (dec.get("records") or []) if isinstance(dec, dict) else []:
        if isinstance(rec, dict) and rec.get("symbol"):
            symbols.append(str(rec["symbol"]))
    symbols = list(dict.fromkeys(symbols))[:25]
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
    return {"day": day, "radar": radar, "decisions": decisions, "symbols": symbols, "candles": candles, "bx": bx}


def build_veto(inputs: Dict[str, Any], now: datetime, env: Dict[str, str]) -> Dict[str, Any]:
    day = inputs.get("day") or now.astimezone(rules.HKT).strftime("%Y-%m-%d")
    problems: List[dict] = []
    radar_res = inputs.get("radar") or {}
    if not radar_res.get("ok"):
        problems.append(report.problem("DATA_UNAVAILABLE", f"public radar unreadable: {radar_res.get('error') or 'missing'}"))
    dec_res = inputs.get("decisions") or {}
    dec = ((dec_res.get("data") or {}).get("decisions") if dec_res.get("ok") else None)
    if not isinstance(dec, dict):
        dec = {"ok": False, "records": [], "history": []}
    now_ms = int(now.timestamp() * 1000)
    rows = []
    btc_bars = _bars_from_hl(((inputs.get("candles") or {}).get("BTC") or {}).get("1h", {}).get("data"))
    if not btc_bars:
        btc_bars = _bars_from_bx((inputs.get("bx") or {}).get("BTC", {}).get("data"))
    btc_ret = _ret_48h(btc_bars, now_ms)
    radar = radar_res.get("data") if radar_res.get("ok") else {}
    base = {str(r.get("symbol")) for r in _radar_rows(radar, "1d") if r.get("dual_cross_up")}
    chase = {str(r.get("symbol")) for r in _radar_rows(radar, "4h") if r.get("dual_cross_up")}
    symbols = [s for s in (inputs.get("symbols") or []) if s != "BTC"] or sorted(base | chase)
    for coin in symbols:
        if coin in base:
            strategy = "Base"
        elif coin in chase:
            strategy = "Chase"
        else:
            strategy = "unknown"
        rec = None
        for r in dec.get("records") or []:
            if isinstance(r, dict) and str(r.get("symbol")) == coin:
                rec = r
        decision = (rec or {}).get("decision") or "none"
        who = decider.decider_for_coin(coin, dec, day)
        pack = (inputs.get("candles") or {}).get(coin) or {}
        bars_1h = _bars_from_hl((pack.get("1h") or {}).get("data") if (pack.get("1h") or {}).get("ok") else None)
        bars_4h = _bars_from_hl((pack.get("4h") or {}).get("data") if (pack.get("4h") or {}).get("ok") else None)
        source = "HL"
        if not bars_1h:
            bx = (inputs.get("bx") or {}).get(coin) or {}
            if bx.get("ok"):
                bars_1h = _bars_from_bx(bx.get("data"))
                source = "BX"
        raw = _ret_48h(bars_1h, now_ms)
        aware, hit = _sl_aware(bars_1h, bars_4h, now_ms, raw)
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
        rows.append({
            "coin": coin, "strategy": strategy, "desk_decision": decision, "decider": who,
            "ret_48h": raw, "ret_48h_sl_aware": aware, "excess_vs_btc": excess,
            "hard_sl_hit": hit, "outcome": outcome, "candle_source": source,
        })
    if radar_res.get("ok") is False and not rows:
        pass
    status = "problem" if problems else "ok"
    lines = ["## Desk veto + CONT", "",
             "coin | strategy | desk_decision | decider | ret_48h | ret_48h_sl_aware | excess_vs_btc | hard_sl_hit | outcome",
             ""]
    for r in rows:
        lines.append(
            f"{r['coin']} | {r['strategy']} | {r['desk_decision']} | {r['decider']} | "
            f"{_n(r['ret_48h'])} | {_n(r['ret_48h_sl_aware'])} | {_n(r['excess_vs_btc'])} | "
            f"{r['hard_sl_hit']} | {r['outcome']}")
    if not rows:
        lines.append("(no signals)")
    md = "\n".join(lines) + "\n"
    brain_values = []
    for r in rows:
        brain_values.append((now.isoformat(), day, r["coin"], r["strategy"], r["desk_decision"], r["decider"],
                             r["ret_48h"], r["ret_48h_sl_aware"], r["excess_vs_btc"], r["hard_sl_hit"],
                             r["outcome"], r))
    brain = []
    if brain_values:
        brain.append({
            "table": "raw.river_desk_veto",
            "columns": ["run_at", "as_of", "coin", "strategy", "desk_decision", "decider", "ret_48h",
                        "ret_48h_sl_aware", "excess_vs_btc", "hard_sl_hit", "outcome", "report"],
            "values": brain_values,
        })
    notify = "problem" if problems else None
    summary = f"desk veto {day}: {len(rows)} signals" + (f", {len(problems)} problem(s)" if problems else "")
    return report.shell("river_desk_veto", now, status, summary, problems, notify, md, brain_rows=brain, rows=rows)


def _n(v) -> str:
    if v is None:
        return "未知"
    try:
        return f"{float(v):.4f}"
    except (TypeError, ValueError):
        return "未知"


def fetch_journal(src, now: datetime, env: Dict[str, str]) -> Dict[str, Any]:
    day = now.astimezone(rules.HKT).strftime("%Y-%m-%d")
    start, _ = hlparse.window_today(now)
    end_ms = int(now.timestamp() * 1000)
    inp = {
        "day": day,
        "fills": src.hl({"type": "userFillsByTime", "user": src.hl_address,
                         "startTime": int(start.timestamp() * 1000), "endTime": end_ms}),
        "state": src.hl({"type": "clearinghouseState", "user": src.hl_address}),
        "orders": src.hl({"type": "frontendOpenOrders", "user": src.hl_address}),
        "mids": src.hl({"type": "allMids"}),
        "run_report": src.cockpit(f"/api/exec/run-report?date={day}"),
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
    return inp


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
    rows = []
    seen = set()
    for x in hlparse.fills(fill_res.get("data")) if fill_res.get("ok") else []:
        coin = str(x.get("coin") or "")
        seen.add(coin)
        px = hlparse.num(x.get("px"))
        sz = hlparse.num(x.get("sz"))
        side = str(x.get("side") or x.get("dir") or "")
        who = decider.decider_for_coin(coin, dec, day)
        pack = (inputs.get("candles") or {}).get(coin) or {}
        c1 = (pack.get("1h") or {}).get("data") if (pack.get("1h") or {}).get("ok") else None
        c4 = (pack.get("4h") or {}).get("data") if (pack.get("4h") or {}).get("ok") else None
        both = stops.both_stops(px, px, sz if "Open" in str(x.get("dir") or "") or str(x.get("side")) == "B" else -(sz or 0),
                                 c1, c4, now_ms)
        hard_px = (both["hard"] or {}).get("price")
        hit = False
        if hard_px and px and str(x.get("dir") or "").startswith("Close") and px <= hard_px:
            hit = True
            problems.append(report.problem("HARD_SL_HIT", f"{coin} close px {px} at or through 4H Filter {hard_px:.6g}", coin))
        ts = datetime.fromtimestamp(int(x["time"]) / 1000, tz=rules.HKT).isoformat()
        rows.append({
            "time": ts, "coin": coin, "side": side, "size": sz, "price": px,
            "fee": hlparse.num(x.get("fee")), "pnl": hlparse.num(x.get("closedPnl")),
            "decider": who, "soft": both["soft"], "hard": both["hard"], "hard_sl_hit": hit,
            "kind": "fill",
        })
    for rec in dec.get("records") or []:
        if not isinstance(rec, dict):
            continue
        coin = str(rec.get("symbol") or "")
        if coin in seen:
            continue
        who = decider.decider_for_coin(coin, dec, day)
        rows.append({
            "time": rec.get("timestamp"), "coin": coin, "side": rec.get("decision"), "size": rec.get("size_pct"),
            "price": None, "fee": None, "pnl": None, "decider": who,
            "soft": None, "hard": None, "hard_sl_hit": False, "kind": "decision",
        })
    status = "problem" if problems else "ok"
    lines = ["## Trade journal", "", "time | coin | side | size | price | fee | pnl | decider | soft | hard | hard_sl_hit", ""]
    for r in rows:
        lines.append(
            f"{r['time']} | {r['coin']} | {r['side']} | {r['size']} | {r['price']} | {r['fee']} | {r['pnl']} | "
            f"{r['decider']} | {stops.fmt_level(r['soft']) if r.get('soft') else '未知'} | "
            f"{stops.fmt_level(r['hard']) if r.get('hard') else '未知'} | {r['hard_sl_hit']}")
    if not rows:
        lines.append("(no fills or decisions today)")
    md = "\n".join(lines) + "\n"
    values = []
    for r in rows:
        values.append((now.isoformat(), r["time"], r["coin"], r["side"], r["size"], r["price"], r["fee"], r["pnl"],
                       r["decider"], r.get("soft"), r.get("hard"), bool(r["hard_sl_hit"]), {"kind": r["kind"]}))
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
