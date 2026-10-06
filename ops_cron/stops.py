"""One stop-distance function shared by the Harbor P&L report and the Cove BO report.

Both stops:
- soft exit: 1H Lower (1H close below this is the soft exit)
- Hard SL: 4H Filter

`sl_distance` is the distance of that level from the mark, and the position P&L if price
trades there (from entry, signed size: long > 0).
"""
from __future__ import annotations

from typing import Any, Optional

from . import gc_levels

# Published portfolio cap (GIIQ-SoT-4). Read here so a report can flag it; sizing code is not called.
MARGIN_CAP_PCT = 70.0
# Cove spec: weekly running cost to show next to P&L.
WEEKLY_COST_USD = 91.0


def sl_distance(mark: Optional[float], level: Optional[float], entry: Optional[float],
                size: Optional[float]) -> dict:
    """Distance of `level` from `mark`, and P&L if the position is closed at `level`.

    distance_pct is |mark - level| / |mark| * 100 (how far the stop sits from the mark).
    pnl_if_hit is (level - entry) * size. Long size is positive, so a stop below entry is a loss.
    Any missing input leaves that field None. Callers print 未知; they do not reuse an old number.
    """
    out: dict = {"price": None, "distance_pct": None, "pnl_if_hit": None}
    try:
        px = float(level) if level is not None else None
        mk = float(mark) if mark is not None else None
    except (TypeError, ValueError):
        return out
    if px is None or px <= 0 or mk is None or mk == 0:
        return out
    out["price"] = px
    out["distance_pct"] = abs(mk - px) / abs(mk) * 100.0
    try:
        en = float(entry) if entry is not None else None
        sz = float(size) if size is not None else None
    except (TypeError, ValueError):
        en, sz = None, None
    if en is not None and sz is not None:
        out["pnl_if_hit"] = (px - en) * sz
    return out


def liq_closer_than_hard(mark: Optional[float], liq: Optional[float], hard_px: Optional[float]) -> Optional[bool]:
    """True when the liquidation price is closer to the mark than the Hard SL (liq would win)."""
    try:
        mk, lq, hd = float(mark), float(liq), float(hard_px)
    except (TypeError, ValueError):
        return None
    if mk == 0 or lq <= 0 or hd <= 0:
        return None
    return abs(mk - lq) < abs(mk - hd)


def both_stops(mark, entry, size, candles_1h, candles_4h, now_ms: int) -> dict:
    """1H Lower soft exit and 4H Filter Hard SL, each through `sl_distance`."""
    soft_lv = gc_levels.channel_at(candles_1h, "1h", now_ms)
    hard_lv = gc_levels.channel_at(candles_4h, "4h", now_ms)
    soft = sl_distance(mark, soft_lv.get("lower"), entry, size)
    hard = sl_distance(mark, hard_lv.get("filter"), entry, size)
    soft["label"] = "1H Lower"
    hard["label"] = "4H Filter"
    soft["level_ok"] = bool(soft_lv.get("ok"))
    hard["level_ok"] = bool(hard_lv.get("ok"))
    soft["level_error"] = soft_lv.get("error") or ""
    hard["level_error"] = hard_lv.get("error") or ""
    return {"soft": soft, "hard": hard}


def fmt_level(lv: dict) -> str:
    if not lv or lv.get("price") is None:
        why = (lv or {}).get("level_error") or "no level"
        return f"未知 ({why})" if why else "未知"
    pnl = lv.get("pnl_if_hit")
    pnl_s = "未知" if pnl is None else f"{pnl:+.2f}"
    return f"{lv['price']:.6g} / {lv['distance_pct']:.2f}% from mark / P&L if hit {pnl_s}"


def num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
