"""The 09:22 summary sections: run report, decisions, fills, open positions with both stops, BX status."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from . import decider, hlparse, rules, stops


def build(inputs: Dict[str, Any], now: datetime) -> str:
    day = now.astimezone(rules.HKT).strftime("%Y-%m-%d")
    lines = ["### Today's 08:55 run report", ""]
    rr = (inputs.get("run_report_today") or {}).get("data") if (inputs.get("run_report_today") or {}).get("ok") else None
    if not isinstance(rr, dict):
        lines.append("- 未知 (run report unavailable)")
    else:
        lines.append(f"- date {rr.get('date')} · runs {len(rr.get('runs') or [])} · "
                     f"executed {len(rr.get('executed') or [])} · skipped {len(rr.get('skipped') or [])} · "
                     f"failed {len(rr.get('failed') or [])}")
        for item in (rr.get("executed") or [])[:12]:
            lines.append(f"- executed {item.get('symbol')} px {item.get('px')} mid {item.get('mid')} "
                         f"hard_sl {item.get('hard_sl')}")
        if not rr.get("executed"):
            lines.append("- executed: none")
    lines += ["", "### Decisions stored", ""]
    dec = (rr or {}).get("decisions") if isinstance(rr, dict) else None
    if not isinstance(dec, dict):
        lines.append("- 未知 (run report has no decisions field)")
    elif not dec.get("ok", True):
        lines.append(f"- 未知 ({dec.get('error') or 'unreadable'})")
    elif not dec.get("posted"):
        lines.append("- none stored for this HKT date")
    else:
        lines.append(f"- {dec.get('count')} stored")
        for rec in dec.get("records") or []:
            who = decider.decider_for_coin(rec.get("symbol"), dec, day)
            lines.append(f"- {rec.get('symbol')} {rec.get('decision')} type {rec.get('type')} "
                         f"source {rec.get('source')} decider {who} at {rec.get('timestamp')}")
    lines += ["", "### Fills (today HKT)", ""]
    fill_res = inputs.get("hl_fills") or {}
    if not fill_res.get("ok"):
        lines.append(f"- 未知 ({fill_res.get('error') or 'fills unavailable'})")
    else:
        start, end = hlparse.window_today(now)
        today = hlparse.in_window(hlparse.fills(fill_res.get("data")), start, end)
        if not today:
            lines.append("- none")
        for x in today[:20]:
            lines.append(f"- {x.get('coin')} {x.get('dir')} sz {x.get('sz')} px {x.get('px')} "
                         f"fee {x.get('fee')} closedPnl {x.get('closedPnl')}")
    lines += ["", "### Open positions (1H Lower soft exit, 4H Filter Hard SL)", ""]
    state = inputs.get("hl_state") or {}
    if not state.get("ok"):
        lines.append("- 未知 (no clearinghouse snapshot)")
    else:
        pos = hlparse.positions(state.get("data"))
        mids = hlparse.mids((inputs.get("all_mids") or {}).get("data"))
        orders = (inputs.get("hl_orders") or {}).get("data") if (inputs.get("hl_orders") or {}).get("ok") else None
        if not pos:
            lines.append("- none")
        now_ms = int(now.timestamp() * 1000)
        for p in pos:
            coin = p["coin"]
            mark = mids.get(coin)
            if mark is None and p.get("position_value") and p.get("szi"):
                mark = abs(p["position_value"] / p["szi"])
            candles = (inputs.get("pos_candles") or {}).get(coin) or {}
            c1 = (candles.get("1h") or {}).get("data") if (candles.get("1h") or {}).get("ok") else None
            c4 = (candles.get("4h") or {}).get("data") if (candles.get("4h") or {}).get("ok") else None
            both = stops.both_stops(mark, p.get("entry_px"), p.get("szi"), c1, c4, now_ms)
            resting = (hlparse.hard_sl_status(orders, coin, p["side"], p.get("szi"), mark)
                       if isinstance(orders, list) else {"ok": False})
            lines.append(f"- {coin} {p['side']} sz {p['szi']} entry {p.get('entry_px')} mark {mark} "
                         f"uPnL {p.get('unrealized_pnl')}")
            lines.append(f"  soft {stops.fmt_level(both['soft'])}")
            lines.append(f"  hard {stops.fmt_level(both['hard'])}")
            lines.append(f"  {hlparse.fmt_hard_sl(resting)}")
    lines += ["", "### BX status", ""]
    bx = (inputs.get("bx_status") or {}).get("data") if (inputs.get("bx_status") or {}).get("ok") else None
    if not isinstance(bx, dict):
        lines.append("- 未知")
    else:
        br = bx.get("breaker") or {}
        lines.append(f"- BX_LIVE={bx.get('bx_live')} breaker tripped={br.get('tripped')} "
                     f"open {len(bx.get('open') or [])} realized {bx.get('realized_pnl_usd')}")
    lines.append("")
    return "\n".join(lines)
