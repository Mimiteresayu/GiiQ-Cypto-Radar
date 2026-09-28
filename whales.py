#!/usr/bin/env python3
"""Smart-money (whale) positioning on Hyperliquid — input for the `smart_money` dimension.

Wallet set = union of
  1. AUTO: top wallets from the public HL leaderboard (stats-data.hyperliquid.xyz), filtered to
     profitable, sizeable, non-market-maker accounts (refreshed once per HKT day, cached on volume).
  2. MANUAL: addresses MMT adds (e.g. traders spotted on fomo.family / HyperDash / CoinGlass),
     stored in out/whales_watchlist.json (volume) or data/whales_watchlist.json (baked default).
     fomo.family has no public API; its perps settle on Hyperliquid, so a trader's HL address can
     be tracked here like any other wallet.

For every wallet we read clearinghouseState (public, no key) and aggregate per coin:
  long_ntl / short_ntl (USD notional) and n_long / n_short (wallet counts).

Read-only public data. No keys, no orders. Safe to expose as a TaaS data product later.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

LEADERBOARD_URL = os.environ.get("HL_LEADERBOARD_URL", "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard")
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Protocol vaults / known non-directional accounts to ignore (lower-case).
EXCLUDE = {
    "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303",  # HLP vault
}
TOP_N = int(os.environ.get("WHALE_TOP_N", "40"))
MIN_ACCOUNT_USD = float(os.environ.get("WHALE_MIN_ACCOUNT_USD", "500000"))
MAX_TURNOVER = float(os.environ.get("WHALE_MAX_MONTH_TURNOVER", "300"))  # month volume / account value; above = likely MM
MAX_WALLETS = int(os.environ.get("WHALE_MAX_WALLETS", "80"))
TIME_BUDGET_S = float(os.environ.get("WHALE_TIME_BUDGET_S", "240"))


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def valid_address(a: Any) -> bool:
    return isinstance(a, str) and bool(ADDR_RE.match(a.strip()))


def hkt_day(now: Optional[datetime] = None) -> str:
    return ((now or datetime.now(timezone.utc)) + timedelta(hours=8)).strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# Leaderboard -> smart-money list
# ---------------------------------------------------------------------------
def fetch_leaderboard(timeout: int = 90) -> List[dict]:
    req = urllib.request.Request(LEADERBOARD_URL, headers={"User-Agent": "giiq-radar/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    rows = data.get("leaderboardRows") if isinstance(data, dict) else data
    return rows if isinstance(rows, list) else []


def _windows(row: dict) -> Dict[str, dict]:
    """windowPerformances: [["day", {...}], ["week", {...}], ...] or a dict -> {window: perf}."""
    wp = row.get("windowPerformances") or []
    if isinstance(wp, dict):
        return {str(k): v or {} for k, v in wp.items()}
    out = {}
    for item in wp:
        if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict):
            out[str(item[0])] = item[1]
    return out


def select_smart_money(rows: List[dict], top_n: int = TOP_N, min_account: float = MIN_ACCOUNT_USD,
                       max_turnover: float = MAX_TURNOVER) -> List[dict]:
    picked = []
    for r in rows or []:
        addr = str(r.get("ethAddress") or "").lower()
        if not valid_address(addr) or addr in EXCLUDE:
            continue
        acct = _f(r.get("accountValue")) or 0.0
        if acct < min_account:
            continue
        w = _windows(r)
        m, a = w.get("month") or {}, w.get("allTime") or {}
        pnl_m, roi_m, vlm_m = _f(m.get("pnl")), _f(m.get("roi")), _f(m.get("vlm"))
        pnl_a = _f(a.get("pnl"))
        if not (pnl_m and pnl_m > 0 and roi_m and roi_m > 0 and pnl_a and pnl_a > 0):
            continue
        turnover = (vlm_m or 0.0) / acct if acct else 0.0
        if turnover > max_turnover:
            continue  # very high turnover vs equity -> market maker / HFT, not a directional view
        picked.append({"address": addr, "label": r.get("displayName") or "", "source": "leaderboard",
                       "account_value": round(acct, 0), "pnl_month": round(pnl_m, 0), "roi_month": round(roi_m, 4),
                       "pnl_all": round(pnl_a, 0), "turnover_month": round(turnover, 1)})
    picked.sort(key=lambda x: -x["pnl_month"])
    return picked[:top_n]


def load_manual_watchlist(paths: List[str]) -> List[dict]:
    """First existing file wins. Format: {"wallets": [{"address", "label", "source"}]}."""
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        items = data.get("wallets") if isinstance(data, dict) else data
        out = []
        for it in items or []:
            a = str((it or {}).get("address") or "").strip().lower()
            if valid_address(a):
                out.append({"address": a, "label": str(it.get("label") or "")[:60],
                            "source": str(it.get("source") or "manual")[:30]})
        return out
    return []


# ---------------------------------------------------------------------------
# Positions -> per-coin aggregate
# ---------------------------------------------------------------------------
def wallet_positions(state: Any) -> List[dict]:
    out = []
    groups = state.get("assetPositions") or [] if isinstance(state, dict) else []
    for g in groups:
        p = (g or {}).get("position") or {}
        szi, ntl = _f(p.get("szi")), _f(p.get("positionValue"))
        if not p.get("coin") or not szi or ntl is None:
            continue
        out.append({"coin": str(p["coin"]).upper(), "side": "long" if szi > 0 else "short", "ntl": abs(ntl)})
    return out


def aggregate(per_wallet: Dict[str, List[dict]]) -> Dict[str, dict]:
    coins: Dict[str, dict] = {}
    for _addr, positions in per_wallet.items():
        for p in positions:
            a = coins.setdefault(p["coin"], {"long_ntl": 0.0, "short_ntl": 0.0, "n_long": 0, "n_short": 0})
            a[f"{p['side']}_ntl"] += p["ntl"]
            a[f"n_{p['side']}"] += 1
    for a in coins.values():
        a["long_ntl"], a["short_ntl"] = round(a["long_ntl"], 0), round(a["short_ntl"], 0)
    return coins


def build_snapshot(hl_post: Callable[[dict], Any], out_dir: str, now: Optional[datetime] = None,
                   leaderboard_fetch: Callable[[], List[dict]] = fetch_leaderboard,
                   sleep_s: float = 0.15) -> Dict[str, Any]:
    """Refresh smart-money list (once per HKT day) + read every wallet's positions."""
    now = now or datetime.now(timezone.utc)
    wdir = os.path.join(out_dir, "whales")
    os.makedirs(wdir, exist_ok=True)
    errors: List[str] = []

    sm_path = os.path.join(wdir, f"smart_money_{hkt_day(now)}.json")
    smart: List[dict] = []
    try:
        with open(sm_path, encoding="utf-8") as f:
            smart = json.load(f)
    except (OSError, ValueError):
        try:
            smart = select_smart_money(leaderboard_fetch())
            _atomic(sm_path, smart)
        except Exception as e:  # leaderboard down -> fall back to the latest cached list
            errors.append(f"leaderboard: {e}")
            smart = _latest_cached_list(wdir)

    manual = load_manual_watchlist([os.path.join(out_dir, "whales_watchlist.json"),
                                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "whales_watchlist.json")])
    wallets: Dict[str, dict] = {}
    for w in manual + smart:  # manual first: its label/source wins on duplicates
        wallets.setdefault(w["address"], w)
    wallet_list = list(wallets.values())[:MAX_WALLETS]

    per_wallet: Dict[str, List[dict]] = {}
    meta: List[dict] = []
    t0 = time.time()
    for w in wallet_list:
        if time.time() - t0 > TIME_BUDGET_S:
            errors.append(f"time budget hit after {len(per_wallet)} wallets")
            break
        try:
            state = hl_post({"type": "clearinghouseState", "user": w["address"]})
            pos = wallet_positions(state)
            per_wallet[w["address"]] = pos
            meta.append({**w, "n_positions": len(pos),
                         "account_value_now": _f(((state or {}).get("marginSummary") or {}).get("accountValue"))})
        except Exception as e:
            errors.append(f"{w['address'][:10]}: {e}")
        if sleep_s:
            time.sleep(sleep_s)

    snap = {"ts": now.isoformat(), "hkt_day": hkt_day(now), "n_wallets": len(per_wallet),
            "n_manual": sum(1 for m in meta if m.get("source") != "leaderboard"),
            "coins": aggregate(per_wallet), "wallets": meta, "errors": errors[:20]}
    _atomic(os.path.join(wdir, "latest.json"), snap)
    _atomic(os.path.join(wdir, f"snapshot_{hkt_day(now)}.json"), snap)
    return snap


def load_previous(out_dir: str, today: str) -> Optional[dict]:
    """Most recent snapshot from an EARLIER HKT day (for day-over-day change)."""
    wdir = os.path.join(out_dir, "whales")
    try:
        names = sorted(n for n in os.listdir(wdir) if n.startswith("snapshot_") and n[9:17] < today)
    except OSError:
        return None
    return _read(os.path.join(wdir, names[-1])) if names else None


def _latest_cached_list(wdir: str) -> List[dict]:
    try:
        names = sorted(n for n in os.listdir(wdir) if n.startswith("smart_money_"))
    except OSError:
        return []
    return (_read(os.path.join(wdir, names[-1])) or []) if names else []


def _read(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _atomic(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def save_manual_watchlist(out_dir: str, wallets: List[dict]) -> Dict[str, Any]:
    """Replace the volume watchlist. Returns accepted / rejected entries."""
    ok, bad = [], []
    for it in wallets or []:
        a = str((it or {}).get("address") or "").strip().lower()
        if valid_address(a):
            ok.append({"address": a, "label": str(it.get("label") or "")[:60],
                       "source": str(it.get("source") or "manual")[:30]})
        else:
            bad.append(it)
    if len(ok) > MAX_WALLETS:
        return {"ok": False, "error": f"max {MAX_WALLETS} wallets"}
    _atomic(os.path.join(out_dir, "whales_watchlist.json"),
            {"updated": datetime.now(timezone.utc).isoformat(), "wallets": ok})
    return {"ok": True, "accepted": len(ok), "rejected": bad}


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import scan_gc_radar as sgr
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
    s = build_snapshot(sgr.hl_post, out)
    print(json.dumps({k: s[k] for k in ("ts", "n_wallets", "n_manual", "errors")}, indent=2))
