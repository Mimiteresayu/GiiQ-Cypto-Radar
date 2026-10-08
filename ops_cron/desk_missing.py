"""08:30 HKT: alert when today's cockpit decision file has no non-fallback desk POST.

Does not store a fallback and does not change the 08:50 Railway fallback.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from . import decider, report, rules

ALERT = ("no Claude desk POST yet; Railway 08:50 fallback will apply "
         "(HL Base 2% + Hard SL, Chase veto)")


def fetch(src, now: datetime, env: Dict[str, str]) -> Dict[str, Any]:
    day = now.astimezone(rules.HKT).strftime("%Y-%m-%d")
    return {"day": day, "run_report": src.cockpit(f"/api/exec/run-report?date={day}")}


def build(inputs: Dict[str, Any], now: datetime, env: Dict[str, str]) -> Dict[str, Any]:
    day = inputs.get("day") or now.astimezone(rules.HKT).strftime("%Y-%m-%d")
    res = inputs.get("run_report") or {}
    problems = []
    if not res.get("ok"):
        problems.append(report.problem("DATA_UNAVAILABLE", f"decisions unreadable: {res.get('error') or 'not fetched'}"))
        text = problems[0]["msg"]
    else:
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        dec = data.get("decisions") if isinstance(data, dict) else None
        if not isinstance(dec, dict):
            problems.append(report.problem(
                "DATA_UNAVAILABLE",
                "run report has no decisions field; cannot see today's desk POSTs"))
            text = problems[0]["msg"]
        elif not dec.get("ok", True):
            problems.append(report.problem("DATA_UNAVAILABLE", f"decisions unreadable: {dec.get('error') or 'bad file'}"))
            text = problems[0]["msg"]
        elif decider.posted_today(dec):
            text = f"desk POST present for {day} ({dec.get('count')} stored)"
        else:
            problems.append(report.problem("DESK_MISSING", ALERT))
            text = ALERT
    status = "problem" if problems else "ok"
    md = f"## Desk missing — {day}\n\n{text}\n"
    return report.shell("desk_missing", now, status, text, problems, "problem" if problems else None, md, day=day)
