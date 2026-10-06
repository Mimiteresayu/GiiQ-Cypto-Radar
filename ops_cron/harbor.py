"""Harbor daily P&L + portfolio. Read-only. Always one message.

SL levels go through stops.sl_distance / stops.both_stops (shared with the Cove BO report).
Missing numbers are 未知. Nothing is filled in from a previous run.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import decider, hlparse, report, rules, stops

UNKNOWN = "未知"


def fetch(src, now: datetime, env: Dict[str, str]) -> Dict[str, Any]:
    day = now.astimezone(rules.HKT).strftime("%Y-%m-%d")
    y_start, y_end = hlparse.window_yesterday(now)
    w_start, _w_end = hlparse.window_days(now, 7)
    end_ms = int(now.timestamp() * 1000)
    inp: Dict[str, Any] = {
        "day": day,
        "hl_state": src.hl({"type": "clearinghouseState", "user": src.hl_address}),
        "hl_orders": src.hl({"type": "frontendOpenOrders", "user": src.hl_address}),
        "all_mids": src.hl({"type": "allMids"}),
        "fills_7d": src.hl({"type": "userFillsByTime", "user": src.hl_address,
                            "startTime": int(w_start.timestamp() * 1000), "endTime": end_ms}),
        "fills_all": src.hl({"type": "userFills", "user": src.hl_address}),
        "funding_7d": src.hl({"type": "userFunding", "user": src.hl_address,
                              "startTime": int(w_start.timestamp() * 1000)}),
        "run_report": src.cockpit(f"/api/exec/run-report?date={day}"),
        "bx_status": src.cockpit("/api/bx/status"),
        "yesterday_start_ms": int(y_start.timestamp() * 1000),
        "yesterday_end_ms": int(y_end.timestamp() * 1000),
    }
    state = inp["hl_state"]
    coins = [p["coin"] for p in hlparse.positions(state.get("data") if state.get("ok") else None)]
    candles = {}
    for coin in coins:
        candles[coin] = {}
        for interval, n_bars, bar_ms in (("1h", 400, 3_600_000), ("4h", 450, 14_400_000)):
            candles[coin][interval] = src.hl({"type": "candleSnapshot", "req": {
                "coin": coin, "interval": interval,
                "startTime": end_ms - n_bars * bar_ms, "endTime": end_ms}})
    inp["pos_candles"] = candles
    return inp


def _fee(env: Dict[str, str]) -> dict:
    root = Path(env.get("RIVER_ADMIN_DIR") or "river/admin")
    if not root.is_dir():
        return {"usd": None, "note": UNKNOWN, "owner": "River", "source": None}
    files = sorted(root.glob("ai_infra_usage_review_*.md"))
    if not files:
        return {"usd": None, "note": UNKNOWN, "owner": "River", "source": None}
    text = files[-1].read_text(encoding="utf-8", errors="replace")
    m = re.search(r"(?:月費|monthly fee|monthly)[^\n]{0,40}?\$?\s*(\d+(?:\.\d+)?)", text, re.I)
    usd = float(m.group(1)) if m else None
    return {"usd": usd, "note": None if usd is not None else UNKNOWN, "owner": "River", "source": files[-1].name}


def _decisions(inp: Dict[str, Any]) -> Optional[dict]:
    res = inp.get("run_report") or {}
    data = res.get("data") if res.get("ok") and isinstance(res.get("data"), dict) else None
    dec = (data or {}).get("decisions") if isinstance(data, dict) else None
    return dec if isinstance(dec, dict) else None


def _strategy(symbol: str, dec: Optional[dict]) -> str:
    if not isinstance(dec, dict):
        return UNKNOWN
    for rec in dec.get("records") or []:
        if isinstance(rec, dict) and str(rec.get("symbol") or "").upper() == symbol.upper():
            t = rec.get("type")
            return str(t) if t else UNKNOWN
    return UNKNOWN


def build(inputs: Dict[str, Any], now: datetime, env: Dict[str, str]) -> Dict[str, Any]:
    day = inputs.get("day") or now.astimezone(rules.HKT).strftime("%Y-%m-%d")
    problems: List[dict] = []
    state_res = inputs.get("hl_state") or {}
    state = state_res.get("data") if state_res.get("ok") else None
    if not state_res.get("ok"):
        problems.append(report.problem("DATA_UNAVAILABLE", f"HL account unreadable: {state_res.get('error') or 'missing'}"))
    nav = hlparse.account_value(state)
    usdc = hlparse.raw_usdc(state)
    pos = hlparse.positions(state)
    upnl = sum(p["unrealized_pnl"] or 0.0 for p in pos) if pos else (None if state is None else 0.0)

    fills_7 = hlparse.fills((inputs.get("fills_7d") or {}).get("data")) if (inputs.get("fills_7d") or {}).get("ok") else None
    fills_all = hlparse.fills((inputs.get("fills_all") or {}).get("data")) if (inputs.get("fills_all") or {}).get("ok") else None
    if fills_7 is None:
        problems.append(report.problem("DATA_UNAVAILABLE", "HL fills (7d) unreadable"))
    all_truncated = fills_all is not None and len(fills_all) >= 2000
    y0, y1 = hlparse.window_yesterday(now)
    d0, _d1 = hlparse.window_days(now, 7)
    y_fills = hlparse.in_window(fills_7, y0, y1) if fills_7 is not None else None
    w_fills = hlparse.in_window(fills_7, d0, now) if fills_7 is not None else None
    y_pnl = hlparse.sum_pnl(y_fills) if y_fills is not None else None
    w_pnl = hlparse.sum_pnl(w_fills) if w_fills is not None else None
    all_pnl = None if fills_all is None or all_truncated else hlparse.sum_pnl(fills_all)

    fund = hlparse.funding_usdc((inputs.get("funding_7d") or {}).get("data")) if (inputs.get("funding_7d") or {}).get("ok") else None
    if fund is not None:
        y_fund = sum(x["usdc"] for x in fund if y0.timestamp() * 1000 <= x["time"] < y1.timestamp() * 1000)
        w_fund = sum(x["usdc"] for x in fund if d0.timestamp() * 1000 <= x["time"] <= now.timestamp() * 1000)
    else:
        y_fund = w_fund = None

    dec = _decisions(inputs)
    mids = hlparse.mids((inputs.get("all_mids") or {}).get("data")) if (inputs.get("all_mids") or {}).get("ok") else {}
    orders = (inputs.get("hl_orders") or {}).get("data") if (inputs.get("hl_orders") or {}).get("ok") else None
    if (inputs.get("hl_orders") or {}).get("ok") is False:
        problems.append(report.problem("DATA_UNAVAILABLE", "HL open orders unreadable"))
    now_ms = int(now.timestamp() * 1000)

    pos_lines = []
    pos_rows = []
    for p in pos:
        coin = p["coin"]
        mark = mids.get(coin)
        if mark is None and p.get("position_value") and p.get("szi"):
            mark = abs(p["position_value"] / p["szi"])
        candles = (inputs.get("pos_candles") or {}).get(coin) or {}
        c1 = (candles.get("1h") or {}).get("data") if (candles.get("1h") or {}).get("ok") else None
        c4 = (candles.get("4h") or {}).get("data") if (candles.get("4h") or {}).get("ok") else None
        both = stops.both_stops(mark, p.get("entry_px"), p.get("szi"), c1, c4, now_ms)
        resting = hlparse.hard_sl_order(orders, coin) if isinstance(orders, list) else None
        if isinstance(orders, list) and resting is None:
            problems.append(report.problem("NO_SL", f"Hard SL missing on open HL position {coin}", coin))
        hard_px = (both["hard"] or {}).get("price")
        if resting is not None:
            hard_px = hlparse.trigger_px(resting) or hard_px
        if stops.liq_closer_than_hard(mark, p.get("liq"), hard_px):
            problems.append(report.problem("LIQ_INSIDE_SL", f"liq is closer than Hard SL on {coin}", coin))
        who = decider.decider_for_coin(coin, dec, day)
        pos_rows.append({"coin": coin, "stops": both, "decider": who, "mark": mark})
        pos_lines.append(
            f"- {coin} {p['side']} sz {p['szi']} entry {p.get('entry_px')} mark {mark} "
            f"uPnL {report.usd(p.get('unrealized_pnl'))} decider {who}\n"
            f"  soft (1H Lower) {stops.fmt_level(both['soft'])}\n"
            f"  hard (4H Filter) {stops.fmt_level(both['hard'])}\n"
            f"  HL trigger {'yes @ ' + str(hlparse.trigger_px(resting)) if resting else 'MISSING'}"
        )

    close_lines = []
    by_coin: Dict[str, float] = {}
    by_strat: Dict[str, float] = {}
    if y_fills is None:
        close_lines.append(f"- {UNKNOWN}")
    else:
        grouped: Dict[str, List[dict]] = {}
        for x in y_fills:
            if str(x.get("dir") or "").startswith("Close") or "Liquidat" in str(x.get("dir") or ""):
                grouped.setdefault(str(x.get("coin")), []).append(x)
        if not grouped:
            close_lines.append("- none")
        for coin, rows in sorted(grouped.items()):
            pnl = hlparse.sum_pnl(rows)
            strat = _strategy(coin, dec)
            who = decider.decider_for_coin(coin, dec, day)
            by_coin[coin] = by_coin.get(coin, 0.0) + pnl
            by_strat[strat] = by_strat.get(strat, 0.0) + pnl
            close_lines.append(f"- {coin} strategy {strat} realized {report.usd(pnl)} exit {UNKNOWN} decider {who}")

    def _group_pnl(rows: Optional[List[dict]]) -> Dict[str, float]:
        if rows is None:
            return {}
        out: Dict[str, float] = {}
        for x in rows:
            if str(x.get("dir") or "").startswith("Close") or "Liquidat" in str(x.get("dir") or ""):
                c = str(x.get("coin"))
                out[c] = out.get(c, 0.0) + (hlparse.num(x.get("closedPnl")) or 0) - (hlparse.num(x.get("fee")) or 0)
        return out

    w_by = _group_pnl(w_fills)
    a_by = {} if all_truncated or fills_all is None else _group_pnl(fills_all)

    sleeves = [
        ("HL", nav, "Harbor" if nav is None else None),
        ("BX", None, "Harbor"),
        ("Prop", None, "Helm"),
        ("股票", None, "MMT"),
        ("現金", None, "MMT"),
    ]
    known = [(n, v) for n, v, _o in sleeves if v is not None]
    known_sum = sum(v for _n, v in known)
    port_lines = []
    for name, value, owner in sleeves:
        if value is None:
            port_lines.append(f"- {name}: {UNKNOWN} (owner {owner})")
        else:
            share = (value / known_sum * 100.0) if known_sum else None
            port_lines.append(f"- {name}: {report.usd(value)} ({report.pct(share)} of known)")
    port_lines.append("- percents use known sleeves only; unknown sleeves are excluded")
    port_lines.append("- BX: no read-only key and no BX NAV table in Brain → 未知")

    lines = [
        f"# Harbor P&L {day}",
        "",
        f"HL USDC {report.usd(usdc if usdc is not None else nav)} · unrealized {report.usd(upnl)} · "
        f"yesterday realized {report.usd(y_pnl)} · 7d realized {report.usd(w_pnl)} · "
        f"since open realized {UNKNOWN + ' (userFills capped at 2000)' if all_truncated else report.usd(all_pnl)}",
        f"funding yesterday {report.usd(y_fund)} · funding 7d {report.usd(w_fund)}",
        "",
        "## Positions",
        "",
        *(pos_lines or ["- none"]),
        "",
        "## Yesterday closes",
        "",
        *close_lines,
        "",
        "## Realized by strategy and coin",
        "",
        f"- yesterday by strategy: " + (", ".join(f"{k} {report.usd(v)}" for k, v in sorted(by_strat.items())) or "none"),
        f"- yesterday by coin: " + (", ".join(f"{k} {report.usd(v)}" for k, v in sorted(by_coin.items())) or "none"),
        f"- 7d by coin: " + (", ".join(f"{k} {report.usd(v)}" for k, v in sorted(w_by.items())) or ("未知" if w_fills is None else "none")),
        f"- since open by coin: " + (UNKNOWN if all_truncated or fills_all is None else
                                     (", ".join(f"{k} {report.usd(v)}" for k, v in sorted(a_by.items())) or "none")),
        "",
        "## Portfolio",
        "",
        *port_lines,
        "",
    ]
    if now.astimezone(rules.HKT).weekday() == 0:
        fee = _fee(env)
        lines += [
            "## Monday: infra spend vs trading P&L",
            "",
            f"- latest monthly infra fee: {report.usd(fee['usd']) if fee['usd'] is not None else UNKNOWN} "
            f"(owner {fee['owner']}" + (f", {fee['source']}" if fee.get("source") else "") + ")",
            "- cumulative infra spend: 未知 (only the latest monthly file is read; months are not summed)",
            f"- cumulative trading P&L: {UNKNOWN if all_truncated or all_pnl is None else report.usd(all_pnl)}",
            "- infra spend and trading principal are separate lines",
            "",
        ]
    md = "\n".join(lines)
    status = "problem" if problems else "ok"
    summary = f"Harbor P&L {day} · HL {report.usd(nav)} · uPnL {report.usd(upnl)} · yesterday {report.usd(y_pnl)}"
    if problems:
        summary += " · " + "; ".join(p["code"] for p in problems[:6])
    brain = [{
        "table": "raw.harbor_pnl_daily",
        "columns": ["run_at", "report_date", "markdown", "report"],
        "values": [(now.isoformat(), day, md, {"summary": summary, "problems": problems, "nav": nav})],
    }]
    return report.shell(
        "harbor_pnl", now, status, summary, problems, "report", md,
        brain_rows=brain, out_file={"name": f"pnl_{day}.md", "text": md}, positions=pos_rows,
    )
