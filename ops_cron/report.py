"""Shared report shell. No I/O."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from . import rules


def shell(check: str, now: datetime, status: str, summary: str, problems: List[dict], notify: Optional[str],
          markdown: str, **extra: Any) -> Dict[str, Any]:
    hk = now.astimezone(rules.HKT)
    rep: Dict[str, Any] = {
        "check": check,
        "run_at": now.isoformat(),
        "run_at_hkt": hk.strftime("%Y-%m-%d %H:%M"),
        "status": status,
        "summary": summary[:500],
        "problems": problems,
        "info": extra.pop("info", []),
        "notify": notify,
        "markdown": markdown,
    }
    rep.update(extra)
    return rep


def problem(code: str, msg: str, coin: Optional[str] = None, source: str = "ops_cron") -> dict:
    return {"code": code, "coin": coin, "msg": msg[:300], "source": source}


def usd(v) -> str:
    if v is None:
        return "未知"
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "未知"


def pct(v) -> str:
    if v is None:
        return "未知"
    try:
        return f"{float(v):.2f}%"
    except (TypeError, ValueError):
        return "未知"
