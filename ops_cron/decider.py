"""Who actually decided a trade.

Cockpit stores the last decision per coin. source \"claude\" is written for every non-fallback
POST today, so it does not prove Claude.ai versus Forge. With no stronger evidence the decider
is unknown. source fallback is the Railway 08:50 path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from . import rules

RAILWAY_FALLBACK = "Railway 08:50 fallback"
CLAUDE = "Claude.ai"
FORGE = "Forge"
UNKNOWN = "unknown"

_BY_SOURCE = {
    "fallback": RAILWAY_FALLBACK,
    "railway": RAILWAY_FALLBACK,
    "railway_fallback": RAILWAY_FALLBACK,
    "forge": FORGE,
    "claude.ai": CLAUDE,
    "claudeai": CLAUDE,
}
_EXPLICIT = {CLAUDE, FORGE, RAILWAY_FALLBACK, UNKNOWN}


def _ts(v: Any) -> Optional[datetime]:
    return rules._ts(v)


def decider_of(rec: Optional[dict]) -> str:
    if not isinstance(rec, dict):
        return UNKNOWN
    explicit = str(rec.get("decider") or rec.get("actor") or "").strip()
    if explicit in _EXPLICIT:
        return explicit
    src = str(rec.get("source") or "").strip().lower()
    if src in _BY_SOURCE:
        return _BY_SOURCE[src]
    return UNKNOWN


def last_before(records: Iterable[dict], day: str, cutoff: tuple = (8, 55)) -> Optional[dict]:
    """Last record on `day` (YYYY-MM-DD, HKT) whose timestamp is strictly before cutoff."""
    best = None
    best_ts = None
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        ts = _ts(rec.get("timestamp"))
        if ts is None:
            continue
        hkt = ts.astimezone(rules.HKT)
        if hkt.strftime("%Y-%m-%d") != day:
            continue
        if (hkt.hour, hkt.minute) >= cutoff:
            continue
        if best_ts is None or ts >= best_ts:
            best, best_ts = rec, ts
    return best


def decider_for_coin(symbol: str, decisions: Optional[dict], day: str) -> str:
    """Decider = whoever's POST was last before 08:55 HKT. No evidence → unknown."""
    if not isinstance(decisions, dict):
        return UNKNOWN
    hist = [r for r in (decisions.get("history") or []) if isinstance(r, dict)
            and str(r.get("symbol") or "").upper() == str(symbol or "").upper()]
    chosen = last_before(hist, day)
    if chosen is None:
        recs = decisions.get("records") or []
        for rec in recs:
            if isinstance(rec, dict) and str(rec.get("symbol") or "").upper() == str(symbol or "").upper():
                ts = _ts(rec.get("timestamp"))
                if ts is not None:
                    hkt = ts.astimezone(rules.HKT)
                    if hkt.strftime("%Y-%m-%d") == day and (hkt.hour, hkt.minute) < (8, 55):
                        chosen = rec
                        break
    return decider_of(chosen)


def posted_today(decisions: Optional[dict]) -> bool:
    """A non-fallback desk POST is in today's decision file."""
    if not isinstance(decisions, dict) or not decisions.get("ok", True):
        return False
    rows = list(decisions.get("records") or [])
    return any(str((r or {}).get("source") or "").lower() != "fallback" for r in rows if isinstance(r, dict))
