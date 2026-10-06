"""Cove BO live report. Weekday 09:32 and 20:32 HKT. Always one Telegram message.

Stops use the same `stops.sl_distance` / `stops.both_stops` function as Harbor.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from . import decider, hlparse, report, rules, stops


def fetch(src, now: datetime, env: Dict[str, str]) -> Dict[str, Any]:
    day = now.astimezone(rules.HKT).strftime("%Y-%m-%d")
    w_start, _ = hlparse.window_week(now)
    t_start, _ = hlparse.window_today(now)
    end_ms = int(now.timestamp() * 1000)
    inp: Dict[str, Any] = {
        "day": day,
        "hl_state": src.hl({"type": "clearinghouseState", "user": src.hl_address}),
        "hl_spot": src.hl({"type": "spotClearinghouseState", "user": src.hl_address}),
        "hl_orders": src.hl({"type": "frontendOpenOrders", "user": src.hl_address}),
        "all_mids": src.hl({"type": "allMids"}),
        "fills": src.hl({"type": "userFillsByTime", "user": src.hl_address,
                         "startTime": int(w_start.timestamp() * 1000), "endTime": end_ms}),
        "run_report": src.cockpit(f"/api/exec/run-report?date={day}"),
        "week_start_ms": int(w_start.timestamp() * 1000),
        "today_start_ms": int(t_start.timestamp() * 1000),
    }
    coins = [p["coin"] for p in hlparse.positions(inp["hl_state"].get("data") if inp["hl_state"].get("ok") else None)]
    candles = {}
    for coin in coins:
        candles[coin] = {
            interval: src.hl({"type": "candleSnapshot", "req": {
                "coin": coin, "interval": interval,
                "startTime": end_ms - n * bar, "endTime": end_ms}})
            for interval, n, bar in (("1h", 400, 3_600_000), ("4h", 450, 14_400_000))
        }
    inp["pos_candles"] = candles
    return inp


def _actors(coin: str, dec: dict, day: str, coin_fills: List[dict]) -> List[str]:
    who = decider.decider_for_coin(coin, dec, day) if isinstance(dec, dict) else "unknown"
    ts = None
    if isinstance(dec, dict):
        chosen = decider.last_before(dec.get("history") or [], day)
        if chosen and str(chosen.get("symbol") or "").upper() == coin.upper():
            ts = chosen.get("timestamp")
        if ts is None:
            for rec in dec.get("records") or []:
                if str(rec.get("symbol") or "").upper() == coin.upper():
                    ts = rec.get("timestamp")
                    break
    lines = [f"entry decision: {who}" + (f" at {ts}" if ts else " (no log time)" if who == "unknown" else "")]
    opens = [x for x in coin_fills if str(x.get("dir") or "").startswith("Open")]
    closes = [x for x in coin_fills if str(x.get("dir") or "").startswith("Close") or "Liquidat" in str(x.get("dir") or "")]
    if opens:
        lines.append(f"order: Railway at {opens[-1].get('time')}")
    else:
        lines.append("order: unknown")
    if closes:
        lines.append(f"close: Railway at {closes[-1].get('time')}")
    else:
        lines.append("close: unknown")
    return lines


def build(inputs: Dict[str, Any], now: datetime, env: Dict[str, str]) -> Dict[str, Any]:
    day = inputs.get("day") or now.astimezone(rules.HKT).strftime("%Y-%m-%d")
    session = "morning" if now.astimezone(rules.HKT).hour < 12 else "evening"
    problems: List[dict] = []
    state_res = inputs.get("hl_state") or {}
    state = state_res.get("data") if state_res.get("ok") else None
    if not state_res.get("ok"):
        problems.append(report.problem("DATA_UNAVAILABLE", f"HL account unreadable: {state_res.get('error') or 'missing'}"))
    nav = hlparse.account_value(state) if state_res.get("ok") else None
    spot_res = inputs.get("hl_spot") or {}
    usdc = hlparse.spot_usdc(spot_res.get("data")) if spot_res.get("ok") else None
    margin = hlparse.margin_used(state) if state_res.get("ok") else None
    if nav and margin is not None and nav > 0 and (margin / nav * 100.0) > stops.MARGIN_CAP_PCT:
        problems.append(report.problem(
            "MARGIN_HIGH", f"margin {margin / nav * 100:.1f}% of NAV is above the {stops.MARGIN_CAP_PCT:.0f}% cap"))
    fills = hlparse.fills((inputs.get("fills") or {}).get("data")) if (inputs.get("fills") or {}).get("ok") else None
    if fills is None:
        problems.append(report.problem("DATA_UNAVAILABLE", "HL fills unreadable"))
    t0, _ = hlparse.window_today(now)
    w0, _ = hlparse.window_week(now)
    day_pnl = hlparse.sum_pnl(hlparse.in_window(fills, t0, now)) if fills is not None else None
    week_pnl = hlparse.sum_pnl(hlparse.in_window(fills, w0, now)) if fills is not None else None

    rr = (inputs.get("run_report") or {}).get("data") if (inputs.get("run_report") or {}).get("ok") else None
    dec = rr.get("decisions") if isinstance(rr, dict) else None
    if not isinstance(dec, dict) or not dec.get("ok", True):
        problems.append(report.problem("DATA_UNAVAILABLE", "desk decisions unreadable"))
        posted = False
    else:
        posted = decider.posted_today(dec)
        if not posted:
            problems.append(report.problem("DESK_MISSING", "desk did not POST today"))

    pos = hlparse.positions(state)
    mids = hlparse.mids((inputs.get("all_mids") or {}).get("data")) if (inputs.get("all_mids") or {}).get("ok") else {}
    orders = (inputs.get("hl_orders") or {}).get("data") if (inputs.get("hl_orders") or {}).get("ok") else None
    if (inputs.get("hl_orders") or {}).get("ok") is False:
        problems.append(report.problem("DATA_UNAVAILABLE", "HL open orders unreadable"))
    now_ms = int(now.timestamp() * 1000)
    blocks = []
    for p in pos:
        coin = p["coin"]
        mark = mids.get(coin)
        if mark is None and p.get("position_value") and p.get("szi"):
            mark = abs(p["position_value"] / p["szi"])
        candles = (inputs.get("pos_candles") or {}).get(coin) or {}
        c1 = (candles.get("1h") or {}).get("data") if (candles.get("1h") or {}).get("ok") else None
        c4 = (candles.get("4h") or {}).get("data") if (candles.get("4h") or {}).get("ok") else None
        both = stops.both_stops(mark, p.get("entry_px"), p.get("szi"), c1, c4, now_ms)
        resting = hlparse.hard_sl_status(orders, coin, p["side"], p.get("szi"), mark) if isinstance(orders, list) else None
        if isinstance(orders, list) and not (resting or {}).get("ok"):
            problems.append(report.problem("NO_SL", f"Hard SL is not on HL for {coin}", coin))
        coin_fills = [x for x in (fills or []) if x.get("coin") == coin]
        who = _actors(coin, dec if isinstance(dec, dict) else {}, day, coin_fills)
        blocks.append("\n".join([
            f"### {coin}",
            f"entry {p.get('entry_px')} mark {mark} unrealized {report.usd(p.get('unrealized_pnl'))}",
            f"soft exit (1H close < 1H Lower): {stops.fmt_level(both['soft'])}",
            f"Hard SL (4H Filter level): {stops.fmt_level(both['hard'])}",
            hlparse.fmt_hard_sl(resting or {"ok": False}),
            *who,
        ]))

    cost = stops.WEEKLY_COST_USD
    vs = None if week_pnl is None else week_pnl - cost
    lines = [
        f"# BO live {day} {session}",
        "",
        f"perp accountValue {report.known_usd(nav, state_res.get('ok') and nav is not None)} · "
        f"spot USDC {report.known_usd(usdc, spot_res.get('ok') and usdc is not None)} · "
        f"today P&L {report.usd(day_pnl)} · week P&L {report.usd(week_pnl)} · "
        f"week cost ~${cost:.0f} · week P&L minus cost {report.usd(vs)}",
        "",
        "## Positions",
        "",
        *(blocks or ["- none"]),
        "",
        "## Anomalies",
        "",
    ]
    if problems:
        lines += [f"- {p['code']}: {p['msg']}" for p in problems]
    else:
        lines.append("- none")
    if not posted and not any(p["code"] == "DATA_UNAVAILABLE" and "decisions" in p["msg"] for p in problems):
        lines.append("- desk POST: missing")
    md = "\n".join(lines) + "\n"
    status = "problem" if problems else "ok"
    summary = f"BO {session} {day} NAV {report.usd(nav)} today {report.usd(day_pnl)} week {report.usd(week_pnl)}"
    brain = [{
        "table": "raw.bo_live_report",
        "columns": ["run_at", "report_date", "session", "nav", "pnl_day", "pnl_week", "markdown", "report"],
        "values": [(now.isoformat(), day, session, nav, day_pnl, week_pnl, md, {"summary": summary, "problems": problems})],
    }]
    return report.shell("bo_live_report", now, status, summary, problems, "report", md, brain_rows=brain, session=session)
